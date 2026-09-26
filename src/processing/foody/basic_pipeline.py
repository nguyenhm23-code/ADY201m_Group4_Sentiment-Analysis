import json
import re
import hashlib
from datetime import datetime, timezone
import os
from dotenv import load_dotenv

import pandas as pd
from minio import Minio
import emoji

# ---------------------------------------------------------
# 0. Cấu hình kết nối MinIO
# ---------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 2. LOAD BIẾN MÔI TRƯỜNG TỪ FILE .env
env_path = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(env_path)
client = Minio(
        "localhost:9000",
        access_key=os.getenv("MINIO_ROOT_USER"),     # Tự động lấy từ .env
        secret_key=os.getenv("MINIO_ROOT_PASSWORD"), # Tự động lấy từ .env
        secure=False
    )

# ---------------------------------------------------------
# 1. Kéo toàn bộ file JSON thô từ MinIO về
# ---------------------------------------------------------
def load_raw_records_from_minio(bucket: str) -> list[dict]:
    records = []
    objects = client.list_objects(bucket, recursive=True)
    for obj in objects:
        if not obj.object_name.endswith(".json"):
            continue
        response = client.get_object(bucket, obj.object_name)
        try:
            data = json.loads(response.read().decode("utf-8"))
            if isinstance(data, list):
                records.extend(data)
            else:
                records.append(data)
        finally:
            response.close()
            response.release_conn()
    return records


# ---------------------------------------------------------
# 2. Các hàm trích xuất / biến đổi feature
# ---------------------------------------------------------
def split_shop_id(raw_id: str) -> tuple[int, str]:
    """
    'com-chien-gion-long-bon-hoang-12345' -> (12345, 'com-chien-gion-long-bon-hoang')
    Giả định số shop_id nằm ở cuối chuỗi ID Quán, phân tách bằng dấu '-'.
    Điều chỉnh regex nếu format thực tế khác.
    """
    match = re.search(r"(\d+)$", raw_id)
    shop_id = int(match.group(1)) if match else abs(hash(raw_id)) % (10**8)
    shop_name = re.sub(r"-\d+$", "", raw_id)
    return shop_id, shop_name


def infer_platform(url: str) -> str:
    if "foody" in url:
        return "Foody"
    if "shopeefood" in url:
        return "ShopeeFood"
    return "Unknown"


def count_emoji(text: str) -> int:
    return sum(1 for ch in text if ch in emoji.EMOJI_DATA)


def make_review_id(shop_id: int, user_name: str, dt_str: str) -> str:
    raw = f"{shop_id}_{user_name}_{dt_str}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def infer_region(city: str) -> str:
    north = {"ha noi", "hai phong", "quang ninh"}
    south = {"ho chi minh", "can tho", "vung tau"}
    city_norm = city.strip().lower()
    if city_norm in north:
        return "Bac"
    if city_norm in south:
        return "Nam"
    return "Trung"


# ---------------------------------------------------------
# 3. Chuẩn hoá 1 record raw -> dict theo schema đích
# ---------------------------------------------------------
def normalize_record(rec: dict) -> dict:
    raw_shop_id = rec.get("ID Quán", "")
    shop_id, shop_name = split_shop_id(raw_shop_id)

    url = rec.get("URL Quán", "")
    source_platform = infer_platform(url)  # dùng xong rồi bỏ url, không lưu lại

    comment_text = rec.get("Bình Luận", "") or ""
    dt_raw = rec.get("Ngày Giờ", "")
    try:
        comment_dt = datetime.strptime(dt_raw, "%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        comment_dt = pd.NaT

    return {
        "review_id": make_review_id(shop_id, rec.get("Tên User", ""), dt_raw),
        "shop_id": shop_id,
        "shop_name": shop_name,
        "city": rec.get("Thành Phố", ""),
        "region": infer_region(rec.get("Thành Phố", "")),
        "user_name": rec.get("Tên User", ""),
        "rating": pd.to_numeric(rec.get("Điểm Đánh Giá"), errors="coerce"),
        "device": rec.get("Thiết Bị", ""),
        "comment_datetime": comment_dt,
        "comment_hour": comment_dt.hour if pd.notna(comment_dt) else None,
        "day_of_week": comment_dt.strftime("%A") if pd.notna(comment_dt) else None,
        "comment_text": comment_text,
        "comment_length_words": len(comment_text.split()),
        "comment_length_chars": len(comment_text),
        "emoji_count": count_emoji(comment_text),
        "teencode_score": None,        # feature phức tạp -> để null, xử lý bằng LLM riêng
        "spelling_error_rate": None,   # feature phức tạp -> để null, xử lý riêng
        "num_reviews_by_user": None,   # điền ở bước aggregate bên dưới
        "label_source": "human",       # mặc định; ghi đè "bot_generated" khi merge dữ liệu AI sinh
        "source_platform": source_platform,
        "crawl_timestamp": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------
# 4. Pipeline chính: raw -> dedup -> fillna -> df chuẩn hoá
# ---------------------------------------------------------
def build_clean_dataframe(bucket) -> pd.DataFrame:
    raw_records = load_raw_records_from_minio(bucket)
    normalized = [normalize_record(r) for r in raw_records]
    df = pd.DataFrame(normalized)

    # --- Loại bỏ duplicate dựa trên review_id ---
    before = len(df)
    df = df.drop_duplicates(subset="review_id", keep="first")
    print(f"Removed {before - len(df)} duplicate rows")

    # --- Tính num_reviews_by_user sau khi đã dedup ---
    df["num_reviews_by_user"] = df.groupby("user_name")["review_id"].transform("count")

    # --- Tính shop_review_count / shop_avg_rating từ chính dataset ---
    shop_stats = (
        df.groupby("shop_id")["rating"]
        .agg(shop_review_count="count", shop_avg_rating="mean")
        .reset_index()
    )
    df = df.merge(shop_stats, on="shop_id", how="left")

    # --- Fill null cho các trường đơn giản (không đụng tới teencode_score / spelling_error_rate) ---
    df["rating"] = df["rating"].fillna(df["rating"].median())
    df["device"] = df["device"].fillna("Unknown")
    df["city"] = df["city"].fillna("Unknown")
    df["comment_text"] = df["comment_text"].fillna("")
    df["comment_hour"] = df["comment_hour"].fillna(-1).astype(int)
    df["day_of_week"] = df["day_of_week"].fillna("Unknown")

    # teencode_score và spelling_error_rate: giữ nguyên NaN, xử lý ở bước LLM scoring riêng

    return df


if __name__ == "__main__":
    final_df = build_clean_dataframe("foody-raw-data")
    print(final_df.shape)
    print(final_df.head())
    final_df.to_csv("reviews_clean.csv", index=False)
    #final_df.to_parquet("reviews_clean.parquet", index=False)