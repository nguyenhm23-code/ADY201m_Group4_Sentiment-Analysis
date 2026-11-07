# Kiểm tra pipeline và schema pre-EDA — 01/10/2026

Yêu cầu áp dụng: MinIO → lọc/làm sạch → PostgreSQL → Metabase; không tạo thêm
feature modeling. Tài liệu `db_schema_pre_eda.md` thay thế thiết kế feature trong
`features_and_db_schema.md`. `hypotheses.md` là ngữ cảnh phân tích, chưa triển khai
feature, kiểm định hay sinh review bot tại giai đoạn này.

## Các sửa đổi

- Cả `basic_pipeline.py` và `postgres_load.py` chạy được trực tiếp trong thư mục
  script hoặc bằng `python -m` từ root; tự thêm project root vào đường dẫn import
  khi chạy trực tiếp nên không còn lỗi `No module named src`.
- Basic pipeline xuất `data/processed/foody_clean.json`. Loader chỉ đọc file này;
  chạy loader trước khi có file sạch sẽ báo rõ phải chạy pipeline trước.
- Ba bảng shops/users/reviews dùng hợp đồng pre-EDA, không có cột feature.
  Review có BIGSERIAL surrogate riêng và nullable BIGINT ID nguồn, giữ cả dữ liệu
  ngày 28/09 thiếu ID. Pseudo-user là tên lower/trim, giữ tên hiển thị gốc ở users.
- 26 trường của crawler extended đều có mapping lưu DB, gồm URL quán. Crawler cũ
  có tám trường, được hỗ trợ. Thiếu trường tùy chọn lưu NULL, không tự bịa số liệu.
- Rating, user, thời gian hoặc giá không hợp lệ được cách ly với toàn bộ raw record
  trong `outputs/pre_eda_rejected.json`; các record này chưa được nạp vào DB sạch.
  Tất cả JSON gốc vẫn còn trên MinIO và data/raw. Trường crawl mới chưa có mapping
  cũng bị báo/cách ly, không bị bỏ âm thầm.
- Không DROP schema/data cũ. Loader từ chối bảng có bộ cột khác hợp đồng pre-EDA.

## Điều chỉnh so với ví dụ SQL trong tài liệu

1. Giữ `shop_url` theo yêu cầu trực tiếp của người dùng lưu tất cả trường crawl,
   dù tài liệu tham khảo đề xuất bỏ URL.
2. Unique khóa tự nhiên chỉ áp dụng khi source_review_id IS NULL, đúng phần giải
   thích trong tài liệu. Giữ các ID nguồn khác nhau có cùng nội dung/thời gian.
3. Index khóa tự nhiên dùng `md5(comment_text)` vì bản index nguyên văn đã thực
   sự thất bại khi nạp: index row 3040 byte vượt giới hạn 2704 byte. DB giữ nguyên
   TEXT đầy đủ (bình luận không ID dài nhất 6906 byte). Loader so sánh nguyên văn
   khi phát hiện hash trùng nhưng nội dung khác và rollback, tránh gộp nhầm.
   Đây là biểu thức index, không phải feature mới hay cột phân tích.

## Kết quả chạy dữ liệu thật

| Chỉ số | Số lượng |
|---|---:|
| Raw đọc từ MinIO (28, 29, 30/09) | 167.059 |
| Record cách ly | 171 |
| Giá cao nhất nhỏ hơn giá thấp nhất | 151 |
| Thiếu user/quán/thành phố | 14 |
| Rating/ngày giờ/bình luận thiếu hoặc sai | 6 |
| Review sạch sau lọc trùng | 101.175 |
| Review có ID nguồn | 89.285 |
| Review không ID nguồn | 11.890 |
| Quán | 3.221 |
| Pseudo-user | 30.901 |
| Quán Bắc / Trung / Nam | 143 / 371 / 2.707 |

Chạy `python basic_pipeline.py` trực tiếp tại thư mục processing đã thành công.
Chạy `python postgres_load.py` trực tiếp tại thư mục ingestion đã thành công.
Chạy lại loader bằng module trên cùng file sạch không thay đổi số review và
khóa surrogate. Không dùng số ID cuối làm số record: BIGSERIAL có thể có khoảng
trống sau UPSERT/rollback, đây là hành vi sequence của PostgreSQL.

15 test đã qua, trong đó sáu test trên PostgreSQL thật với schema tạm riêng:
UPSERT lặp lại, nâng record thiếu ID lên ID nguồn và giữ surrogate, giữ các ID
nguồn khác nhau, bình luận dài không bị cắt, rollback cả parent/review và từ chối
schema cũ. Toàn bộ trường của reviews/shops/users đã được đối chiếu với file sạch.

## Giới hạn dữ liệu nguồn

- Display name không định danh được người thật; người trùng tên có thể chung
  pseudo-user. Dữ liệu chỉ lưu phiên bản metadata quán/user gần nhất, không lưu
  lịch sử từng snapshot trong schema này.
- JSON không có tên quán, tên được dựng từ slug để đáp ứng shop_name NOT NULL.
  Không có thời điểm crawl nguồn, các timestamp theo dõi là lúc ETL quan sát.
- Quán có price_max=0 < price_min được cách ly thay vì tự sửa giá. 171 raw record
  trong báo cáo chưa thuộc DB sạch; sửa dữ liệu nguồn hoặc quy tắc làm sạch rồi
  chạy lại hai bước nếu cần đưa chúng vào DB.
- Timestamp crawler cũ có giây, crawler mới có phút; chỉ gộp khi khóa thực sự
  trùng, không làm tròn để đoán hai review là một.

## Mapping trường crawl → DB

| Trường nguồn | Cột DB |
|---|---|
| ID Quán | `shops.shop_id` |
| Thành Phố | `shops.city` |
| URL Quán | `shops.shop_url` |
| Giờ Mở Cửa | `shops.opening_hour` |
| Giờ Đóng Cửa | `shops.closing_hour` |
| Giá Thấp Nhất | `shops.price_min` |
| Giá Cao Nhất | `shops.price_max` |
| Lượt Xem | `shops.views` |
| Tổng Số Bình Luận | `shops.shop_review_count` |
| Điểm Trung Bình Quán | `shops.shop_avg_rating` |
| Số Bình Luận Tuyệt Vời | `shops.excellent_count` |
| Số Bình Luận Khá Tốt | `shops.good_count` |
| Số Bình Luận Trung Bình | `shops.average_count` |
| Số Bình Luận Kém | `shops.poor_count` |
| Điểm Vị Trí | `shops.location_score` |
| Điểm Giá Cả | `shops.price_score` |
| Điểm Chất Lượng | `shops.quality_score` |
| Điểm Phục Vụ | `shops.service_score` |
| Điểm Không Gian | `shops.space_score` |
| Tên User | `users.user_name` |
| ID Bình Luận | `reviews.source_review_id` |
| Điểm Đánh Giá | `reviews.rating` |
| Thiết Bị | `reviews.device` |
| Ngày Giờ | `reviews.comment_datetime` |
| Bình Luận | `reviews.comment_text` |
| Số Ảnh Bình Luận | `reviews.num_photos` |

## SQL để vẽ trên Metabase

Chọn nguồn PostgreSQL đã kết nối rồi mở truy vấn SQL:

```sql
SELECT s.region, COUNT(*) AS review_count, ROUND(AVG(r.rating), 2) AS avg_rating
FROM public.reviews r
JOIN public.shops s ON s.shop_id = r.shop_id
GROUP BY s.region
ORDER BY review_count DESC;
```

Chọn biểu đồ cột với region là trục X và review_count là trục Y.
Không cần kéo dữ liệu về Python/DataFrame. View `public.foody_reviews_flat`
được cung cấp để truy vấn review cùng thông tin quán và user.
