import os
from minio import Minio
from dotenv import load_dotenv
from datetime import datetime

# 1. TỰ ĐỘNG XÁC ĐỊNH ĐƯỜNG DẪN GỐC CỦA DỰ ÁN
# Lùi 2 lần từ utils -> src -> project
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 2. LOAD BIẾN MÔI TRƯỜNG TỪ FILE .env
env_path = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(env_path)

def upload_dataset_to_minio(file_name, bucket_name="foody-raw-data"):
    # Từ thư mục project gốc, chui vào src/ingestion để tìm file json
    local_file_path = os.path.join(PROJECT_ROOT, 'src', 'ingestion', file_name)
    
    print(f"Đang tìm kiếm file tại: {local_file_path}")

    # 3. KẾT NỐI MINIO BẰNG BIẾN MÔI TRƯỜNG (BẢO MẬT)
    client = Minio(
        "localhost:9000",
        access_key=os.getenv("MINIO_ROOT_USER"),     # Tự động lấy từ .env
        secret_key=os.getenv("MINIO_ROOT_PASSWORD"), # Tự động lấy từ .env
        secure=False
    )

    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        print(f"Đã tạo bucket mới: {bucket_name}")

    if os.path.exists(local_file_path):
        # 4. TỰ ĐỘNG LẤY NGÀY GIỜ HIỆN TẠI ĐỂ TẠO THƯ MỤC TRÊN MINIO
        today_str = datetime.now().strftime("%Y-%m-%d")
        minio_object_name = f"{today_str}/{file_name}" 
        
        client.fput_object(
            bucket_name,
            minio_object_name,
            local_file_path
        )
        print(f"Thành công! Đã đẩy file lên MinIO với tên: {minio_object_name}")
    else:
        print(f"Lỗi: Không tìm thấy file JSON. Vui lòng kiểm tra lại đường dẫn: {local_file_path}")

if __name__ == "__main__":
    filenames = [
        "foody_binh-thuan_dataset.json", "foody_da-nang_dataset.json",
        "foody_can-tho_dataset.json", "foody_ha-noi_dataset.json",
        "foody_hai-phong_dataset.json", "foody_ho-chi-minh-city_dataset.json",
        "foody_khanh-hoa_dataset.json", "foody_vung-tau_dataset.json"
    ]
    for filename in filenames:
        upload_dataset_to_minio(filename)
