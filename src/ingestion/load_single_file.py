"""Load one raw Foody JSON file through MinIO, cleaning, and PostgreSQL.

Run from the project root:
    python -m src.ingestion.load_single_file --input data/raw/new_reviews.json

Accepts one review object or an array of reviews. Only the uploaded object is
read from MinIO. Existing cleaning and database upsert rules are reused.
Credentials and endpoints come from the project's .env file.
"""

import argparse
import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion.minio_upload import BUCKET_NAME, create_minio_client, upload_folder
from src.ingestion.postgres_load import database_config, to_database_value, upsert_reviews
from src.processing.foody.basic_pipeline import build_dataframe, save_json


def parse_records(payload):
    data = json.loads(payload.decode("utf-8-sig"))
    records = [data] if isinstance(data, dict) else data
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("JSON must contain a review object or an array of review objects.")
    if not records:
        raise ValueError("The input JSON contains no records.")
    return records


def load_single_file(input_path, bucket=None, batch_size=1000, output_dir=None):
    """Upload a snapshot, read it back, clean it, and upsert valid reviews.

    MinIO keeps the raw object if a later step fails. Retrying identical bytes
    on the same upload date reuses the object name; database identity rules
    prevent duplicate reviews. Folders use DD-MM-YYYY in Vietnam time.
    The returned processed count is NOT a count of newly inserted reviews.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero.")
    input_path = Path(input_path).expanduser().resolve()
    payload = input_path.read_bytes()
    parse_records(payload)  # Reject malformed/empty input before uploading.
    database_config()  # Validate required settings without opening a connection.
    bucket = bucket or os.getenv("MINIO_RAW_BUCKET", BUCKET_NAME)
    digest = hashlib.sha256(payload).hexdigest()
    object_name = f"{upload_folder()}/{digest}.json"
    run_dir = (Path(output_dir) if output_dir is not None else
               PROJECT_ROOT / "outputs" / "single_file_load" / digest).resolve()

    client = create_minio_client(bucket)
    print(f"[1/3] Uploading {input_path}", flush=True)
    client.put_object(bucket, object_name, BytesIO(payload), len(payload),
                      content_type="application/json")
    print(f"Raw object: {bucket}/{object_name}", flush=True)

    print("[2/3] Reading only this object from MinIO and running the pipeline...", flush=True)
    response = client.get_object(bucket, object_name)
    try:
        downloaded = response.read()
    finally:
        response.close()
        response.release_conn()
    if hashlib.sha256(downloaded).hexdigest() != digest:
        raise ValueError("The MinIO object differs from the uploaded snapshot; stopping.")
    records = parse_records(downloaded)
    rejected = []
    rejected_path = run_dir / "rejected.json"
    try:
        frame = build_dataframe(records, rejected)
    finally:
        save_json(rejected_path, rejected)
        print(f"Rejected records: {len(rejected):,}; report: {rejected_path}", flush=True)
    if frame.empty:
        raise ValueError("No valid reviews to upsert.")
    clean_path = run_dir / "clean.json"
    clean_rows = [{key: to_database_value(value) for key, value in row.items()}
                  for row in frame.to_dict("records")]
    save_json(clean_path, clean_rows)
    print(f"Raw: {len(records):,}; clean unique reviews: {len(frame):,}", flush=True)
    print(f"Clean data: {clean_path}", flush=True)

    print("[3/3] Upserting into PostgreSQL...", flush=True)
    processed = upsert_reviews(frame, batch_size=batch_size)
    print(f"Success: processed {processed:,} reviews (inserts or updates).", flush=True)
    return processed


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="One raw crawler JSON file")
    parser.add_argument("--bucket", default=os.getenv("MINIO_RAW_BUCKET", BUCKET_NAME))
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path, help="Directory for clean.json and rejected.json")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"Input file not found: {args.input}")
    if args.batch_size < 1:
        parser.error("--batch-size must be greater than zero")
    try:
        load_single_file(args.input, args.bucket, args.batch_size, args.output_dir)
    except Exception as error:
        print(f"Load failed: {error}", file=sys.stderr, flush=True)
        print("If upload succeeded, the raw object remains in MinIO. "
              "Fix the error and rerun the same command.", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
