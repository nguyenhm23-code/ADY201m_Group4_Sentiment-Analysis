# Nhật ký hỗ trợ AI

File AI_Log.md trước đợt rà soát chứa dữ liệu NUL, không đọc được như Markdown. Bản gốc được giữ trong backup triển khai; nội dung dưới đây chỉ ghi những yêu cầu và công việc có bằng chứng trong hội thoại, không tái tạo nhật ký trước đó.

| Ngày | Yêu cầu của người dùng | AI hỗ trợ | Cách kiểm tra / giới hạn |
|---|---|---|---|
| 22/09/2026 | “giúp t hoàn thành hệ thống tiền xử lý dữ liệu, đồng bộ hóa phù hợp làm data training” | Cleaner chuẩn hóa Foody/Maps, JSONL/SQLite, nhãn tạm, import annotation, chia nhóm quán | Bộ13 tests ban đầu; snapshot410 review; bản được tạo trong workspace, chưa cài vào repoD lúc đó |
| 23/09/2026 | “kiểm tra tổng quan dự án, có gì bất thường hay thiếu sót cần bổ sung m hãy xử lý giúp t” và “tiếp tục tiến độ còn đang dang dở trước đó đi” | Rà soát ingestion, sửa ghi đè Foody và trạng thái Maps; củng cố cleaner; bổ sung MinIO/Compose, hai baseline, notebook, tài liệu/Git ignore | Kết quả cụ thể trong PROJECT_AUDIT.md; Docker/R/live crawl chưa chạy trong đợt rà soát; chưa có đánh giá ML bằng nhãn người |

Nhóm cần bổ sung kết quả chạy trên máy, lựa chọn chấp nhận/từ chối gợi ý, các prompt tiếp theo và thông tin thành viên thực hiện. Không coi nhãn rating hoặc output AI là nhãn người đã xác nhận. Không có kết quả nghiên cứu, commit hoặc báo cáo nộp nào được tự tạo trong log.

| 27/09/2026 | Phân tích và sửa lỗi diagnostics Maps, tự kiểm thử các quán đã lỗi | Kiểm tra danh sách review đầy đủ, phục hồi có giới hạn, phân biệt yêu cầu đăng nhập, sửa thành phố có mã bưu chính và tiến độ khi resume | 25 kiểm thử tự động và 7 tình huống DOM bằng Chrome; kiểm thử trực tiếp có kết quả và giới hạn riêng trong GMAP_REPAIR_20260927.md; chưa khẳng định ổn định toàn bộ quán |


## 2026-09-28 — diagnostics, kiểm thử, làm sạch và MinIO

- Yêu cầu: truy lỗi ảnh/diagnostics, rà soát code, lọc JSON hai nguồn và tải MinIO.
- Thay đổi: giới hạn transport ChromeDriver, diagnostics không làm hỏng kết quả partial, restart phiên hỏng, tải thêm review Foody; chuẩn hóa thành phố, xuất JSON theo nguồn, kiểm tra đối soát và upload processed có SHA-256 readback.
- Kiểm thử: 52 test trong tests + 21 test cleaner đạt; 3 quán Maps lỗi trước đó đạt 20 review mới/quán; Foody Ngô Khang đạt 26 review. Chưa khẳng định chạy ổn toàn bộ 996 quán.
- Dữ liệu: 5.429 đầu vào, 24 trùng ID, 5.405 master, 5.117 đủ điều kiện văn bản; raw 53 file và processed 14 file tải MinIO, đọc lại kiểm tra. Không tạo nhãn người hoặc tự quy kết bot.
- Bản sao lưu và chi tiết: reports/AUDIT_20260928.md; không commit/push tự động.
