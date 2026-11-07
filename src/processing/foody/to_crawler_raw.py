"""Convert the English Foody export to the reference crawler's raw field names.

Run from the project root:
    python -m src.processing.foody.to_crawler_raw

This only converts local files. It preserves all reviews and raw text, does not
deduplicate, and uses JSON null for unavailable source data except missing
usernames, which become "unknown". Database validation still applies.
Input files are never overwritten.
"""

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DIR = PROJECT_ROOT / "data/raw/18-10-2026"

# None means this export has no equivalent source field. Never substitute
# synthetic review hashes for native IDs or infer a username from review_id.
FIELD_SOURCES = {
    "Thành Phố": "city_raw",
    "ID Quán": "native_restaurant_id",
    "URL Quán": "restaurant_url",
    "ID Bình Luận": "native_review_id",
    "Tên User": "user_name",
    "Điểm Đánh Giá": "rating_raw",
    "Thiết Bị": "device",
    "Ngày Giờ": "published_at_raw",
    "Bình Luận": "text_raw",
    "Số Ảnh Bình Luận": None,
    "Điểm Vị Trí": None,
    "Điểm Giá Cả": None,
    "Điểm Chất Lượng": None,
    "Điểm Phục Vụ": None,
    "Điểm Không Gian": None,
    "Điểm Trung Bình Quán": "restaurant_rating",
    "Giờ Mở Cửa": None,
    "Giờ Đóng Cửa": None,
    "Giá Thấp Nhất": None,
    "Giá Cao Nhất": None,
    "Lượt Xem": None,
    "Tổng Số Bình Luận": None,
    "Số Bình Luận Tuyệt Vời": None,
    "Số Bình Luận Khá Tốt": None,
    "Số Bình Luận Trung Bình": None,
    "Số Bình Luận Kém": None,
}


def read_records(path):
    records = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError(f"Expected a JSON array of objects: {path}")
    return records


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def convert_file(input_path, reference_path, output_path, report_path):
    input_path, reference_path, output_path, report_path = (
        Path(path).expanduser().resolve()
        for path in (input_path, reference_path, output_path, report_path)
    )
    if {input_path, reference_path} & {output_path, report_path} or output_path == report_path:
        raise ValueError("Output/report paths must differ from both source files and each other.")
    reference = read_records(reference_path)
    if not reference:
        raise ValueError("The reference file is empty; cannot determine its fields.")
    fields = tuple(reference[0])
    if set(fields) != set(FIELD_SOURCES):
        raise ValueError("The reference must use the supported 26-field crawler schema.")
    if any(set(row) != set(fields) for row in reference):
        raise ValueError("The reference file contains inconsistent field sets.")
    records = read_records(input_path)
    converted = []
    unknown_usernames_filled = 0
    for number, row in enumerate(records, 1):
        if row.get("source") != "foody" or row.get("schema_version") != "1.2.0":
            raise ValueError(f"Record {number}: expected Foody export schema 1.2.0.")
        if row.get("rating_scale") != 10:
            raise ValueError(f"Record {number}: expected ratings on the 0-10 scale.")
        converted_row = {field: row.get(FIELD_SOURCES[field])
                         if FIELD_SOURCES[field] else None for field in fields}
        username = converted_row["Tên User"]
        if username is None or (isinstance(username, str) and not username.strip()):
            converted_row["Tên User"] = "unknown"
            unknown_usernames_filled += 1
        converted.append(converted_row)

    missing = {field: sum(row[field] is None or row[field] == "" for row in converted)
               for field in fields}
    required = ("Thành Phố", "ID Quán", "URL Quán", "Tên User",
                "Điểm Đánh Giá", "Ngày Giờ", "Bình Luận")
    report = {
        "converted_at_utc": datetime.now(timezone.utc).isoformat(),
        "input": str(input_path), "reference": str(reference_path), "output": str(output_path),
        "input_records": len(records), "output_records": len(converted),
        "output_field_count": len(fields), "field_sources": FIELD_SOURCES,
        "unknown_usernames_filled": unknown_usernames_filled,
        "missing_value_counts": missing,
        "missing_required_field_counts": {field: missing[field] for field in required},
        "discarded_source_fields": sorted(set().union(*(set(row) for row in records))
                                          - {field for field in FIELD_SOURCES.values() if field}),
        "notes": ["All source records retained; no deduplication or semantic cleaning.",
                  "Missing or blank usernames become 'unknown' as requested; "
                  "other unavailable values remain null.",
                  "text_raw, rating_raw and published_at_raw are preserved as supplied.",
                  "Matching fields does not guarantee acceptance by the database pipeline; "
                  "it still requires valid field values. 'unknown' is a shared placeholder, "
                  "not an identified user."],
    }
    write_json(output_path, converted)
    write_json(report_path, report)
    print(f"Converted {len(converted):,} records with {len(fields)} fields each.")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    print(f"Missing usernames filled with 'unknown': {unknown_usernames_filled:,}")
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_DIR / "foody.json")
    parser.add_argument("--reference", type=Path,
                        default=DEFAULT_DIR / "foody_merged_dataset_mienBac.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_DIR / "foody_clean.json")
    parser.add_argument("--report", type=Path,
                        default=PROJECT_ROOT / "outputs/foody_raw_conversion_report.json")
    args = parser.parse_args()
    try:
        convert_file(args.input, args.reference, args.output, args.report)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Conversion failed: {error}\n")


if __name__ == "__main__":
    main()
