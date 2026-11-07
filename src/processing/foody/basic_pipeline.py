"""MinIO JSON -> clean pre-EDA source data matching shops/users/reviews.

Run from the project root: python -m src.processing.foody.basic_pipeline.
The database loader splits this frame into the three normalized tables.
"""

import json
import os
import re
import unicodedata
import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from minio import Minio

# Support both python basic_pipeline.py and python -m src.processing.foody.basic_pipeline.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.processing.foody.schema import FLAT_COLUMNS, NATURAL_KEY, RAW_FIELD_MAP, SHOP_COLUMNS

load_dotenv(PROJECT_ROOT / ".env")
VIETNAM_TIME = timezone(timedelta(hours=7))
BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


def minio_client():
    # Construct lazily: importing/testing normalization needs no MinIO connection.
    return Minio(
        os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.getenv("MINIO_ROOT_USER"),
        secret_key=os.getenv("MINIO_ROOT_PASSWORD"),
        secure=False,
    )


def load_raw_records_from_minio(bucket: str) -> list[dict]:
    client = minio_client()
    records = []
    objects = sorted(
        client.list_objects(bucket, recursive=True),
        key=lambda obj: (obj.last_modified, obj.object_name),
    )
    for obj in objects:
        if not obj.object_name.lower().endswith(".json"):
            continue
        response = client.get_object(bucket, obj.object_name)
        try:
            data = json.loads(response.read().decode("utf-8-sig"))
            rows = [data] if isinstance(data, dict) else data
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{obj.object_name}: JSON phải chứa object hoặc mảng object")
            records.extend(rows)
        finally:
            response.close()
            response.release_conn()
    return records


def optional_text(value):
    if value is None or pd.isna(value):
        return None
    return str(value).strip() or None


def parse_integer(value, minimum=0, maximum=2**31 - 1):
    """Parse without a float intermediary, preserving all BIGINT digits."""
    try:
        if isinstance(value, bool):
            return None
        number = Decimal(str(value).strip())
        if not number.is_finite() or number != number.to_integral_value():
            return None
        return int(number) if minimum <= number <= maximum else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_score(value):
    try:
        number = float(str(value).strip().replace(",", "."))
        return number if 0 <= number <= 10 else None
    except (TypeError, ValueError):
        return None


def parse_time(value):
    value = optional_text(value)
    if value == "24:00":
        value = "00:00"
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except (ValueError, TypeError):
            pass
    return None


def parse_datetime(value):
    value = optional_text(value)
    # Older crawler exports include a trailing comma after timestamps.
    if value:
        value = value.rstrip(", ")
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(VIETNAM_TIME).replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError, AttributeError):
        return None


def infer_platform(url):
    host = urlparse(url or "").hostname or ""
    for domain, name in (("foody.vn", "Foody"), ("shopeefood.vn", "ShopeeFood")):
        if host == domain or host.endswith("." + domain):
            return name
    return "Unknown"


def infer_region(city):
    normalized = unicodedata.normalize("NFD", city or "").lower().replace("đ", "d")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[\s_]+", "-", normalized.strip())
    regions = {
        "Bac": "ha-noi hai-phong quang-ninh bac-ninh bac-giang hai-duong hung-yen ha-nam nam-dinh ninh-binh thai-binh vinh-phuc phu-tho thai-nguyen bac-kan cao-bang lang-son tuyen-quang ha-giang lao-cai yen-bai son-la dien-bien lai-chau hoa-binh",
        "Trung": "thanh-hoa nghe-an ha-tinh quang-binh quang-tri hue thua-thien-hue da-nang quang-nam quang-ngai binh-dinh phu-yen khanh-hoa ninh-thuan binh-thuan kon-tum gia-lai dak-lak dak-nong lam-dong",
        "Nam": "ho-chi-minh tp-ho-chi-minh sai-gon saigon hcm can-tho vung-tau ba-ria-vung-tau binh-duong binh-phuoc dong-nai tay-ninh long-an tien-giang ben-tre tra-vinh vinh-long dong-thap an-giang kien-giang hau-giang soc-trang bac-lieu ca-mau",
    }
    for region, cities in regions.items():
        if normalized in cities.split():
            return region
    return None


def normalize_record(rec: dict, timestamp=None) -> dict:
    unknown = set(rec) - set(RAW_FIELD_MAP)
    if unknown:
        raise ValueError("Unmapped crawl fields: " + ", ".join(sorted(unknown)))
    timestamp = timestamp or datetime.now(VIETNAM_TIME).replace(tzinfo=None)
    shop_id = optional_text(rec.get("ID Quán"))
    city = optional_text(rec.get("Thành Phố"))
    user_name = rec.get("Tên User")
    if not shop_id or not city or not isinstance(user_name, str) or not user_name.strip():
        raise ValueError("Thiếu slug quán, thành phố hoặc tên user")
    rating = parse_score(rec.get("Điểm Đánh Giá"))
    comment_dt = parse_datetime(rec.get("Ngày Giờ"))
    comment_text = rec.get("Bình Luận")
    if rating is None or comment_dt is None or not isinstance(comment_text, str):
        raise ValueError("Thiếu/sai rating, ngày giờ hoặc bình luận")
    source_raw = rec.get("ID Bình Luận")
    source_id = parse_integer(source_raw, 1, BIGINT_MAX) if source_raw is not None else None
    if source_raw not in (None, "") and source_id is None:
        raise ValueError("ID Bình Luận không phải BIGINT dương")
    result = dict.fromkeys(FLAT_COLUMNS)
    result.update(
        source_review_id=source_id, shop_id=shop_id,
        shop_name=optional_text(rec.get("Tên Quán")) or shop_id.replace("-", " ").title(),
        city=city, region=infer_region(city), shop_url=optional_text(rec.get("URL Quán")),
        user_name=user_name, user_pseudo_id=user_name.strip().lower(), first_seen_at=timestamp,
        rating=rating, comment_datetime=comment_dt, comment_text=comment_text,
        device=optional_text(rec.get("Thiết Bị")), source_platform=infer_platform(rec.get("URL Quán")),
        crawled_at=timestamp, last_crawled_at=timestamp,
    )
    if result["source_platform"] == "Unknown":
        raise ValueError("URL Quán không thuộc Foody/ShopeeFood")
    for source, column in RAW_FIELD_MAP.items():
        value = rec.get(source)
        if value is None or value == "":
            continue
        if column in {"opening_hour", "closing_hour"}:
            parsed = parse_time(value)
        elif column in {"price_min", "price_max", "views", "shop_review_count", "excellent_count",
                        "good_count", "average_count", "poor_count", "num_photos"}:
            parsed = parse_integer(value)
        elif column in {"shop_avg_rating", "location_score", "price_score", "quality_score", "service_score", "space_score"}:
            parsed = parse_score(value)
        else:
            continue
        if parsed is None:
            raise ValueError("Giá trị crawl không hợp lệ: " + source)
        result[column] = parsed
    if result["price_min"] is not None and result["price_max"] is not None and result["price_max"] < result["price_min"]:
        raise ValueError("Giá Cao Nhất nhỏ hơn Giá Thấp Nhất")
    return result


def deduplicate_records(records):
    """IDs win; natural keys deduplicate unidentified rows only.

    A no-ID record matching exactly one native ID is the same review. Ambiguous
    matches to multiple IDs remain separate rather than guessing an identity.
    """
    native, unidentified = {}, {}
    for row in records:
        key = row["source_review_id"]
        target = native if key is not None else unidentified
        key = key if key is not None else tuple(row[c] for c in NATURAL_KEY)
        if key in target:
            # Missing optional fields in older crawls must not erase richer data.
            target[key] = {c: row[c] if row[c] is not None else target[key][c] for c in FLAT_COLUMNS}
        else:
            target[key] = row.copy()
    matches = {}
    for sid, row in native.items():
        matches.setdefault(tuple(row[c] for c in NATURAL_KEY), []).append(sid)
    for key, row in list(unidentified.items()):
        ids = matches.get(key, [])
        if len(ids) == 1:
            current = native[ids[0]]
            native[ids[0]] = {c: current[c] if current[c] is not None else row[c] for c in FLAT_COLUMNS}
            del unidentified[key]
    return list(native.values()) + list(unidentified.values())


def build_dataframe(raw_records: list[dict], rejected=None) -> pd.DataFrame:
    timestamp = datetime.now(VIETNAM_TIME).replace(tzinfo=None)
    normalized, errors = [], rejected if rejected is not None else []
    for number, record in enumerate(raw_records, 1):
        try:
            normalized.append(normalize_record(record, timestamp))
        except ValueError as error:
            errors.append({"record_number": number, "reason": str(error), "raw_record": record})
    if raw_records and not normalized:
        raise ValueError("Không có record hợp lệ; kiểm tra report record bị cách ly")
    frame = pd.DataFrame(normalized, columns=FLAT_COLUMNS, dtype=object)
    if frame.empty:
        return frame
    shops = frame.loc[:, list(SHOP_COLUMNS)].groupby("shop_id", sort=False).last().reset_index()
    frame = pd.DataFrame(deduplicate_records(normalized), columns=FLAT_COLUMNS, dtype=object)
    frame = frame.drop(columns=list(SHOP_COLUMNS[1:])).merge(shops, on="shop_id", how="left")
    return frame.loc[:, list(FLAT_COLUMNS)]


def build_clean_dataframe(bucket, rejected=None) -> pd.DataFrame:
    return build_dataframe(load_raw_records_from_minio(bucket), rejected)


def save_json(path, records):
    from datetime import date, time
    def encode(value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        raise TypeError(type(value).__name__)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=encode), encoding="utf-8")
    temporary.replace(path)


def load_clean_dataframe(path):
    from datetime import time
    rows = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("Clean JSON phải chứa mảng records")
    for row in rows:
        if set(row) != set(FLAT_COLUMNS):
            raise ValueError("Clean JSON không khớp schema pre-EDA; chạy lại basic_pipeline")
        for column in ("opening_hour", "closing_hour"):
            if row[column] is not None:
                row[column] = time.fromisoformat(row[column])
        for column in ("comment_datetime", "crawled_at", "last_crawled_at", "first_seen_at"):
            row[column] = datetime.fromisoformat(row[column])
    return pd.DataFrame(rows, columns=FLAT_COLUMNS, dtype=object)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="MinIO → lọc sạch pre-EDA → JSON")
    parser.add_argument("--bucket", default=os.getenv("MINIO_RAW_BUCKET", "foody-raw-data"))
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/processed/foody_clean.json")
    args = parser.parse_args()
    print(f"Đọc JSON từ MinIO bucket {args.bucket}...", flush=True)
    raw = load_raw_records_from_minio(args.bucket)
    print(f"Chuẩn hóa và lọc trùng {len(raw):,} record...", flush=True)
    rejected = []
    report = PROJECT_ROOT / "outputs/pre_eda_rejected.json"
    try:
        frame = build_dataframe(raw, rejected)
    finally:
        save_json(report, rejected)
    # pandas missing values must become JSON null, never NaN.
    rows = [{k: None if pd.isna(v) else v for k, v in row.items()} for row in frame.to_dict("records")]
    save_json(args.output, rows)
    print(f"Đã lưu {len(frame):,} review sạch: {args.output}", flush=True)
    print(f"Cách ly {len(rejected):,} record lỗi: {report}", flush=True)


if __name__ == "__main__":
    main()
