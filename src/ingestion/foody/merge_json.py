"""Gộp JSON dữ liệu Foody, giữ nguyên các record và file nguồn.

Chạy từ thư mục gốc: python src/ingestion/foody/merge_json.py
"""

import json
import argparse
import sys
import tempfile
from pathlib import Path


INPUT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = INPUT_DIR.parents[2] / "data" / "raw" / "foody_merged_dataset.json"


def merge_json_files(input_dir=INPUT_DIR, output_file=OUTPUT_FILE, source_pattern="*.json"):
    input_dir = Path(input_dir).resolve()
    output_file = Path(output_file).resolve()

    # Chỉ đọc JSON ngay trong thư mục, không đọc chrome_profile/checkpoints.
    source_files = sorted(input_dir.glob(source_pattern))
    if not source_files:
        raise FileNotFoundError("Không tìm thấy file JSON trong {}".format(input_dir))
    if output_file in source_files:
        raise ValueError("File đầu ra phải khác các file JSON nguồn")

    merged_records = []
    for source in source_files:
        try:
            with source.open("r", encoding="utf-8-sig") as stream:
                data = json.load(stream)
        except (ValueError, UnicodeError) as error:
            raise ValueError("File JSON không hợp lệ: {}".format(source.name)) from error

        # Chấp nhận một mảng records hoặc một object đơn lẻ.
        records = [data] if isinstance(data, dict) else data
        if not isinstance(records, list) or any(
            not isinstance(record, dict) for record in records
        ):
            raise ValueError("{} phải chứa object hoặc mảng object".format(source.name))

        merged_records.extend(records)
        print("{}: {:,} records".format(source.name, len(records)))

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = None
    try:
        # Ghi file tạm trước để tránh để lại đầu ra dở dang nếu ghi thất bại.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(output_file.parent),
            prefix=output_file.stem + "_", suffix=".tmp", delete=False
        ) as stream:
            temporary_file = Path(stream.name)
            json.dump(merged_records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary_file.replace(output_file)
    finally:
        if temporary_file is not None and temporary_file.exists():
            temporary_file.unlink()

    print("\nĐã gộp {} file, tổng {:,} records.".format(len(source_files), len(merged_records)))
    print("File đầu ra: {}".format(output_file))
    return output_file, len(merged_records)


if __name__ == "__main__":
    # Console Windows có thể dùng bảng mã không hỗ trợ tiếng Việt.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR, help="Thư mục chứa JSON nguồn")
    parser.add_argument("--pattern", default="*.json", help="Mẫu tên file JSON nguồn")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE, help="File JSON gộp")
    args = parser.parse_args()
    merge_json_files(input_dir=args.input_dir, output_file=args.output, source_pattern=args.pattern)
