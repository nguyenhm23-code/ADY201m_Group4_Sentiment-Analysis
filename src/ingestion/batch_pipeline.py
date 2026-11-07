"""Process exactly one uploaded crawler batch using the shared Foody pipeline."""

import hashlib
import json
from pathlib import Path

from src.ingestion.postgres_load import to_database_value, upsert_reviews
from src.processing.foody.basic_pipeline import build_dataframe, save_json


def process_minio_batch(client, bucket, object_name, local_path, output_dir):
    """Read back the raw object, quarantine invalid rows, and commit with a receipt.

    Exceptions intentionally propagate: the uploader must keep its checkpoint
    until the database and completion log have both been written successfully.
    """
    response = client.get_object(bucket, object_name)
    try:
        payload = response.read()
    finally:
        response.close()
        response.release_conn()
    expected = Path(local_path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != hashlib.sha256(expected).hexdigest():
        raise ValueError("MinIO batch differs from the local checkpoint payload")
    records = json.loads(payload.decode("utf-8"))
    output_dir = Path(output_dir)
    rejected = []
    try:
        frame = build_dataframe(records, rejected)
    finally:
        save_json(output_dir / "rejected.json", rejected)
    clean = [{key: to_database_value(value) for key, value in row.items()}
             for row in frame.to_dict("records")]
    save_json(output_dir / "clean.json", clean)
    # Include the digest so receipts cannot silently acknowledge another payload.
    stats = upsert_reviews(frame, return_stats=True,
                           batch_id=f"{bucket}/{object_name}#{digest}")
    return {
        "raw_rows": len(records), "rejected_rows": len(rejected),
        "duplicates_in_batch": len(records) - len(rejected) - len(frame),
        "clean_rows": len(frame), **stats,
        "clean_path": str(output_dir / "clean.json"),
        "rejected_path": str(output_dir / "rejected.json"),
    }
