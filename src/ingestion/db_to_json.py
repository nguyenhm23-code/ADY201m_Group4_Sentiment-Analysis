"""Export all reviews, shop details, and users from PostgreSQL to one JSON array.

Run from the project root:
    python -m src.ingestion.db_to_json
    python -m src.ingestion.db_to_json --output data/processed/foody_db.json

Uses public.foody_reviews_flat and the POSTGRES_* settings in .env. Reads in
chunks without loading the entire database into Python memory. Each run replaces
the output with a fresh snapshot; an unsuccessful export preserves the old file.
"""

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion.postgres_load import database_config

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "processed" / "foody_db.json"
EXPORT_SQL = """
SELECT row_to_json(review)::text
FROM public.foody_reviews_flat AS review
ORDER BY review.review_id
"""
VIETNAM_TIME = timezone(timedelta(hours=7))


def export_db_to_json(output_path=DEFAULT_OUTPUT, batch_size=2000):
    """Return the number of exported reviews; an empty database produces [].

    PostgreSQL serializes dates, times, nulls, and numeric values directly to
    JSON, preserving BIGINT IDs without a floating-point conversion. The named
    cursor streams a single SELECT snapshot, including while crawling continues.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero")
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"[{datetime.now(VIETNAM_TIME).isoformat()}] Exporting PostgreSQL → {output_path}", flush=True)
    connection = psycopg2.connect(**database_config())
    temporary = None
    count = 0
    try:
        connection.set_session(readonly=True)
        with connection:
            with connection.cursor(name="foody_json_export") as cursor:
                cursor.itersize = batch_size
                cursor.execute(EXPORT_SQL)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", delete=False,
                    dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp",
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write("[")
                    for (row_json,) in cursor:
                        stream.write(",\n" if count else "\n")
                        stream.write("  " + row_json)
                        count += 1
                    stream.write("\n]\n" if count else "]\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        # Replace only once the complete snapshot is on disk and the read
        # transaction has ended successfully. Works with existing files on Windows.
        temporary.replace(output_path)
    finally:
        try:
            connection.close()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    print(f"[{datetime.now(VIETNAM_TIME).isoformat()}] Exported {count:,} rows "
          f"in {time.monotonic() - started:.2f}s: {output_path}", flush=True)
    return count


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Destination JSON file (default: data/processed/foody_db.json)")
    parser.add_argument("--batch-size", type=int, default=2000,
                        help="Rows fetched per network round trip (default: 2000)")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be greater than zero")
    try:
        export_db_to_json(args.output, args.batch_size)
    except psycopg2.errors.UndefinedTable:
        print("Export failed: public.foody_reviews_flat does not exist. "
              "Run the pipeline/database loader first.", file=sys.stderr, flush=True)
        return 1
    except Exception as error:
        print(f"Export failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
