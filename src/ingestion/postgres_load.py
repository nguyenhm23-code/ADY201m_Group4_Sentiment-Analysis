"""Chạy từ thư mục dự án: python -m src.ingestion.postgres_load."""

import os
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# Schema cố định tương ứng với DataFrame của basic_pipeline.
COLUMN_TYPES = {
    "review_id": "TEXT PRIMARY KEY",
    "shop_id": "BIGINT",
    "shop_name": "TEXT",
    "city": "TEXT",
    "region": "TEXT",
    "user_name": "TEXT",
    "rating": "DOUBLE PRECISION",
    "review_photo_count": "INTEGER",
    "shop_position_rating": "DOUBLE PRECISION",
    "shop_price_rating": "DOUBLE PRECISION",
    "shop_quality_rating": "DOUBLE PRECISION",
    "shop_service_rating": "DOUBLE PRECISION",
    "shop_atmosphere_rating": "DOUBLE PRECISION",
    "shop_foody_avg_rating": "DOUBLE PRECISION",
    "opening_time": "TEXT",
    "closing_time": "TEXT",
    "min_price_vnd": "INTEGER",
    "max_price_vnd": "INTEGER",
    "view_count": "BIGINT",
    "shop_total_review_count": "INTEGER",
    "shop_excellent_review_count": "INTEGER",
    "shop_good_review_count": "INTEGER",
    "shop_average_review_count": "INTEGER",
    "shop_bad_review_count": "INTEGER",
    "device": "TEXT",
    "comment_datetime": "TIMESTAMP",
    "comment_hour": "INTEGER",
    "day_of_week": "TEXT",
    "comment_text": "TEXT",
    "comment_length_words": "INTEGER",
    "comment_length_chars": "INTEGER",
    "emoji_count": "INTEGER",
    "teencode_score": "DOUBLE PRECISION",
    "spelling_error_rate": "DOUBLE PRECISION",
    "num_reviews_by_user": "BIGINT",
    "label_source": "TEXT",
    "source_platform": "TEXT",
    "crawl_timestamp": "TIMESTAMPTZ",
    "shop_review_count": "BIGINT",
    "shop_avg_rating": "DOUBLE PRECISION",
}
COLUMNS = tuple(COLUMN_TYPES)
NEW_COLUMNS = (
    "review_photo_count", "shop_position_rating", "shop_price_rating",
    "shop_quality_rating", "shop_service_rating", "shop_atmosphere_rating",
    "shop_foody_avg_rating", "opening_time", "closing_time",
    "min_price_vnd", "max_price_vnd", "view_count",
    "shop_total_review_count", "shop_excellent_review_count",
    "shop_good_review_count", "shop_average_review_count",
    "shop_bad_review_count",
)
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS public.foody_reviews ("
    + ", ".join(f"{name} {kind}" for name, kind in COLUMN_TYPES.items())
    + ", updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)

updates = []
for column in COLUMNS:
    if column == "review_id":
        continue
    if column in {"teencode_score", "spelling_error_rate"}:
        # Pipeline chưa tính hai điểm này: giữ điểm đã có trong database.
        updates.append(f"{column} = COALESCE(EXCLUDED.{column}, foody_reviews.{column})")
    else:
        updates.append(f"{column} = EXCLUDED.{column}")
UPSERT_SQL = (
    f"INSERT INTO public.foody_reviews ({', '.join(COLUMNS)}) VALUES %s "
    "ON CONFLICT (review_id) DO UPDATE SET "
    + ", ".join(updates)
    + ", updated_at = CURRENT_TIMESTAMP"
)


def database_config():
    required = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError("Thiếu cấu hình .env: " + ", ".join(missing))
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "connect_timeout": 10,
    }


def to_database_value(value):
    """Chuyển NaN/NaT thành SQL NULL và scalar pandas/numpy thành kiểu Python."""
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        return value.item()
    return value


def upsert_reviews(df: pd.DataFrame, batch_size: int = 1000) -> int:
    """Thêm/cập nhật dữ liệu trong một transaction; lỗi sẽ rollback toàn bộ.

    Trả về số review được gửi (bao gồm cả thêm mới và cập nhật).
    Không xóa những review không xuất hiện trong DataFrame.
    """
    if batch_size < 1:
        raise ValueError("batch_size phải lớn hơn 0")
    if df.empty:
        return 0
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError("DataFrame thiếu cột: " + ", ".join(sorted(missing)))
    if df["review_id"].isna().any() or df["review_id"].astype(str).str.strip().eq("").any():
        raise ValueError("review_id không được rỗng")
    records = df.loc[:, list(COLUMNS)].drop_duplicates(subset="review_id", keep="last")
    connection = psycopg2.connect(**database_config())
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(CREATE_TABLE_SQL)
                for column in NEW_COLUMNS:
                    cursor.execute(
                        f"ALTER TABLE public.foody_reviews ADD COLUMN IF NOT EXISTS {column} {COLUMN_TYPES[column]}"
                    )
                batch = []
                for row in records.itertuples(index=False, name=None):
                    batch.append(tuple(to_database_value(value) for value in row))
                    if len(batch) == batch_size:
                        execute_values(cursor, UPSERT_SQL, batch, page_size=batch_size)
                        batch = []
                if batch:
                    execute_values(cursor, UPSERT_SQL, batch, page_size=batch_size)
    finally:
        connection.close()
    return len(records)


def main():
    from src.processing.foody.basic_pipeline import build_clean_dataframe

    bucket = os.getenv("MINIO_RAW_BUCKET", "foody-raw-data")
    dataframe = build_clean_dataframe(bucket)
    count = upsert_reviews(dataframe)
    print(f"Đã thêm/cập nhật {count} review vào public.foody_reviews.")


if __name__ == "__main__":
    main()
