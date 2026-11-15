# BÁO CÁO TIẾN TRÌNH XÂY DỰNG HỆ THỐNG THU THẬP VÀ PHÂN TÍCH ĐÁNH GIÁ NHÀ HÀNG

## 1. Mục tiêu và hướng triển khai

Phần công việc tập trung vào việc xây dựng luồng dữ liệu phục vụ nghiên cứu các từ và cụm từ liên hệ với đánh giá thấp của khách hàng. Quá trình triển khai đi từ thu thập review Foody, lưu dữ liệu thô trên MinIO, chuẩn hóa và nạp PostgreSQL, đến phân tích khám phá, đối chiếu Google Maps và xây dựng mô hình Logistic Regression để tìm keyword ứng viên.

Câu hỏi nghiên cứu chính là: **Những từ và cụm từ nào có liên hệ rõ rệt với nhóm review điểm thấp, và mối liên hệ đó được thể hiện như thế nào qua thống kê và mô hình học máy?** Phân tích liên nền tảng bổ sung câu hỏi về khả năng duy trì mối liên hệ của cùng một cụm từ trên hai nguồn dữ liệu.

Kết quả thực nghiệm sử dụng 132.640 review Foody và 130.270 review Google Maps sau xử lý. Hai mô hình TF-IDF kết hợp Logistic Regression được huấn luyện riêng trên từng nguồn, đạt macro-F1 test lần lượt 0,8076 và 0,8981. Bên cạnh dự đoán nhóm điểm, quy trình xuất trọng số đặc trưng, đối chiếu support và lift, đồng thời giải thích đóng góp của từng đặc trưng trong một review.

## 2. Tổ chức hệ thống và luồng dữ liệu

Hệ thống phân tách dữ liệu theo mục đích sử dụng để có thể truy vết từ kết quả phân tích về dữ liệu nguồn.

| Thành phần | Vai trò |
| --- | --- |
| Python crawler | Thu thập thông tin quán và review Foody |
| MinIO | Lưu các lô JSON thô để đối chiếu và xử lý lại |
| Pipeline chuẩn hóa | Chuyển schema, kiểm tra giá trị, lọc trùng và cách ly bản ghi lỗi |
| PostgreSQL | Lưu dữ liệu có cấu trúc trong các bảng quán, người dùng và review |
| JSON snapshot | Cung cấp đầu vào xác định cho các notebook |
| Jupyter Notebook | Thực hiện EDA, phân tích keyword, so sánh nguồn và modeling |

Docker Compose khai báo ba dịch vụ MinIO, PostgreSQL 16 và Metabase. MinIO phục vụ API lưu trữ và giao diện quản trị; PostgreSQL lưu dữ liệu quan hệ; Metabase cung cấp môi trường truy vấn và quan sát dữ liệu. Các dịch vụ có vùng lưu trữ riêng để giữ dữ liệu qua các lần khởi động lại container.

## 3. Thu thập dữ liệu Foody

### 3.1. Thu thập danh sách quán

Việc thu thập được tách thành hai bước. Trước hết, chương trình `get_urls.py` dùng Selenium và Chrome để mở trang theo thành phố, lấy liên kết quán, cuộn trang và thao tác nút “Xem thêm”.

Các URL được kiểm tra theo tên miền và cấu trúc đường dẫn, loại tham số truy vấn và dấu gạch chéo cuối, đồng thời tránh lưu trùng trong một lần thu thập. Danh sách được ghi thành file theo thành phố để crawler review có thể sử dụng lại. Chương trình dừng khi đạt số lượng yêu cầu hoặc không tìm được URL mới qua hai lượt liên tiếp.

Cách tách bước này cho phép kiểm tra phạm vi quán trước khi thu thập review, đồng thời tái sử dụng danh sách khi cần cào lại.

### 3.2. Thu thập thông tin quán và review

Crawler đọc thông tin quán từ trang web, gồm URL, thành phố, điểm trung bình, điểm theo khía cạnh, khoảng giá, giờ hoạt động và số lượng bình luận khi các trường này xuất hiện.

Đối với review, chương trình lấy mã quán từ dữ liệu khởi tạo của trang và gọi endpoint phân trang `/__get/Review/ResLoadMore` trong cùng phiên Chrome. Mỗi yêu cầu lấy 10 review, sử dụng `LastId` và danh sách loại trừ để tiếp tục phân trang. Các chế độ mới nhất, thứ tự khác, có ảnh và không ảnh được kết hợp nhằm mở rộng phạm vi thu thập.

Mỗi review gồm mã bình luận nguồn, mã quán, người viết, rating, thiết bị, thời gian đăng, nội dung và số ảnh. HTML trong nội dung được chuyển thành văn bản; thời gian nguồn được chuyển sang giờ Việt Nam. Review thu được qua nhiều bộ lọc được hợp nhất theo mã bình luận.

Crawler có cơ chế thử lại khi gọi API thất bại, kiểm tra trang lặp và đối chiếu số review thu được với tổng số mà Foody công bố. Việc đối chiếu này giúp nhận diện quán cần tiếp tục thu thập, thay vì suy luận rằng kết thúc phân trang đồng nghĩa đã thu đủ dữ liệu.

### 3.3. Lưu dữ liệu và tiến độ

Dữ liệu được lưu thành JSON theo thành phố. Chương trình ghi qua file tạm rồi thay thế file đích để giảm nguy cơ để lại JSON đang ghi dở.

File tiến độ ghi lại URL quán đã đáp ứng điều kiện hoàn tất. Ở lần chạy tiếp theo, các URL này được bỏ qua; quán chưa đáp ứng điều kiện vẫn có thể được xử lý lại. Ngoài bản lưu theo thành phố, các review mới được chuyển sang bộ đệm upload theo lô.

## 4. Lưu MinIO và chuẩn hóa dữ liệu

### 4.1. Upload theo lô và phục hồi

Module `MinioBatchUploader` tổ chức upload vào bucket `foody-raw-data`, với kích thước lô mặc định 2.000 review. Phần dư được gửi khi kết thúc bình thường. Tên object gồm ngày upload và mã lô riêng.

Bộ đệm có checkpoint lưu review đang chờ, khóa đã xử lý, tên object và số lô hoàn tất. Khóa chống gửi trùng ưu tiên kết hợp URL quán với mã review nguồn; trường hợp không có mã nguồn sử dụng nội dung bản ghi để tạo khóa.

Tên object và payload được ghi trước khi thực hiện thao tác mạng. Khi xử lý bị gián đoạn, lần chạy tiếp theo có thể tiếp tục đúng lô đang dở. Các sự kiện bắt đầu, upload, hoàn tất và thất bại được ghi vào log JSONL, kèm số dòng và giai đoạn xử lý.

### 4.2. Đọc lại dữ liệu từ MinIO

Sau upload, `process_minio_batch` đọc lại object từ MinIO và đối chiếu SHA-256 với payload checkpoint. Bước kiểm tra này bảo đảm dữ liệu đưa vào chuẩn hóa khớp với lô đã gửi.

Pipeline ánh xạ các trường thu thập sang schema thống nhất, chuyển rating và số lượng về dạng số, chuẩn hóa ngày giờ và kiểm tra các điều kiện như miền rating, số ảnh không âm, mã nguồn hợp lệ và quan hệ giữa giá tối thiểu với giá tối đa.

Dữ liệu không hợp lệ được cách ly trong `rejected.json`, kèm lý do và bản ghi nguồn. Dữ liệu đạt điều kiện được lưu vào `clean.json` và chuyển sang bước nạp cơ sở dữ liệu.

### 4.3. Kết quả vận hành đã ghi nhận

Log ngày 30/10/2026 ghi nhận một lô 371 review hoàn tất luồng MinIO, chuẩn hóa và PostgreSQL: 371 dòng sạch, 371 dòng thêm mới, không có dòng bị cách ly trong lô đó.

Một lô khác gồm 5 review ghi nhận 2 dòng thêm mới và 3 dòng cập nhật. Đây là bằng chứng vận hành cho cả trường hợp nạp dữ liệu mới và gặp lại dữ liệu đã có. Các con số này phản ánh từng lô, không phải tổng số review của toàn bộ hệ thống.

## 5. Tổ chức và nạp dữ liệu PostgreSQL

Dữ liệu được phân thành ba bảng nghiệp vụ và một view phục vụ phân tích.

| Bảng hoặc view | Nội dung |
| --- | --- |
| `shops` | Thông tin quán, địa điểm, giá, điểm theo khía cạnh và URL |
| `users` | Khóa người dùng quy ước, tên hiển thị và thời điểm ghi nhận |
| `reviews` | Nội dung, rating, thời gian, thiết bị, mã nguồn và khóa liên kết |
| `foody_reviews_flat` | Ghép ba bảng thành dạng phẳng để truy vấn và xuất dữ liệu |

`review_id` là khóa do PostgreSQL sinh; `source_review_id` lưu mã bình luận từ nguồn. Bản ghi có mã nguồn được xử lý theo mã đó. Khi thiếu mã nguồn, pipeline sử dụng khóa tự nhiên gồm quán, người dùng quy ước, thời gian và nội dung để đối chiếu.

Cơ chế upsert cho phép thêm mới hoặc cập nhật dữ liệu gặp lại, đồng thời giữ giá trị cũ khi dữ liệu mới thiếu trường tùy chọn. Một bảng biên nhận lô được ghi cùng transaction với dữ liệu review để nhận diện lô đã commit khi thực hiện lại.

Khóa người dùng quy ước được tạo từ tên hiển thị chuẩn hóa, vì vậy chỉ phục vụ liên kết theo quy tắc hiện có, không xác nhận danh tính duy nhất của người viết.

Module `db_to_json.py` xuất view phẳng thành JSON theo từng phần bằng cursor, tránh phải giữ toàn bộ kết quả truy vấn trong bộ nhớ. File đích chỉ được thay thế sau khi snapshot được xuất thành công. Bộ dữ liệu Foody sử dụng trong EDA có 138.131 dòng và 34 cột.

## 6. Kiểm tra chất lượng và chuẩn bị dữ liệu phân tích

### 6.1. Chuẩn hóa và lọc trùng

Nội dung gốc được giữ lại; một cột `text_norm` riêng được tạo bằng chuẩn hóa Unicode NFC, chuyển chữ thường, gộp khoảng trắng và bỏ khoảng trắng đầu cuối.

Đối với Foody, quy tắc lọc trùng trong EDA sử dụng sáu thành phần: nền tảng, quán, thời gian bình luận, rating, nội dung chuẩn hóa và thiết bị. Chỉ các dòng đủ khóa mới được xét loại trùng; dòng thiếu khóa được giữ để tránh gộp thiếu căn cứ.

Quy tắc này loại 5.305 dòng, còn 132.826 review. Đây là quy tắc xác định trùng theo nội dung và bối cảnh, không mặc nhiên chứng minh mọi dòng bị loại đều là lỗi crawler.

### 6.2. Rating bằng 0 và văn bản không có token

Dữ liệu Foody có 40 review rating bằng 0. Việc đọc mẫu cho thấy có trường hợp nội dung tích cực, nên điểm 0 không được mặc nhiên xem là nhãn tiêu cực đáng tin cậy. Nhóm này được khảo sát riêng ở bước kiểm tra dữ liệu và loại trước phân tích keyword, modeling.

Token được xác định bằng regex `(?u)\b\w+\b`. Đây là các đơn vị chữ hoặc số theo regex, không phải kết quả tách từ tiếng Việt bằng mô hình ngôn ngữ. Vì vậy, “phục vụ” gồm hai token và được biểu diễn bởi một bigram.

Có 146 review không tạo được token theo quy tắc này. Sau khi loại 40 review rating bằng 0 và 146 review không có token, tập làm việc Foody còn 132.640 review: 23.144 review điểm thấp và 109.496 review còn lại. Tỷ lệ điểm thấp là 17,45%.

### 6.3. Chuẩn bị Google Maps để đối chiếu

Dữ liệu Google Maps được ánh xạ về các trường tương ứng với Foody. Rating 1–5 sao được nhân 2 để đưa về thang số 0–10; theo quy ước nhóm thấp không quá 5, review Google Maps 1–2 sao thuộc nhóm thấp, còn 3–5 sao thuộc nhóm còn lại.

Bộ Google Maps thiếu toàn bộ thời gian đăng và không có trường thiết bị trong schema phân tích. Vì vậy, khóa lọc trùng sử dụng bốn trường nền tảng, quán, rating và nội dung chuẩn hóa.

| Mốc xử lý | Foody | Google Maps |
| --- | ---: | ---: |
| Review đầu vào | 138.131 | 147.565 |
| Comment rỗng sau chuẩn hóa | 0 | 15.534 |
| Rating thiếu | 0 | 1.010 |
| Dòng bị loại theo khóa trùng | 5.305 | 669 |
| Corpus hợp lệ sau lọc trùng | 132.826 | 130.352 |
| Review dùng cho keyword | 132.640 | 130.270 |
| Review điểm thấp trong tập keyword | 23.144 | 18.206 |
| Tỷ lệ điểm thấp | 17,45% | 13,98% |

Hai nền tảng sử dụng khóa lọc trùng khác nhau và có mức thiếu dữ liệu khác nhau. Vì thế, số dòng bị loại không đủ để kết luận nguồn nào có chất lượng tốt hơn. Việc đổi thang điểm cũng chỉ thiết lập quy ước so sánh, không bảo đảm hành vi chấm điểm của người dùng hai nền tảng tương đương.

## 7. Phân tích khám phá và tìm keyword bằng thống kê

### 7.1. Phân phối rating, độ dài và nội dung lặp

Phân phối rating cho thấy nhóm điểm cao chiếm đa số trên cả hai nền tảng. Đây là cơ sở để xem xét baseline khi đọc lift và sử dụng thêm các chỉ số theo lớp khi đánh giá mô hình.

Trong corpus, trung vị độ dài Foody là 70 token, Google Maps là 23 token. Tuy nhiên, khi xét theo nhóm điểm trong tập làm việc, Foody có trung vị 41 token ở nhóm thấp và 78 token ở nhóm còn lại; Google Maps lần lượt là 37 và 21 token.

Như vậy, hướng chênh lệch độ dài giữa hai nhóm điểm đảo ngược giữa hai nguồn. Review dài có nhiều cơ hội chứa một cụm từ hơn, nên độ dài cần được cân nhắc khi giải thích các cụm kể chuyện hoặc từ nối xuất hiện nhiều trong review thấp.

Sau lọc trùng theo khóa, corpus Foody vẫn có 7.848 review thuộc các nhóm nội dung lặp, tương đương 5,91%; Google Maps có 6.658 review, tương đương 5,11%. Các nội dung này được khảo sát theo số quán và phân bố rating. Không áp dụng xóa toàn bộ nội dung giống nhau vì những câu ngắn phổ biến có thể xuất hiện ở nhiều review khác nhau.

### 7.2. Support, lift và độ phủ

Phân tích sử dụng unigram, bigram và trigram. Với CountVectorizer đặt `binary=True`, mỗi review chỉ đóng góp tối đa một lượt cho một cụm, dù cụm đó xuất hiện nhiều lần trong review.

Với cụm k và nhóm điểm thấp L:

- **Support** là số review chứa k.
- **Low support** là số review điểm thấp chứa k.
- **Lift** bằng P(L | k) / P(L).
- **Độ phủ nhóm thấp** bằng số review thấp chứa k chia tổng số review thấp, nhân 100%.

Lift đo mức tập trung tương đối của cụm trong nhóm thấp so với baseline; độ phủ cho biết cụm xuất hiện trong bao nhiêu phần trăm review thấp. Một cụm có lift cao vẫn có thể có độ phủ nhỏ.

### 7.3. Kiểm tra độ ổn định qua nhiều lần chia

Quy trình sử dụng năm lần chia discovery và validation theo nhóm `text_norm`, với 20% số nhóm dành cho validation ở mỗi lần. Toàn bộ review có cùng nội dung chuẩn hóa được đưa về cùng phía trong một lần chia.

Ứng viên được tìm trên discovery, sau đó mới đo trên validation. Trong phiên bản EDA được thực nghiệm, cụm cần xuất hiện trong ít nhất 5 review thấp trên discovery và có lift discovery từ 2 trở lên. Một lần validation đủ điều kiện đánh giá khi cụm có support ít nhất 10.

Cụm được xem là ổn định khi được chọn ít nhất 4/5 lần, có ít nhất 3 lần đủ mẫu kiểm chứng và đạt lift validation từ 2 trở lên trong ít nhất 80% số lần đủ mẫu. Các phép đếm được xử lý theo lô 5.000 review để kiểm soát bộ nhớ.

Kết quả ghi nhận 1.834 n-gram ổn định trên Foody và 4.563 trên Google Maps, trong đó 984 cụm cùng ổn định ở cả hai nguồn. Năm lần chia là phép khảo sát độ nhạy với cách tách mẫu, không phải năm bộ dữ liệu độc lập.

### 7.4. So sánh cùng một keyword giữa hai nền tảng

Khi đo trên toàn bộ tập làm việc của từng nguồn, ba ví dụ cho kết quả:

| Cụm | P thấp khi có cụm ở Foody | Lift Foody | P thấp khi có cụm ở Maps | Lift Maps |
| --- | ---: | ---: | ---: | ---: |
| thiếu tôn trọng | 82,43% | 4,724 | 94,69% | 6,775 |
| phục vụ tệ | 75,54% | 4,329 | 92,83% | 6,642 |
| bao giờ quay | 91,44% | 5,241 | 95,74% | 6,851 |

Ở các ví dụ này, lift Google Maps cao hơn do cả tỷ lệ thấp khi có cụm cao hơn và baseline thấp hơn. Không thể khái quát rằng mọi keyword đều có lift Maps cao hơn. Chẳng hạn, “dịch vụ tốt” có lift khoảng 2,925 ở Foody nhưng chỉ 0,188 ở Maps.

Kết quả này cho thấy keyword liên hệ với rating không đồng nghĩa với một từ điển cảm xúc cố định. Cần đọc ngữ cảnh và xem cách nội dung được phân bố theo rating trên từng nguồn.

## 8. Modeling bằng TF-IDF và Logistic Regression trên hai nền tảng

### 8.1. Vai trò của mô hình và quy ước nhãn

Modeling chuyển nội dung review thành vector TF-IDF và học dự đoán nhóm rating thấp. Phân loại là nhiệm vụ trung gian để học mối liên hệ giữa văn bản và rating; sau huấn luyện, trọng số được phân tích để tìm keyword ứng viên và giải thích dự đoán.

Foody sử dụng nhóm thấp với 0 < rating ≤ 5 trên thang 0–10. Google Maps giữ rating gốc 1–5 sao để truy vết, nhân 2 về thang số chung và xác định nhóm thấp là 1–2 sao; 3–5 sao thuộc nhóm còn lại. Mỗi nguồn được huấn luyện một mô hình riêng theo cùng phương pháp. Các trường mã quán và rating chỉ phục vụ chuẩn bị, đối chiếu dữ liệu và tạo nhãn; đầu vào dự đoán chỉ là văn bản.

Notebook Google Maps đọc trực tiếp `data/raw/ggmap/gmap.json`, ánh xạ schema và lọc trùng bằng bốn trường nguồn, quán, rating và nội dung chuẩn hóa. Sau lọc, dữ liệu còn 130.270 review, trong đó 18.206 review thấp, chiếm 13,98%. Kết quả chuẩn bị dữ liệu khớp quy mô working trong EDA Google Maps.

### 8.2. Chia dữ liệu và kiểm soát giao nội dung

Train, validation và test được chia theo nhóm `text_norm`, với tỷ lệ nhóm xấp xỉ 60/20/20. Mỗi nội dung chuẩn hóa chỉ xuất hiện trong một tập của cùng thí nghiệm. Seed cố định là 42 cho lần tách test và 43 cho lần tách validation.

| Nguồn | Tập | Số review | Số nội dung khác nhau | Review thấp | Tỷ lệ thấp |
| --- | --- | --- | --- | --- | --- |
| Foody | train | 79.949 | 75.672 | 14.048 | 17,57% |
| Foody | validation | 26.205 | 25.225 | 4.489 | 17,13% |
| Foody | test | 26.486 | 25.225 | 4.607 | 17,39% |
| Google Maps | train | 78.614 | 74.813 | 10.905 | 13,87% |
| Google Maps | validation | 25.632 | 24.938 | 3.693 | 14,41% |
| Google Maps | test | 26.024 | 24.938 | 3.608 | 13,86% |

TF-IDF dùng n-gram 1–3, tần suất tài liệu tối thiểu 5, tối đa 100.000 đặc trưng và chuẩn hóa L2. Logistic Regression dùng `class_weight="balanced"`, solver `liblinear`, tối đa 2.000 vòng lặp và ngưỡng dự đoán 0,5.

Mỗi pipeline chỉ fit từ vựng, IDF và hệ số trên train khi thử cấu hình. Validation được transform bằng pipeline đó và chỉ dùng để chọn C. Các keyword EDA tính từ toàn bộ dataset không được dùng để lọc trước feature của mô hình.

### 8.3. Lựa chọn cấu hình bằng validation

| C | Macro-F1 validation Foody | Macro-F1 validation Google Maps |
| --- | --- | --- |
| 0,1 | 0,7838 | 0,8719 |
| 1,0 | 0,8095 | 0,8910 |
| 10,0 | 0,8146 | 0,8997 |

Foody chọn C = 10,0; Google Maps chọn C = 10,0 theo macro-F1 validation. Sau khi chọn, mỗi pipeline được fit lại trên train cộng validation của nguồn tương ứng: 106.154 review Foody và 104.246 review Google Maps. Tập test được dành cho đánh giá cuối, không dùng để chọn C, ngưỡng dự đoán hoặc keyword.

Mô hình cuối Google Maps có 100.000 đặc trưng. Cấu hình lưu kèm phiên bản thư viện: scikit-learn 1.7.2 cho lần chạy Foody và 1.9.1 cho lần chạy Maps. Sự khác biệt môi trường được ghi nhận khi đối chiếu hai thí nghiệm.

### 8.4. Kết quả test và baseline

Baseline luôn dự đoán lớp phổ biến trong development của từng nguồn. Các chỉ số được tính riêng trên test của nguồn tương ứng.

| Chỉ số test | Foody baseline | Foody Logistic | Maps baseline | Maps Logistic |
| --- | --- | --- | --- | --- |
| Accuracy | 0,8261 | 0,8774 | 0,8614 | 0,9489 |
| Macro-F1 | 0,4524 | 0,8076 | 0,4628 | 0,8981 |
| Precision nhóm thấp | 0,0000 | 0,6149 | 0,0000 | 0,7809 |
| Recall nhóm thấp | 0,0000 | 0,7903 | 0,0000 | 0,8772 |
| F1 nhóm thấp | 0,0000 | 0,6917 | 0,0000 | 0,8263 |
| ROC-AUC | 0,5000 | 0,9242 | 0,5000 | 0,9752 |
| Average precision nhóm thấp | 0,1739 | 0,7658 | 0,1386 | 0,8964 |

Baseline đạt accuracy cao do nhóm còn lại chiếm đa số nhưng không phát hiện review thấp. Logistic Regression nâng macro-F1 và F1 nhóm thấp trên cả hai nguồn, cho thấy văn bản có tín hiệu dự đoán nhóm rating vượt việc chỉ đoán theo phân bố lớp.

| Nguồn | Đúng nhóm còn lại | Báo nhầm thấp | Bỏ sót thấp | Phát hiện đúng thấp |
| --- | --- | --- | --- | --- |
| Foody | 19.599 | 2.280 | 966 | 3.641 |
| Google Maps | 21.528 | 888 | 443 | 3.165 |

Trên Google Maps, mô hình phát hiện đúng 3.165 trong 3.608 review thấp, bỏ sót 443 review thấp và báo nhầm 888 review thuộc nhóm còn lại thành nhóm thấp. Precision/recall nhóm thấp lần lượt là 78,09% và 87,72%. Có 7 review test không có đặc trưng trong từ vựng; các review này chỉ được dự đoán dựa trên bias.

### 8.5. Diễn giải so sánh

Macro-F1 test Foody là 0,8076, Google Maps là 0,8981. Chênh lệch điểm cần được đọc cùng tỷ lệ lớp, precision, recall và baseline của từng nguồn. Hai nguồn khác nhau về thang điểm gốc, quy ước nhóm thấp, độ dài review, khóa lọc trùng và môi trường chạy; vì vậy không quy toàn bộ chênh lệch cho một yếu tố riêng.

Đây là so sánh cùng phương pháp được huấn luyện riêng trên hai nền tảng, không phải thí nghiệm chuyển mô hình giữa Foody và Google Maps. Chia theo text_norm ngăn câu chuẩn hóa trùng qua các tập nhưng không tự bảo đảm khả năng tổng quát sang quán mới, thời điểm mới hoặc câu diễn đạt gần giống.

## 9. Phân tích trọng số và giải thích keyword

### 9.1. Kết hợp hệ số với support và lift

Logistic Regression tính logit bằng bias cộng tổng tích giữa TF-IDF và hệ số đặc trưng. Hệ số dương đẩy dự đoán về nhóm thấp; hệ số âm đẩy theo hướng ngược lại. Đóng góp thực tế của một đặc trưng trong review phụ thuộc đồng thời vào hệ số và TF-IDF của nó.

Ứng viên modeling có hệ số dương, support ít nhất 30 và low support ít nhất 10 trên development. Một bảng riêng áp dụng thêm lift từ 2 trở lên. Đây là các ngưỡng lọc đầu ra modeling, khác với ngưỡng tìm ứng viên EDA. Lift development là thống kê trong phần dữ liệu đã dùng fit mô hình, không phải kết quả kiểm chứng độc lập qua năm lần chia.

**Ví dụ ứng viên Foody xếp theo hệ số trong chính mô hình của nguồn đó:**

| Keyword | Hệ số | Support development | Low support | Lift development |
| --- | --- | --- | --- | --- |
| tệ | 13,454 | 3.089 | 2.135 | 3,958 |
| làm ăn | 10,376 | 829 | 574 | 3,965 |
| lần cuối | 8,105 | 412 | 317 | 4,406 |
| dở | 7,533 | 2.228 | 1.043 | 2,681 |
| thái độ | 7,365 | 3.817 | 1.985 | 2,978 |

**Ví dụ ứng viên Google Maps xếp theo hệ số trong chính mô hình của nguồn đó:**

| Keyword | Hệ số | Support development | Low support | Lift development |
| --- | --- | --- | --- | --- |
| tệ | 18,309 | 4.246 | 3.699 | 6,221 |
| dở | 11,099 | 1.593 | 1.093 | 4,900 |
| không | 10,300 | 38.843 | 7.137 | 1,312 |
| chém | 9,215 | 187 | 100 | 3,819 |
| dỡ | 8,673 | 126 | 110 | 6,234 |

Foody có 13.593 ứng viên hệ số dương đủ support, trong đó 3.144 đạt thêm lift từ 2. Google Maps có 8.062 ứng viên đủ support, trong đó 5.228 đạt thêm lift từ 2. Các con số này mô tả tập ứng viên theo tiêu chí định lượng, không tự xác nhận mọi cụm đều diễn đạt lời phàn nàn.

Không so sánh trực tiếp độ lớn hệ số giữa hai mô hình như cùng một thang đo. Từ vựng, IDF, regularization và các n-gram tương quan đều ảnh hưởng đến hệ số được học. Khi đối chiếu một keyword giữa hai nguồn, support, tỷ lệ thấp, lift và ngữ cảnh cần được xem cùng nhau.

### 9.2. Giải thích theo từng review

Cả hai notebook đều có chức năng liệt kê các feature thực sự xuất hiện trong review, TF-IDF, hệ số và tích đóng góp. Tổng đóng góp cộng bias được kiểm tra với decision_function; sigmoid của logit được đối chiếu với đầu ra predict_proba. Một feature không xuất hiện trong vector không đóng góp vào logit.

Quy trình đọc các review dự đoán sai có độ tự tin cao và lấy mẫu ngữ cảnh ở cả nhóm thấp lẫn nhóm còn lại. Việc này giúp xem xét rating lệch nội dung, câu khen chê hỗn hợp, tên riêng và mảnh câu có hệ số cao.

### 9.3. Kiểm tra phủ định trên Google Maps

Các câu sau là ví dụ chẩn đoán, không có nhãn rating và không tham gia fit hoặc tính điểm test.

| Câu chẩn đoán | Điểm lớp thấp của mô hình Maps | Dự đoán thấp |
| --- | --- | --- |
| Nhân viên thiếu tôn trọng khách. | 0,9930 | Có |
| Không hề có chuyện nhân viên thiếu tôn trọng khách. | 0,9985 | Có |
| Đồ ăn ngon nhưng nhân viên thiếu tôn trọng khách. | 0,8390 | Có |
| Phục vụ chậm. | 0,9378 | Có |
| Phục vụ không chậm. | 0,9952 | Có |

Ở Foody, mô hình cho điểm thấp khoảng 0,790 với câu “Nhân viên thiếu tôn trọng khách”, nhưng vẫn cho khoảng 0,815 với câu “Không hề có chuyện nhân viên thiếu tôn trọng khách”. Kết hợp các ví dụ ở hai nguồn cho phép quan sát độ nhạy với phủ định, nhưng không dùng vài câu chẩn đoán để kết luận mô hình hiểu đầy đủ ngữ cảnh.

Các đầu ra xác suất phụ thuộc cấu hình class_weight balanced và không mặc nhiên là xác suất đã hiệu chỉnh theo phân bố thực tế. Mỗi nguồn có phiếu 150 ngữ cảnh của top keyword, gồm trường xác định ý chê, khía cạnh và ghi chú, để phục vụ đánh giá ngữ nghĩa.

## 10. Kết quả đạt được và sản phẩm thực nghiệm

Quá trình triển khai hình thành các thành phần thu thập, lưu trữ và xử lý dữ liệu, cùng bộ phân tích liên kết nội dung review với nhóm rating thấp. Log vận hành ghi nhận các lô từ MinIO qua chuẩn hóa đến PostgreSQL; các notebook sử dụng snapshot dữ liệu để kiểm tra chất lượng, khám phá phân phối và thực hiện thí nghiệm.

Phân tích thống kê nhận diện các cụm có liên hệ với điểm thấp trên Foody và Google Maps. Hai mô hình Logistic Regression huấn luyện riêng bổ sung góc nhìn từ các đặc trưng cùng tham gia dự đoán. Kết quả macro-F1 test lần lượt là 0,8076 và 0,8981, vượt baseline lớp phổ biến tương ứng.

Kết luận nghiên cứu là văn bản có tín hiệu dự đoán rating và hỗ trợ tìm keyword ứng viên. Mối liên hệ thống kê, hệ số mô hình và các ví dụ giải thích không tự chứng minh quan hệ nhân quả hoặc xác nhận ngữ nghĩa của toàn bộ danh sách keyword.

Sản phẩm gồm notebook EDA Foody, notebook Google Maps và so sánh, hai notebook modeling, hai bundle mô hình, cấu hình, bảng trọng số, danh sách keyword, dự đoán test và phiếu đọc ngữ cảnh. Lần chạy Foody được lưu ngày 13/11/2026; lần chạy Google Maps được lưu ngày 15/11/2026. Mỗi lần chạy lưu seed, phiên bản thư viện, phân chia dữ liệu và SHA-256 nguồn để hỗ trợ đối chiếu và tái lập.

## Phụ lục nguồn đối chiếu

| Nội dung | Tệp hoặc vị trí trong dự án |
| --- | --- |
| Thu thập URL | `src/ingestion/foody/get_urls.py` |
| Thu thập review | `src/ingestion/foody/crawler_extended.py` |
| Upload và checkpoint | `src/ingestion/minio_upload.py` |
| Xử lý từng lô | `src/ingestion/batch_pipeline.py` |
| Chuẩn hóa và hợp đồng dữ liệu | `src/processing/foody/basic_pipeline.py`, `schema.py` |
| Schema và nạp PostgreSQL | `configs/postgres_schema.sql`, `src/ingestion/postgres_load.py` |
| Xuất dữ liệu | `src/ingestion/db_to_json.py` |
| Log vận hành | `src/ingestion/foody/extended/checkpoints/ingestion_logs/events.jsonl` |
| EDA Foody | `notebooks/eda._present.ipynb` |
| EDA Google Maps và đối chiếu | `notebooks/new_ggmap11.ipynb` |
| Kết quả chạy phần so sánh | `outputs/ggmap_revision2/new_ggmap11_executed.ipynb` |
| Modeling Foody | `notebooks/logistic_keyword_workflow.ipynb` |
| Modeling Google Maps | `notebooks/logistic_keyword_ggmap_workflow.ipynb` |
| Kết quả modeling Foody | `notebooks/outputs/logistic_keywords/20261113T145627_018248Z/` |
| Kết quả modeling Google Maps | `notebooks/outputs/logistic_keywords_ggmap/20261115T093431_080713Z/` |
