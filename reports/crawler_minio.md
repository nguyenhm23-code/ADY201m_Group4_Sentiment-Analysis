# Cào Foody → MinIO → pipeline → PostgreSQL mỗi 2.000 dòng

Tại thư mục gốc dự án, chạy:

```powershell
.venv\Scripts\python.exe src/ingestion/foody/crawler_extended.py
```

Môi trường Python cần có các thư viện trong `requirements.txt`. MinIO và PostgreSQL
phải đang hoạt động. Có thể mở Docker Desktop rồi chạy:

```powershell
docker compose up -d minio postgres
```

`.env` cần có `MINIO_ENDPOINT`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`,
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
`POSTGRES_HOST=localhost`, `POSTGRES_PORT=5432` là mặc định.
`MINIO_SECURE=true` nếu endpoint dùng HTTPS, mặc định false.
Danh sách URL đầu vào vẫn do `get_urls.py` tạo trong
`src/ingestion/foody/extended/checkpoints/`.

## Cấu hình

- `DEFAULT_CITIES`, `PROFILE_DIR`, `FRESH_START`: ở đầu `crawler_extended.py`.
- `BATCH_SIZE = 2_000`, `BUCKET_NAME = "foody-raw-data"`: ở đầu `minio_upload.py`.
- Không cần thêm tham số dòng lệnh. `FRESH_START=True` cào lại dữ liệu local;
  checkpoint MinIO vẫn giữ lịch sử các bình luận đã upload.

## Dữ liệu trên MinIO

Mỗi 2.000 bình luận mới tạo một file JSON là mảng records, giữ nguyên trường dữ
liệu tiếng Việt. Bộ đệm dùng chung giữa các quán và thành phố. Không cần chờ cào
xong một quán mới upload. Phần dư dưới 2.000 record được gửi khi chạy xong.
Ví dụ 4.501 record được chia thành 2.000 + 2.000 + 501.

Ngay sau khi upload từng lô, crawler đọc lại **đúng object vừa upload**, kiểm tra
SHA-256 với payload local, chạy `basic_pipeline.build_dataframe`, lưu dữ liệu sạch
và dữ liệu bị loại, rồi upsert `shops`, `users`, `reviews` trong PostgreSQL.
Toàn bộ transaction của một lô thành công mới ghi nhận hoàn tất trong checkpoint.
Crawler chờ lô này xử lý xong rồi tiếp tục cào; không cần chạy riêng
`basic_pipeline.py` hay `postgres_load.py`.

```text
foody-raw-data/
  17-10-2026/
    foody_batch_<uuid>.json
    foody_batch_<uuid-khac>.json
```

Folder có định dạng `DD-MM-YYYY`, theo giờ Việt Nam UTC+7, được tạo tại lần thử
upload đầu tiên của lô. Các lô gửi trong cùng ngày dùng chung folder; tên file UUID
giúp chúng không ghi đè lẫn nhau. Khi thử lại một lô bị gián đoạn, giữ nguyên đường
dẫn object đã lưu, kể cả khi đã sang ngày mới, để tránh tạo bản sao.

## Log và thống kê

Log được in ra terminal và ghi bền vững vào:

```text
src/ingestion/foody/extended/checkpoints/ingestion_logs/events.jsonl
```

Mỗi dòng là một JSON event có timestamp ISO 8601 theo giờ Việt Nam (`+07:00`),
tên bucket/object và trạng thái. Các event: `records_queued`, `batch_started`,
`minio_uploaded`, `batch_completed`, `batch_failed`.

Mỗi lô còn có thư mục riêng trong `ingestion_logs/batches/foody_batch_<uuid>/`:

- `summary.json`: thống kê lô thành công, giờ bắt đầu/upload/hoàn tất, thời gian xử lý.
- `clean.json`: dữ liệu sạch đã đưa cho loader.
- `rejected.json`: nguyên record không hợp lệ và lý do bị loại.

| Trường log | Ý nghĩa |
| --- | --- |
| `raw_rows` | Số dòng của lô đã upload |
| `crawler_duplicates_skipped_total` | Tổng lượt dòng bỏ qua ở hàng đợi do đã thấy trong checkpoint; gồm cả lần đọc lại JSON local khi khởi động |
| `duplicates_in_batch` | Dòng hợp lệ bị pipeline gộp trùng trong chính lô này |
| `rejected_rows` | Dòng sai/thiếu dữ liệu, được cách ly trong `rejected.json` |
| `clean_rows` | Dòng hợp lệ, duy nhất gửi đến DB |
| `inserted` | Review được thêm mới vào DB |
| `updated` | Review đã tồn tại được upsert, kể cả nội dung không đổi |
| `skipped_existing` | Review thiếu ID bỏ qua vì đã có đúng một review tương ứng có ID trong DB |
| `duplicates_in_db` | `updated + skipped_existing`; không cộng thêm `updated` khi tính tổng dòng trùng |
| `promoted_legacy_ids` | Review cũ được bổ sung ID nguồn; không cộng vào tổng dòng |
| `uploaded_at` | Giờ upload MinIO của lần thử này |
| `db_written_at` | Giờ hoàn tất câu lệnh ghi DB trong transaction thành công ban đầu |
| `completed_at` | Giờ nhận kết quả commit và hoàn tất xử lý |
| `replayed` | `true` khi DB trả lại kết quả lô đã commit ở lần thử trước |

Đối với lô thành công:

```text
raw_rows = rejected_rows + duplicates_in_batch + clean_rows
clean_rows = inserted + updated + skipped_existing
```

Ví dụ: 2.000 raw, 20 lỗi, 30 trùng trong lô → 1.950 clean;
1.700 thêm mới + 240 cập nhật + 10 đã có nên bỏ qua → 250 trùng với DB.

Đọc nhanh các event gần nhất:

```powershell
Get-Content src/ingestion/foody/extended/checkpoints/ingestion_logs/events.jsonl -Tail 10
```

DB tự tạo bảng `public.foody_ingestion_batches` để lưu kết quả cùng transaction
với dữ liệu. Có thể xem lịch sử lô trên Metabase:

```sql
SELECT batch_id, recorded_at,
       (result->>'inserted')::int AS inserted,
       (result->>'updated')::int AS updated,
       (result->>'duplicates_in_db')::int AS duplicates_in_db
FROM public.foody_ingestion_batches
ORDER BY recorded_at DESC;
```

## Tiếp tục sau lỗi

`extended/checkpoints/minio_upload_state.json` lưu bộ đệm, ID bình luận đã gửi và
object đang upload. Khi upload, pipeline, DB hoặc ghi log lỗi, crawler dừng và đóng Chrome. Chạy lại cùng
lệnh để thử lại lô đang chờ rồi tiếp tục cào. Không xóa checkpoint nếu muốn giữ
lịch sử này, và chỉ chạy một crawler trên cùng checkpoint tại một thời điểm.

Nếu DB đã commit nhưng chương trình chưa kịp ghi checkpoint/log, lần sau giữ
nguyên object và nhận lại thống kê từ `foody_ingestion_batches`, không upsert
lại lô đó. Event `batch_completed` có thể xuất hiện nhiều lần khi thử lại;
không cộng dồn event này để tính tổng. Dùng `summary.json` duy nhất của mỗi lô
hoặc bảng receipt trong DB. Khi cả lô đều không hợp lệ, crawler dừng sau khi ghi
`rejected.json`; cần kiểm tra lỗi dữ liệu trước khi chạy lại.

Lần đầu dùng cơ chế mới, crawler cũng đưa JSON local đã có vào hàng đợi. Cơ chế
chống trùng áp dụng theo URL quán + ID bình luận trong checkpoint mới; không dò
các file từng upload thủ công trước đây. Một bình luận có cùng ID sẽ không được
upload lại khi nội dung hoặc điểm quán thay đổi. Record thiếu ID dùng hash toàn
bộ nội dung để nhận diện. JSON local theo thành phố vẫn được lưu như trước.

Checkpoint cũ vẫn đọc được. Các lô mà phiên bản cũ **đã đánh dấu upload xong**
không tự chạy lại qua DB. Nếu muốn nạp phần lịch sử đó, dùng JSON local và lệnh
`python -m src.ingestion.load_single_file --input <duong_dan_json>`; upsert sẽ
gộp theo định danh review. Không xóa checkpoint để ép upload lại.

## Kiểm thử

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Các bài kiểm thử dùng dịch vụ giả để kiểm tra kích thước lô, phần dư, múi giờ,
quarantine, chống trùng và khôi phục sau lỗi upload/DB/checkpoint. Pipeline làm
sạch chạy thật trong test. Các test PostgreSQL thật dùng schema tạm biệt lập,
chỉ chạy khi có biến `ETL_TEST_POSTGRES_DSN` trỏ tới DB kiểm thử.
