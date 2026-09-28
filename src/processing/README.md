# Tiền xử lý Foody và Google Maps

Pipeline dùng thư viện chuẩn Python, chuẩn hóa hai nguồn về cùng schema, xuất JSONL và SQLite. Không sửa dữ liệu crawl gốc. Chạy từ thư mục gốc dự án.

```powershell
python src/processing/cleaner.py --config configs/preprocessing.json
```

Chạy cả crawler rồi xử lý sau khi các worker kết thúc:

```powershell
python src/ingestion/main.py --preprocess --preprocess-config configs/preprocessing.json
```

Các tham số crawler cũ vẫn dùng được. Nếu một crawler thất bại, dữ liệu hiện có vẫn được xử lý và lệnh giữ exit code 1. Ctrl+C/exit 130 bỏ qua tiền xử lý. Không chạy cleaner độc lập trong lúc crawler còn đang cập nhật file. Tích hợp này là đồng bộ sau mỗi đợt crawl, chưa phải dịch vụ theo dõi file liên tục.

Đường dẫn riêng:

```powershell
python src/processing/cleaner.py --raw-dir data/raw --output-dir data/processed
python src/ingestion/main.py --preprocess --output-dir data/raw --processed-dir data/processed
python -m unittest discover -s src/processing -p test_cleaner.py -v
```

## Dữ liệu đầu vào và quy tắc

- Đọc `data/raw/foody/*dataset.json`, `data/raw/gmap/gmap_*.json` và Foody dataset ngay dưới raw. Bỏ qua queue, status, place metadata, diagnostics và Chrome profile.
- Dedup theo ID review có namespace nguồn/quán; nếu Foody thiếu ID review thì dùng hash quán + user + ngày gốc + nội dung. Hash fallback không nhận diện chắc chắn một review Foody đã sửa nội dung. Bản final và partial có cùng ID được hợp nhất, ưu tiên lần thu thập mới hơn.
- Giữ `text_raw` để truy vết; `text_clean` chuẩn hóa Unicode, khoảng trắng, HTML thông dụng; che mẫu email, URL, số điện thoại Việt Nam. Giữ dấu tiếng Việt, emoji, phủ định và dấu câu. Che mẫu không bảo đảm loại hết thông tin cá nhân trong văn bản; bản master/SQLite vẫn chứa text gốc, chỉ dùng nội bộ.
- Review không có chữ/emoji hoặc là placeholder được giữ trong master nhưng loại khỏi tập huấn luyện văn bản. Review dài trùng hoàn toàn trong cùng quán chỉ giữ một mẫu cho huấn luyện.
- Foody giữ thang 10, Maps giữ thang 5; không coi điểm trung bình quán là điểm từng review. `rating_fraction` chỉ là tỷ lệ điểm/thang, không chứng minh hai nền tảng có cùng phân bố đánh giá.
- Foody ưu tiên thành phố từ URL quán, gắn cờ khi khác trường dữ liệu; nguồn khác dùng trường dữ liệu hoặc địa chỉ có tên thành phố đã biết. Bỏ hậu tố mã bưu chính trước khi nhận diện tên thành phố. Không lấy tên file/khu vực tìm kiếm làm thành phố. Tên quán, thiết bị, thành phố thiếu giữ null.
- `search_area` chuẩn hóa thành danh sách tên khu vực, nhận cả chuỗi lẫn danh sách từ crawler. Trường này không thay thế thành phố.
- Ngày tương đối hoặc ngày chỉnh sửa không biến thành ngày đăng chính xác. Ngày dd/mm/yyyy không có giờ được đánh dấu `day`; giá trị 00:00 trong ISO chỉ để lưu trữ. Timestamp không có múi giờ giả định UTC+7 và gắn cờ; timestamp có múi giờ được đổi về UTC+7 để thống kê tháng nhất quán.
- Bình luận dạng object/list, ID sai kiểu và JSON chứa số vô hạn bị từ chối; không chuyển object thành chuỗi để đưa vào training. Review chỉ chứa token che thông tin liên hệ được giữ ở master, loại khỏi training.
- Dữ liệu bị lỗi được ghi vào `rejected.jsonl`; có bản ghi hợp lệ thì vẫn xuất snapshot và CLI trả 1 khi có rejection. Không có bản ghi hợp lệ thì giữ nguyên snapshot trước.

## Các file đầu ra

Mở `data/processed/latest.json` để tìm `path` của snapshot mới nhất. Snapshot nằm trong `data/processed/runs/<run_id>/`:

| File | Dùng để làm gì |
|---|---|
| `reviews.jsonl` | Master chuẩn hóa, một dòng JSON/review; gồm rating và nội dung gốc để kiểm tra |
| `reviews.sqlite` | Bảng reviews, rejected và view thống kê tháng; R/Python/SQL có thể đọc |
| `weak_train.jsonl` | Chỉ mẫu thuộc train chưa có nhãn người, nhãn tạm từ rating |
| `train.jsonl`, `validation.jsonl`, `test.jsonl` | Nhãn sentiment do người nhập; ban đầu rỗng nếu chưa gán nhãn |
| `annotation_queue.jsonl` | Mẫu chưa có nhãn sentiment; không hiển thị rating để giảm ảnh hưởng khi gán nhãn |
| `aspect_labels.jsonl` | Nhãn khía cạnh do người nhập, có split; chưa có nhãn thì rỗng |
| `quality_report.json` | Số lượng, thiếu dữ liệu, trùng, phân bố nguồn/thành phố/split/lớp, cảnh báo |
| `manifest.json` | Hash input, code, annotations và từng file đầu ra; config, label map, cách chia tập |
| `rejected.jsonl` | File/dòng bị từ chối và lý do |

Nội dung input, code, config, nhãn giống nhau sinh cùng run_id. Snapshot mới chỉ xuất hiện sau khi tất cả file ghi thành công; `latest.json` đổi bằng thao tác atomic. Không sửa các snapshot trực tiếp. Chỉ chạy một tiến trình cleaner vào một thư mục output tại một thời điểm. File `.building-*` từ tiến trình bị dừng đột ngột không phải snapshot đã công bố.

Pipeline kiểm tra SHA-256 và kích thước mọi artifact trước khi dùng lại snapshot. File thiếu/bị sửa làm lệnh thất bại rõ ràng, không âm thầm dùng dữ liệu hỏng. Khi gặp lỗi này, khôi phục bản snapshot nguyên gốc hoặc chạy với thư mục output mới để giữ bằng chứng kiểm tra.

## Nhãn và quy trình training

Label map: `negative=0`, `neutral=1`, `positive=2`. Nhãn tạm mặc định:

| Nguồn | Negative | Neutral | Positive |
|---|---|---|---|
| Maps | 1–2 | 3 | 4–5 |
| Foody | 0–4 | trên 4, dưới 7 | 7–10 |

Đây là heuristic cấu hình được, chưa được kiểm định với nhãn người. Rating cao vẫn có thể đi cùng bình luận tiêu cực. Chỉ dùng `weak_train` để thử baseline hoặc weak supervision; không dùng nhãn rating làm đáp án đánh giá cảm xúc văn bản.

1. Đọc `latest.json`; copy `annotation_queue.jsonl` ra `data/annotations/reviews.jsonl`, ngoài thư mục snapshot.
2. Giữ nguyên `review_id`, `text_sha256`, `text`, `split`. Điền `sentiment` và mã người gán nhãn `annotator`. Không dựa vào số sao; gán theo nội dung. Có thể nhập `mixed` nếu khen/chê cân bằng, `uncertain` nếu không đủ căn cứ. Hai nhãn này được giữ để rà soát và không ép thành neutral.
3. Có thể điền `aspects`: ví dụ `[{"aspect":"service","sentiment":"negative"}]`. Tên hợp lệ: `food`, `price`, `service`, `ambience`, `location`, `delivery`. Không nhắc tới khía cạnh thì không gán nhãn khía cạnh đó. Có thể chỉ gán aspects, để sentiment là null.
4. Ưu tiên hoàn thành validation/test, thống nhất hướng dẫn giữa người gán nhãn; kiểm tra bất đồng trên một phần mẫu. Đánh giá phải có đủ lớp và đủ quán độc lập, không chỉ đủ số review.
5. Nhập lại nhãn:

```powershell
python src/processing/cleaner.py --config configs/preprocessing.json --annotations data/annotations/reviews.jsonl
```

ID lạ, ID trùng, hash văn bản đã thay đổi, nhãn không hợp lệ, khía cạnh lặp hoặc thiếu mã annotator dạng chuỗi làm import thất bại và giữ nguyên `latest.json`. Template chưa điền được bỏ qua. Nếu nội dung review đã đổi, phải kiểm tra/gán nhãn lại phiên bản mới. Hash annotation ghi trong manifest luôn tương ứng đúng nội dung được import của lần chạy đó.

Khi training, dùng **chỉ trường `text` làm X**, `label` làm y. Có thể nối `train` và `weak_train` nhưng cần theo dõi riêng chất lượng nhãn; các file này không trùng review_id. Không đưa rating, weak_sentiment, restaurant/source/split ID vào đặc trưng mô hình sentiment văn bản. Fit TF-IDF/vocabulary, resampling, class weights từ train; tuning trên validation; báo cáo cuối trên test có nhãn người. Tokenization/word segmentation riêng cho model được thực hiện ở modeling, không phá text gốc tại đây.

## Chống rò rỉ dữ liệu và giới hạn

Chia theo nhóm quán; những quán có review dài trùng/gần trùng được gom cùng nhóm. Hash nhóm với seed quyết định train/validation/test (mục tiêu 80/10/10 theo nhóm, không bảo đảm tỷ lệ số dòng). Không tách ngẫu nhiên từng review. Review ngắn phổ biến như “Ngon” không nối tất cả quán thành một nhóm. Dò gần trùng dùng candidate blocking, là xấp xỉ và có cảnh báo nếu vượt giới hạn candidate.

Cùng quán xuất hiện trên Foody và Maps chưa tự động được nhận diện. Sau khi xác minh địa chỉ/URL, khai báo `restaurant_aliases` trong config: `{ "foody:<restaurant_id_hash>": "quan-thuc-te-01", "gmap:<restaurant_id_hash>": "quan-thuc-te-01" }`. Lấy ID đầy đủ trong reviews.jsonl; không suy đoán chỉ theo tên quán. Hai ID sẽ thuộc cùng nhóm chia tập.

Thêm review có liên kết trùng mới hoặc sửa alias có thể gộp nhóm và đổi split. Vì vậy **chốt một run_id cho mỗi thí nghiệm**, không đánh giá model cũ trên snapshot mới rồi coi đó là cùng holdout. Đây là split để đánh giá cảm xúc ở quán chưa thấy; dự báo xu hướng tương lai còn cần split theo thời gian và chỉ dùng thông tin trước thời điểm dự báo.

## Phân tích xu hướng và SQL

View hiện có chỉ mô tả rating quan sát được theo tháng và nguồn, chưa phải model dự báo. Không đồng nhất số review với lượng khách; không kết luận quan hệ nhân quả giữa quán đông và chất lượng. Không trộn trực tiếp rating thang 5 và 10. Ngày tương đối bị loại khỏi view vì không đủ độ chính xác.

```sql
SELECT source, COUNT(*) AS reviews, SUM(training_eligible) AS usable_text
FROM reviews GROUP BY source;

SELECT source, city, month, review_count, average_rating, rating_scale
FROM monthly_rating_observations ORDER BY source, city, month;

SELECT split, sentiment, COUNT(*) AS samples
FROM reviews WHERE label_source = 'human' AND training_eligible = 1
GROUP BY split, sentiment;
```

## Kết quả kiểm tra dữ liệu hiện tại

Lần kiểm tra raw ngày 23/09/2026: 211 dòng input → 209 review sau loại 2 ID trùng; 24 Foody, 185 Maps, 8 ID quán. Có 191 mẫu văn bản đủ điều kiện, 89 mẫu weak_train, 191 mẫu cần gán nhãn. 18 review không có nội dung. Không có input bị từ chối. Chưa có nhãn người nên train/validation/test chính thức rỗng.

155 review chưa xác định thành phố; 24 bản ghi trong file Foody mang tên HCM thực tế ghi Đà Nẵng. 185 review Maps chưa có ngày đăng chính xác. Master split: train 107, validation 102, test 0. Cần thêm quán độc lập trước khi đánh giá trên holdout; không coi tỷ lệ hash mục tiêu 80/10/10 là bảo đảm tỷ lệ số dòng ở bộ dữ liệu nhỏ.

Bản kiểm tra 22/09 có 410 review và 41 quán. Số lượng giảm là do raw Foody hiện tại thiếu dữ liệu so với snapshot cũ; cleaner không giả lập bản ghi raw để bù lại. Bản cũ cần được giữ nguyên để truy vết/khôi phục có kiểm soát. Các số liệu trên là thời điểm kiểm tra, không phải cam kết cho các lần crawl sau.

Module này phụ trách xử lý local → JSONL/SQLite. Hướng dẫn hạ tầng MinIO/Docker và tình trạng kiểm chứng toàn hệ thống nằm ở README gốc dự án. Chưa có nhãn người để đánh giá mô hình; EDA/nghiên cứu và kết quả hai model vẫn cần thực hiện trên dữ liệu phù hợp. Không commit raw/processed/annotations chứa dữ liệu lớn hoặc thông tin cá nhân; chỉ commit code/config/test và mẫu dữ liệu đã duyệt.


Schema 1.2.0 bổ sung `foody.json` và `gmap.json`: mảng JSON chuẩn hóa theo từng nguồn, đã khử trùng ID. `excluded_training.jsonl` lưu các dòng không đủ tín hiệu văn bản hoặc bản sao dài cùng quán; các dòng này vẫn còn ở master để phân tích rating. `quality_report.json` có `row_reconciliation_ok` kiểm tra đầu vào = giữ lại + trùng ID + từ chối. Ngày tương đối, địa chỉ chưa xác định và câu giống nhau không tự động bị coi là bình luận bot.
