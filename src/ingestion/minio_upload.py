import os
import sys
import argparse
from minio import Minio
from dotenv import load_dotenv
from datetime import datetime

# 1. TỰ ĐỘNG XÁC ĐỊNH ĐƯỜNG DẪN GỐC CỦA DỰ ÁN
# Lùi 2 lần từ utils -> src -> project
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 2. LOAD BIẾN MÔI TRƯỜNG TỪ FILE .env
env_path = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(env_path)

def upload_dataset_to_minio(file_name="foody_merged_dataset.json", bucket_name="foody-raw-data"):
    # Đọc file JSON đã gộp từ data/raw.
    local_file_path = os.path.join(PROJECT_ROOT, 'data', 'raw', file_name)
    
    print(f"Đang tìm kiếm file tại: {local_file_path}")
    if not os.path.isfile(local_file_path):
        raise FileNotFoundError(
            f"Không tìm thấy file JSON: {local_file_path}. "
            "Hãy chạy src/ingestion/foody/merge_json.py trước."
        )

    # 3. KẾT NỐI MINIO BẰNG BIẾN MÔI TRƯỜNG (BẢO MẬT)
    client = Minio(
        os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.getenv("MINIO_ROOT_USER"),     # Tự động lấy từ .env
        secret_key=os.getenv("MINIO_ROOT_PASSWORD"), # Tự động lấy từ .env
        secure=False
    )

    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        print(f"Đã tạo bucket mới: {bucket_name}")

    # 4. LƯU FILE GỘP TRÊN MINIO THEO NGÀY UPLOAD
    today_str = datetime.now().strftime("%Y-%m-%d")
    minio_object_name = f"{today_str}/{file_name}"

    client.fput_object(
        bucket_name,
        minio_object_name,
        local_file_path,
        content_type="application/json",
    )
    print(f"Thành công! Đã đẩy file lên MinIO với tên: {minio_object_name}")
    return minio_object_name

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Tải JSON thô đã gộp lên MinIO")
    parser.add_argument("--file", default="foody_merged_dataset.json", help="Tên file trong data/raw")
    parser.add_argument("--bucket", default="foody-raw-data")
    args = parser.parse_args()
    upload_dataset_to_minio(file_name=args.file, bucket_name=args.bucket)
