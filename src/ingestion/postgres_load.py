"""Load basic_pipeline's clean JSON into the pre-EDA PostgreSQL schema."""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.processing.foody.basic_pipeline import load_clean_dataframe, deduplicate_records
from src.processing.foody.schema import (
    FLAT_COLUMNS, NATURAL_KEY, REVIEW_COLUMNS, REVIEW_WRITE_COLUMNS,
    SHOP_COLUMNS, TABLE_COLUMN_TYPES, USER_COLUMNS,
)
load_dotenv(PROJECT_ROOT / ".env")
DB_SCHEMA = "public"
CREATE_TABLE_SQL = tuple(
    f"CREATE TABLE IF NOT EXISTS public.{table} ("
    + ", ".join(f"{column} {kind}" for column, kind in types.items()) + ")"
    for table, types in TABLE_COLUMN_TYPES.items()
)
INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_reviews_shop ON public.reviews(shop_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_user ON public.reviews(user_pseudo_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_datetime ON public.reviews(comment_datetime)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_source ON public.reviews(source_review_id) WHERE source_review_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_natural ON public.reviews(shop_id, user_pseudo_id, comment_datetime, md5(comment_text)) WHERE source_review_id IS NULL",
)
VIEW_SQL = (
    "CREATE OR REPLACE VIEW public.foody_reviews_flat AS SELECT "
    + ", ".join([f"r.{column}" for column in REVIEW_COLUMNS]
                + [f"u.{column}" for column in USER_COLUMNS[1:]]
                + [f"s.{column}" for column in SHOP_COLUMNS[1:]])
    + " FROM public.reviews r JOIN public.shops s ON r.shop_id = s.shop_id"
    + " JOIN public.users u ON r.user_pseudo_id = u.user_pseudo_id"
)


def parent_upsert_sql(table, columns, first_seen=False):
    updates = []
    for column in columns[1:]:
        expression = f"COALESCE(EXCLUDED.{column}, {table}.{column})"
        if first_seen and column == "first_seen_at":
            expression = f"LEAST(EXCLUDED.{column}, {table}.{column})"
        updates.append(f"{column} = {expression}")
    return (f"INSERT INTO public.{table} ({', '.join(columns)}) VALUES %s "
            f"ON CONFLICT ({columns[0]}) DO UPDATE SET " + ", ".join(updates))


SHOP_UPSERT_SQL = parent_upsert_sql("shops", SHOP_COLUMNS)
USER_UPSERT_SQL = parent_upsert_sql("users", USER_COLUMNS, first_seen=True)
STAGE_SQL = ("CREATE TEMP TABLE foody_pre_eda_stage ON COMMIT DROP AS SELECT "
             + ", ".join(REVIEW_WRITE_COLUMNS) + " FROM public.reviews WITH NO DATA")
STAGE_INSERT_SQL = "INSERT INTO foody_pre_eda_stage (" + ", ".join(REVIEW_WRITE_COLUMNS) + ") VALUES %s"
# Promote an unambiguous legacy record when a later crawl supplies its native ID.
# The existing database surrogate review_id remains unchanged.
NATURAL_JOIN = " AND ".join(f"r.{c} = s.{c}" for c in NATURAL_KEY)
HASH_COLLISION_SQL = """
SELECT EXISTS (
    SELECT 1 FROM foody_pre_eda_stage s JOIN public.reviews r
    ON r.shop_id = s.shop_id AND r.user_pseudo_id = s.user_pseudo_id
    AND r.comment_datetime = s.comment_datetime AND md5(r.comment_text) = md5(s.comment_text)
    WHERE r.source_review_id IS NULL AND s.source_review_id IS NULL
    AND r.comment_text <> s.comment_text
) OR EXISTS (
    SELECT 1 FROM foody_pre_eda_stage WHERE source_review_id IS NULL
    GROUP BY shop_id, user_pseudo_id, comment_datetime, md5(comment_text)
    HAVING COUNT(DISTINCT comment_text) > 1
)
"""
PROMOTE_SQL = f"""
UPDATE public.reviews r SET source_review_id = s.source_review_id
FROM (
    SELECT {', '.join(NATURAL_KEY)}, MIN(source_review_id) AS source_review_id
    FROM foody_pre_eda_stage WHERE source_review_id IS NOT NULL
    GROUP BY {', '.join(NATURAL_KEY)} HAVING COUNT(DISTINCT source_review_id) = 1
) s
WHERE r.source_review_id IS NULL AND {NATURAL_JOIN}
AND NOT EXISTS (SELECT 1 FROM public.reviews existing WHERE existing.source_review_id = s.source_review_id)
AND NOT EXISTS (SELECT 1 FROM public.reviews known
    WHERE known.source_review_id IS NOT NULL
    AND {' AND '.join(f'known.{c} = s.{c}' for c in NATURAL_KEY)})
"""
REVIEW_UPDATES = ", ".join(
    f"{c} = COALESCE(EXCLUDED.{c}, reviews.{c})"
    for c in REVIEW_WRITE_COLUMNS if c != "source_review_id"
)
NATIVE_UPSERT_SQL = (
    "INSERT INTO public.reviews (" + ", ".join(REVIEW_WRITE_COLUMNS) + ") SELECT "
    + ", ".join(REVIEW_WRITE_COLUMNS) + " FROM foody_pre_eda_stage WHERE source_review_id IS NOT NULL "
    + "ON CONFLICT (source_review_id) WHERE source_review_id IS NOT NULL DO UPDATE SET " + REVIEW_UPDATES
)
NATURAL_UPSERT_SQL = (
    "INSERT INTO public.reviews (" + ", ".join(REVIEW_WRITE_COLUMNS) + ") SELECT "
    + ", ".join(f"s.{c}" for c in REVIEW_WRITE_COLUMNS)
    + " FROM foody_pre_eda_stage s WHERE s.source_review_id IS NULL AND "
    + "(SELECT COUNT(*) FROM public.reviews r WHERE r.source_review_id IS NOT NULL AND "
    + NATURAL_JOIN + ") <> 1 "
    + "ON CONFLICT (shop_id, user_pseudo_id, comment_datetime, md5(comment_text)) WHERE source_review_id IS NULL DO UPDATE SET "
    + REVIEW_UPDATES
)

# A receipt commits in the SAME transaction as the reviews. A retry after a
# lost connection/checkpoint can recover the original counts without rewriting.
BATCH_RECEIPT_SQL = """
CREATE TABLE IF NOT EXISTS public.foody_ingestion_batches (
    batch_id TEXT PRIMARY KEY,
    result JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
FIND_BATCH_SQL = "SELECT result FROM public.foody_ingestion_batches WHERE batch_id = %s"
SAVE_BATCH_SQL = "INSERT INTO public.foody_ingestion_batches (batch_id, result) VALUES (%s, %s)"


def upsert_counts(cursor, statement):
    cursor.execute(
        "WITH written AS (" + statement
        + " RETURNING (xmax = 0) AS inserted) SELECT "
        "COUNT(*) FILTER (WHERE inserted), COUNT(*) FILTER (WHERE NOT inserted) FROM written"
    )
    inserted, updated = cursor.fetchone()
    return int(inserted), int(updated)


def database_config():
    required = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError("Thiếu cấu hình .env: " + ", ".join(missing))
    return {"host": os.getenv("POSTGRES_HOST", "localhost"),
            "port": int(os.getenv("POSTGRES_PORT", "5432")),
            "dbname": os.environ["POSTGRES_DB"], "user": os.environ["POSTGRES_USER"],
            "password": os.environ["POSTGRES_PASSWORD"], "connect_timeout": 10}


def to_database_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value.item() if hasattr(value, "item") else value


def write_batches(cursor, statement, dataframe, columns, batch_size):
    batch = []
    for row in dataframe.loc[:, list(columns)].itertuples(index=False, name=None):
        batch.append(tuple(to_database_value(v) for v in row))
        if len(batch) == batch_size:
            execute_values(cursor, statement, batch, page_size=batch_size)
            batch = []
    if batch:
        execute_values(cursor, statement, batch, page_size=batch_size)


def verify_existing_schema(cursor):
    # Refuse to write into earlier feature schemas; never drop populated tables.
    for table, expected in TABLE_COLUMN_TYPES.items():
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                       (DB_SCHEMA, table))
        actual = {row[0] for row in cursor.fetchall()}
        if actual and actual != set(expected):
            raise ValueError(f"{DB_SCHEMA}.{table} đang dùng schema cũ/khác pre-EDA; cần migration trước khi nạp.")


def upsert_reviews(df, batch_size=1000, *, return_stats=False, batch_id=None):
    """Return the legacy processed count, or committed insert/update statistics.

    `updated` counts existing reviews touched by the upsert (even unchanged
    content). `skipped_existing` counts no-ID reviews already represented by
    one native ID. Both contribute to `duplicates_in_db`.
    """
    if batch_size < 1:
        raise ValueError("batch_size phải lớn hơn 0")
    if set(df.columns) != set(FLAT_COLUMNS):
        raise ValueError("Dữ liệu không khớp schema pre-EDA; chạy basic_pipeline trước")
    if df.empty:
        raise ValueError("Không có dữ liệu sạch để nạp")
    rows = [{k: to_database_value(v) for k, v in row.items()} for row in df.to_dict("records")]
    records = pd.DataFrame(deduplicate_records(rows), columns=FLAT_COLUMNS, dtype=object)
    shops = records.loc[:, list(SHOP_COLUMNS)].groupby("shop_id", sort=False).last().reset_index()
    users = records.loc[:, list(USER_COLUMNS)].drop_duplicates("user_pseudo_id", keep="last")
    connection = psycopg2.connect(**database_config())
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(291291)")
                verify_existing_schema(cursor)
                for statement in (*CREATE_TABLE_SQL, *INDEX_SQL, VIEW_SQL):
                    cursor.execute(statement)
                if batch_id is not None:
                    cursor.execute(BATCH_RECEIPT_SQL)
                    cursor.execute(FIND_BATCH_SQL, (batch_id,))
                    receipt = cursor.fetchone()
                    if receipt:
                        result = dict(receipt[0], replayed=True)
                        return result if return_stats else result["processed"]
                write_batches(cursor, SHOP_UPSERT_SQL, shops, SHOP_COLUMNS, batch_size)
                write_batches(cursor, USER_UPSERT_SQL, users, USER_COLUMNS, batch_size)
                cursor.execute(STAGE_SQL)
                write_batches(cursor, STAGE_INSERT_SQL, records, REVIEW_WRITE_COLUMNS, batch_size)
                cursor.execute(HASH_COLLISION_SQL)
                if cursor.fetchone()[0]:
                    raise ValueError("Xung đột hash khóa tự nhiên; transaction đã rollback, không gộp bình luận khác nhau.")
                cursor.execute(PROMOTE_SQL)
                promoted = cursor.rowcount
                native_inserted, native_updated = upsert_counts(cursor, NATIVE_UPSERT_SQL)
                natural_inserted, natural_updated = upsert_counts(cursor, NATURAL_UPSERT_SQL)
                inserted = native_inserted + natural_inserted
                updated = native_updated + natural_updated
                skipped = len(records) - inserted - updated
                result = {
                    "processed": len(records), "inserted": inserted, "updated": updated,
                    "skipped_existing": skipped, "duplicates_in_db": updated + skipped,
                    "promoted_legacy_ids": promoted, "replayed": False,
                    "db_written_at": datetime.now(timezone(timedelta(hours=7))).isoformat(),
                }
                if batch_id is not None:
                    cursor.execute(SAVE_BATCH_SQL, (batch_id, Json(result)))
    finally:
        connection.close()
    return result if return_stats else len(records)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Clean JSON → PostgreSQL pre-EDA")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data/processed/foody_clean.json")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"Không tìm thấy {args.input}. Chạy basic_pipeline.py trước.")
    print(f"Đọc dữ liệu sạch từ {args.input}...", flush=True)
    frame = load_clean_dataframe(args.input)
    print(f"Nạp {len(frame):,} review vào PostgreSQL...", flush=True)
    count = upsert_reviews(frame, args.batch_size)
    print(f"Đã xử lý {count:,} review; xem public.foody_reviews_flat trên Metabase.", flush=True)


if __name__ == "__main__":
    main()
