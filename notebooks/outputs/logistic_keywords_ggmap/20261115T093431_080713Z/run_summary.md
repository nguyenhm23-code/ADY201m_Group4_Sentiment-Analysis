### Nhận xét từ lần chạy Google Maps

- Dữ liệu: 147,565 review nguồn, 130,270 review sau lọc; nhóm 1–2 sao chiếm 13.98%.
- Validation chọn C = 10; mô hình cuối có 100,000 đặc trưng.
- Test macro-F1 = 0.8981, so với baseline 0.4628.
- Precision/recall/F1 nhóm thấp = 0.7809/0.8772/0.8263.
- Ma trận test: đúng nhóm còn lại 21,528; báo nhầm thấp 888; bỏ sót thấp 443; phát hiện đúng thấp 3,165.
- Có 8,062 ứng viên hệ số dương đủ support; 5,228 ứng viên đạt thêm lift ≥2.

Kết quả này đo phân loại rating trên nội dung chuẩn hóa chưa xuất hiện trong development. Các ứng viên keyword vẫn cần đọc ngữ cảnh; biểu đồ và ví dụ phủ định ở trên hỗ trợ kiểm tra giới hạn mô hình.