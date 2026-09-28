# Rà soát dự án — 23/09/2026

Phạm vi: đọc code/config/dữ liệu hiện có, sửa các lỗi có thể kiểm chứng, bổ sung phần xử lý/lưu trữ/model/notebook còn trống. Không chạy crawler live, không tự commit/push, không tự gán nhãn người hoặc tạo kết quả nghiên cứu.

## Phát hiện và xử lý

| Mức độ | Bất thường trước sửa | Xử lý và bằng chứng kiểm tra |
|---|---|---|
| Cao | Foody khởi tạo rows rỗng mỗi đợt rồi ghi đè dataset ở quán đầu, có thể mất lịch sử khi chạy lại | `crawler.load_existing_reviews` nạp/gộp dữ liệu trước lưu; JSON hỏng được giữ nguyên và báo lỗi. Tests rerun và corrupt history |
| Cao | URL Foody trang chủ phụ thuộc vùng cookie; file mang tên HCM chứa review URL Đà Nẵng | Dùng URL khu vực rõ ràng, lọc link đúng slug vùng; cleaner không tin tên file, ghi city mismatch |
| Cao | Maps có thể abort cả lượt vì status resume hỏng hoặc báo thành công khi mới partial/thiếu quota | Bảo vệ resume, tiếp tục quán khác, phân biệt partial/no_reviews/completed, báo incomplete/shortfall; 16 tests ingestion offline |
| Cao | `cleaner.py`/`model.py`/requirements/Compose còn trống trong repo thật | Tích hợp cleaner 1.1, MinIO adapter, SQLite, hai baseline và lệnh main `--preprocess` |
| Vừa | File cấu hình/schema không hợp lệ có thể crash hoặc thành văn bản giả, mẫu chỉ có số điện thoại/email vẫn được train | Kiểm tra config/kiểu trường/NaN, lọc mẫu chỉ còn token che liên hệ; 21 tests preprocessing |
| Vừa | Snapshot có thể bị sửa nhưng được tái sử dụng; file annotation có thể thay đổi giữa lúc đọc và hash | Kiểm tra SHA256/size artifact trước reuse; import annotations và hash cùng một payload bytes |
| Vừa | README và AI_Log không đọc được do chứa NUL, notebook là file 0 byte | Tài liệu UTF-8 mới, AI log chỉ ghi sự kiện đã có bằng chứng; notebook hợp lệ có output thực, RMarkdown có hướng dẫn |
| Vừa | `.gitignore` chưa loại raw/processed/annotation/browser cache; Docker build context có thể mang theo dữ liệu | Bổ sung ignore và `.dockerignore`; không tự untrack file đã commit trước |
| Vừa | Chưa có luồng Data Lake→DB để thực hiện Report 2 | Adapter upload manifest cuối, restore kiểm tra hash, chạy cleaner từ MinIO về SQLite; Compose MinIO/app/RStudio profile. Tests dùng mock, chưa chạy MinIO thật |

Foody bổ sung tên quán, nguồn, thang điểm và thời điểm thu thập cho review mới nếu DOM có thông tin. Maps giữ lịch sử final/partial, không tự cắt dữ liệu khi giảm giới hạn; thử đợi panel thêm một lần có hạn và thoát khi cuối danh sách ổn định. Đã giữ cơ chế quota vùng xen kẽ.

## Dữ liệu được kiểm tra

Raw hiện tại:211 dòng→209 review duy nhất,24 Foody+185 Maps,8 ID quán,191 mẫu text đủ điều kiện,89 weak_train. Có18 review không có nội dung;155 review không rõ city;185 review Maps không có ngày đăng chính xác.24 review Foody mang tên file HCM nhưng dữ liệu ghi Đà Nẵng.

Snapshot hiện tại:`af2c5fbbb7743a69`. Không có row/file bị từ chối. Master chia107 train/102 validation/0 test; chưa có nhãn người nên các file train/validation/test chính thức rỗng. Tỷ lệ hash mục tiêu không bảo đảm tỷ lệ mẫu ở bộ dữ liệu chỉ8 quán.

Archive nguyên vẹn từ22/09 có410 review,225 Foody+185 Maps; lưu riêng tại `data/recovery/2026-09-22`. Không trộn lại vì không còn đủ trường định danh raw để kiểm tra dedup an toàn khi crawl lại. Đây là dữ liệu của chính dự án, không phải dataset lấy sẵn bên ngoài.

Maps queue có956 địa điểm:2 completed,2 partial,4 failed,948 pending tại lúc đọc. Đây là số địa điểm đã dò, không phải956 quán đã crawl review. Batch có thể dừng sau3 lỗi quán liên tiếp; lỗi selector/challenge cần kiểm tra lại bằng Chrome thực tế.

## Kiểm chứng đã thực hiện

- 51 tests:16 ingestion,21 preprocessing,10 storage,4 modeling. Kiểm thử model dùng fixture kỹ thuật để xác nhận code chạy; không báo score fixture như kết quả dự án.
- Chạy cleaner trên raw thật; SQLite `integrity_check=ok`; ID review duy nhất và nhóm split không giao nhau.
- Train thành công Logistic Regression và Linear SVM trên89 mẫu `weak_train`; lưu artifact có mode `weak_baseline_not_evaluated`, không sinh metric đánh giá từ nhãn rating.
- Hai notebook Python đã chạy top-to-bottom, schema notebook hợp lệ, không có output error. Hai PNG biểu đồ đã xem trực tiếp và đối chiếu số liệu; chưa mở trong giao diện Jupyter để kiểm tra toàn bộ bố cục notebook.
- Python source được parse; Compose YAML được parse. Đây **không** phải `docker compose config` hoặc image build đã chạy.

## Còn thiếu hoặc chưa kiểm chứng

1. Docker CLI và Rscript chưa có ở môi trường rà soát: cần build và demo MinIO thật, kiểm tra health/credentials, chạy RMarkdown trong RStudio. MinIO Dockerfile dùng release source cố định theo [hướng dẫn release chính thức](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z); chưa xác nhận build local.
2. Chưa chạy lại crawler live sau sửa. Foody mới lấy review ban đầu có sẵn trong DOM, chưa phân trang/load-more toàn bộ. Cần quan sát DOM thật và kiểm tra độ phủ trước khi gọi là crawl đầy đủ.
3. Cần thêm quán độc lập, xác minh quán trùng giữa nguồn, gán nhãn sentiment/aspect thủ công. Không tự thay nhãn rating thành gold hoặc tạo test giả để có score.
4. Ngày Maps tương đối không đủ cho dự báo xu hướng theo tháng; số review không đồng nghĩa số khách. Chưa có model dự báo thời gian hoặc kết luận H0/H1.
5. Git lịch sử gần nhất vẫn là3 commit ngày07/09 tại lúc audit; nhiều code mới chưa track. Agent không commit/push thay nhóm. Cần duy trì quy định môn và hoàn thiện PDF/slides/báo cáo thực tế.

## Áp dụng và rollback

Bản cập nhật được chuẩn bị trong workspace trước. Trình triển khai kiểm tra hash file gốc còn khớp, backup những file sẽ thay đổi vào `.audit-backups/<timestamp>/`, rồi mới copy payload đã kiểm thử. Không sửa `data/raw`, không xóa repo hoặc môi trường Python. Trạng thái áp dụng thực tế được ghi trong `deployment_status.json` sau khi triển khai thành công; không có file đó thì chưa khẳng định đã cài.
