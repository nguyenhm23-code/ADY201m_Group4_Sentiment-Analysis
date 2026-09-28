# Kế hoạch nghiên cứu — đề xuất để nhóm chốt

Đề tài: cảm xúc trong bình luận quán ăn tự crawl từ Foody và Google Maps. Đơn vị phân tích chính là review; đánh giá khả năng tổng quát hóa ở quán chưa xuất hiện trong tập train. Đây là kế hoạch đề xuất, chưa phải kết quả kiểm định hay báo cáo đã nộp.

## Giả thuyết có thể kiểm tra

1. **Cảm xúc:** H0: hai mô hình Logistic Regression và Linear SVM không khác nhau về macro-F1 trên tập review có nhãn người. H1: có khác biệt. Cần đánh giá theo nhóm quán, cùng snapshot và cùng holdout; chỉ so sánh score đơn thuần chưa đủ để kết luận ý nghĩa thống kê.
2. **Ngôn ngữ:** H0: độ dài bình luận không liên quan tới rating trong từng nguồn. H1: có liên quan. Phân tích riêng thang điểm từng nền tảng, cân nhắc phụ thuộc nhiều review trong cùng quán; không diễn giải tương quan là nhân quả.
3. **Xu hướng (chưa đủ dữ liệu):** H0: tỷ lệ bình luận tiêu cực không thay đổi theo tháng ở các quán có quan sát lặp. H1: có thay đổi. Cần ngày đăng đáng tin cậy và đủ quán/tháng; số review không đại diện trực tiếp lượng khách. Không lấy ngày thu thập thay ngày đăng.

## Đối chiếu yêu cầu môn

| Report | Nền tảng code được bổ sung | Việc nhóm vẫn cần thực hiện |
|---|---|---|
| 1 | Cấu trúc repo, tài liệu chạy, kế hoạch và sơ đồ kiến trúc | Chốt H0/H1 với giảng viên, viết/nộp proposal PDF |
| 2 | Crawler, lưu MinIO theo snapshot, cleaner, SQLite, Compose | Chạy MinIO/Docker thực tế, lưu bằng chứng raw bucket và SQL |
| 3 | Data Dictionary, notebook kiểm tra dữ liệu, RStudio EDA script | Chạy EDA trong RStudio, diễn giải kết quả và nộp báo cáo |
| 4 | Hai baseline và pipeline đánh giá có nhãn người | Gán nhãn, mở rộng tập quán, chạy đánh giá chính thức và kiểm định |
| 5 | Compose và hướng dẫn demo | Chạy demo toàn hệ thống, chuẩn bị slide/PDF, bảo vệ |

Không dùng dataset có sẵn. Không tự tạo điểm đánh giá model, ngày đăng chính xác, số khách hoặc nhãn người. Log AI ghi đúng quá trình hỗ trợ, không bịa commit, báo cáo hay kết quả. Nhóm cần tự duy trì ít nhất hai commit rõ nghĩa/tuần theo yêu cầu môn; agent không tự commit hoặc push.
