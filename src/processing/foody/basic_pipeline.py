import os
import re
import json
import hashlib
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from minio import Minio

# ---------------------------------------------------------
# 0. CẤU HÌNH KẾT NỐI MINIO
# ---------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[3]
env_path = PROJECT_ROOT / ".env"
load_dotenv(env_path)

client = Minio(
    os.getenv("MINIO_ENDPOINT", "localhost:9000"),
    access_key=os.getenv("MINIO_ROOT_USER", "minioadmin"),
    secret_key=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
    secure=False,
)

# ---------------------------------------------------------
# 1. TẢI DỮ LIỆU JSON GỘP TỪ MINIO (HOẶC LOCAL DỰ PHÒNG)
# ---------------------------------------------------------
def load_raw_records_from_minio(bucket: str) -> list[dict]:
    target_object = "2026-10-22/foody_merged_dataset.json"
    print(f"Đang đọc dữ liệu từ MinIO: {target_object}")

    try:
        response = client.get_object(bucket, target_object)
        data = json.loads(response.read().decode("utf-8"))
        response.close()
        response.release_conn()

        if isinstance(data, list):
            return data
        return [data]
    except Exception as e:
        print(f"Lỗi khi đọc file {target_object} từ MinIO: {e}")
        local_backup = PROJECT_ROOT / "data" / "raw" / "foody_merged_dataset.json"
        if local_backup.exists():
            print(f"Đang dùng file dự phòng local tại: {local_backup}")
            with open(local_backup, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                return data if isinstance(data, list) else [data]
        raise e

# ---------------------------------------------------------
# 2. CÁC HÀM XỬ LÝ & CHUẨN HÓA ĐẶC TRƯNG (FEATURES)
# ---------------------------------------------------------
def split_shop_id(raw_id: str) -> tuple[int, str]:
    raw_str = str(raw_id)
    match = re.search(r"(\d+)$", raw_str)
    shop_id = (
        int(match.group(1))
        if match
        else int(hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:16], 16) % (10**8)
    )
    shop_name = re.sub(r"-\d+$", "", raw_str)
    return shop_id, shop_name

def infer_platform(url: str) -> str:
    url_str = str(url).lower()
    if "foody" in url_str:
        return "Foody"
    if "shopeefood" in url_str:
        return "ShopeeFood"
    return "Foody"

def count_emoji(text: str) -> int:
    return sum(1 for ch in str(text) if ch in emoji.EMOJI_DATA)

def make_review_id(rec: dict, shop_id: int, user_name: str, dt_str: str) -> str:
    existing_id = rec.get("review_id") or rec.get("id")
    if existing_id:
        return str(existing_id)

    comment = str(rec.get("Bình Luận") or rec.get("comment") or rec.get("comment_text") or "")[:50]
    raw = f"{shop_id}_{user_name}_{dt_str}_{comment}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def remove_vietnamese_accent(text: str) -> str:
    """Loại bỏ dấu tiếng Việt (kể cả chữ Đ), chuyển về chữ thường và chuẩn hóa khoảng trắng"""
    if not text:
        return ""
    # Chuyển đổi thủ công chữ Đ/đ vì unicode decomposing không tách được đ thành d + dấu
    s = str(text).replace("Đ", "D").replace("đ", "d")
    nfkd_form = unicodedata.normalize('NFKD', s)
    only_ascii = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    clean_str = only_ascii.lower().replace("-", " ").strip()
    return " ".join(clean_str.split())

def standardize_city_name(city: str) -> str:
    """Quy đổi mọi biến thể về một mã slug chuẩn duy nhất"""
    clean = remove_vietnamese_accent(city)
    if not clean or clean == "unknown":
        return "Unknown"

    if any(k in clean for k in ["ho chi minh", "tp hcm", "hcm", "sai gon"]):
        return "ho-chi-minh"
    if any(k in clean for k in ["ha noi", "hanoi"]):
        return "ha-noi"
    if any(k in clean for k in ["da nang", "danang"]):
        return "da-nang"
    if any(k in clean for k in ["can tho", "cantho"]):
        return "can-tho"
    if any(k in clean for k in ["hai phong", "haiphong"]):
        return "hai-phong"
    if any(k in clean for k in ["vung tau", "vungtau"]):
        return "vung-tau"
    if any(k in clean for k in ["quang ninh"]):
        return "quang-ninh"
    if any(k in clean for k in ["khanh hoa", "nha trang"]):
        return "khanh-hoa"
    if any(k in clean for k in ["bac ninh"]):
        return "bac-ninh"
    if any(k in clean for k in ["binh thuan", "phan thiet"]):
        return "binh-thuan"
    if any(k in clean for k in ["nghe an", "vinh"]):
        return "nghe-an"
    if any(k in clean for k in ["hue", "thua thien"]):
        return "thua-thien-hue"

    return clean.replace(" ", "-")

def infer_region(city_slug: str) -> str:
    north = {"ha-noi", "hai-phong", "quang-ninh", "bac-ninh", "thai-nguyen", "nam-dinh", "ninh-binh"}
    south = {"ho-chi-minh", "can-tho", "vung-tau", "binh-duong", "dong-nai", "an-giang"}
    central = {"da-nang", "thua-thien-hue", "khanh-hoa", "nha-trang", "quang-nam", "nghe-an", "ha-tinh", "binh-thuan"}

    if city_slug in north:
        return "Bac"
    if city_slug in south:
        return "Nam"
    if city_slug in central:
        return "Trung"
    return "Khac"

# ---------------------------------------------------------
# 3. CHUẨN HÓA RECORD RAW THEO TARGET SCHEMA
# ---------------------------------------------------------
def normalize_record(rec: dict) -> dict:
    raw_shop_id = rec.get("ID Quán") or rec.get("shop_id") or ""
    shop_id, shop_name = split_shop_id(raw_shop_id)

    if rec.get("shop_name"):
        shop_name = rec.get("shop_name")

    url = rec.get("URL Quán") or rec.get("url") or rec.get("shop_url") or ""
    source_platform = infer_platform(url)

    comment_text = str(rec.get("Bình Luận") or rec.get("comment") or rec.get("comment_text") or "")
    dt_raw = str(rec.get("Ngày Giờ") or rec.get("datetime") or rec.get("comment_datetime") or "")

    comment_dt = pd.NaT
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            comment_dt = datetime.strptime(dt_raw, fmt)
            break
        except (ValueError, TypeError):
            continue

    user_name = str(rec.get("Tên User") or rec.get("user_name") or rec.get("user_pseudo_id") or "Anonymous")
    
    # Chuẩn hóa tên thành phố và gán phân vùng chuẩn xác
    raw_city = str(rec.get("Thành Phố") or rec.get("city") or "Unknown")
    city_val = standardize_city_name(raw_city)
    region_val = infer_region(city_val)

    return {
        "review_id": make_review_id(rec, shop_id, user_name, dt_raw),
        "shop_id": shop_id,
        "shop_name": shop_name,
        "city": city_val,
        "region": region_val,
        "user_name": user_name,
        "rating": pd.to_numeric(rec.get("rating") or rec.get("Điểm Đánh Giá"), errors="coerce"),
        
        # Ánh xạ thuộc tính cào sâu linh hoạt (bắt cả key mới và key cũ)
        "review_photo_count": pd.to_numeric(rec.get("num_photos") or rec.get("Số Ảnh Bình Luận"), errors="coerce"),
        "shop_position_rating": pd.to_numeric(rec.get("location_score") or rec.get("Điểm Vị Trí"), errors="coerce"),
        "shop_price_rating": pd.to_numeric(rec.get("price_score") or rec.get("Điểm Giá Cả"), errors="coerce"),
        "shop_quality_rating": pd.to_numeric(rec.get("quality_score") or rec.get("Điểm Chất Lượng"), errors="coerce"),
        "shop_service_rating": pd.to_numeric(rec.get("service_score") or rec.get("Điểm Phục Vụ"), errors="coerce"),
        "shop_atmosphere_rating": pd.to_numeric(rec.get("space_score") or rec.get("Điểm Không Gian"), errors="coerce"),
        "shop_foody_avg_rating": pd.to_numeric(rec.get("shop_avg_rating") or rec.get("Điểm Trung Bình Quán"), errors="coerce"),
        
        "opening_time": rec.get("opening_hour") or rec.get("Giờ Mở Cửa"),
        "closing_time": rec.get("closing_hour") or rec.get("Giờ Đóng Cửa"),
        
        "min_price_vnd": pd.to_numeric(rec.get("price_min") or rec.get("Giá Thấp Nhất"), errors="coerce"),
        "max_price_vnd": pd.to_numeric(rec.get("price_max") or rec.get("Giá Cao Nhất"), errors="coerce"),
        "view_count": pd.to_numeric(rec.get("views") or rec.get("Lượt Xem"), errors="coerce"),
        
        "shop_total_review_count": pd.to_numeric(rec.get("shop_review_count") or rec.get("Tổng Số Bình Luận"), errors="coerce"),
        "shop_excellent_review_count": pd.to_numeric(rec.get("excellent_count") or rec.get("Số Bình Luận Tuyệt Vời"), errors="coerce"),
        "shop_good_review_count": pd.to_numeric(rec.get("good_count") or rec.get("Số Bình Luận Khá Tốt"), errors="coerce"),
        "shop_average_review_count": pd.to_numeric(rec.get("average_count") or rec.get("Số Bình Luận Trung Bình"), errors="coerce"),
        "shop_bad_review_count": pd.to_numeric(rec.get("poor_count") or rec.get("Số Bình Luận Kém"), errors="coerce"),
        
        "device": str(rec.get("device") or rec.get("Thiết Bị") or "Unknown"),
        "comment_datetime": comment_dt,
        "comment_hour": comment_dt.hour if pd.notna(comment_dt) else -1,
        "day_of_week": comment_dt.strftime("%A") if pd.notna(comment_dt) else "Unknown",
        "comment_text": comment_text,
        "comment_length_words": len(comment_text.split()),
        "comment_length_chars": len(comment_text),
        "emoji_count": count_emoji(comment_text),
        "teencode_score": None,
        "spelling_error_rate": None,
        "num_reviews_by_user": None,
        "label_source": "human",
        "source_platform": source_platform,
        "crawl_timestamp": datetime.now(timezone.utc),
    }
    for region, cities in regions.items():
        if normalized in cities.split():
            return region
    return None

# ---------------------------------------------------------
# 4. PIPELINE CHÍNH: RAW -> DEDUP -> FILLNA -> DATAFRAME
# ---------------------------------------------------------
def build_clean_dataframe(bucket: str = "foody-raw-data") -> pd.DataFrame:
    raw_records = load_raw_records_from_minio(bucket)
    if not raw_records:
        print("Không tìm thấy dữ liệu từ MinIO!")
        return pd.DataFrame()

    print(f"Đã đọc {len(raw_records):,} bản ghi thô. Đang chuẩn hóa...")
    normalized = [normalize_record(r) for r in raw_records]
    df = pd.DataFrame(normalized)

    before = len(df)
    df = df.drop_duplicates(subset="review_id", keep="last")
    print(f"Đã loại bỏ {before - len(df):,} bản ghi trùng lặp. Còn lại {len(df):,} unique reviews.")

    df["num_reviews_by_user"] = df.groupby("user_name")["review_id"].transform("count")

    shop_stats = (
        df.groupby("shop_id")["rating"]
        .agg(shop_review_count="count", shop_avg_rating="mean")
        .reset_index()
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

    df["rating"] = df["rating"].fillna(df["rating"].median())
    df["device"] = df["device"].fillna("Unknown")
    df["city"] = df["city"].fillna("Unknown")
    df["comment_text"] = df["comment_text"].fillna("")
    df["comment_hour"] = df["comment_hour"].fillna(-1).astype(int)
    df["day_of_week"] = df["day_of_week"].fillna("Unknown")

    return df

if __name__ == "__main__":
    final_df = build_clean_dataframe("foody-raw-data")
    print(f"\nDataFrame kích thước: {final_df.shape}")
