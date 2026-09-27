import os
from minio import Minio

# 1. Kết nối với MinIO Server đang chạy trên Docker
client = Minio(
    "localhost:9000",
    access_key="admin",
    secret_key="adminpassword",
    secure=False
)

bucket_name = "raw-data"

# 2. Kiểm tra bucket, nếu chưa có thì tạo mới
if not client.bucket_exists(bucket_name):
    client.make_bucket(bucket_name)
    print(f"Đã tạo bucket: {bucket_name}")
else:
    print(f"Bucket {bucket_name} đã sẵn sàng.")

# 3. Đường dẫn tới file CSV 35.000 dòng của bạn
file_path = "data/data_member_1_mien_bac.csv"
object_name = "foody_raw_35k.csv"

# 4. Upload file lên MinIO Data Lake
if os.path.exists(file_path):
    client.fput_object(bucket_name, object_name, file_path)
    print(f"Thành công! Đã đẩy file lên MinIO Data Lake tại bucket '{bucket_name}'.")
else:
    print(f"Không tìm thấy file tại đường dẫn {file_path}!")