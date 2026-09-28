# Data dictionary — schema 1.1.0

Grain: một phiên bản review được chọn theo ID nguồn + quán. Review không có nội dung vẫn ở master. Chuỗi thiếu thường là JSON null, không thay bằng “unknown” trong bản ghi. `reviews.jsonl` và `record_json` trong SQLite có cùng schema.

| Trường | Kiểu | Định nghĩa |
|---|---|---|
| schema_version | string | Phiên bản schema/pipeline |
| review_id | string | ID có namespace nguồn, hash định danh review trong quán |
| source | string | foody hoặc gmap |
| native_review_id | string/null | ID review từ crawler nếu có |
| restaurant_id | string | ID quán chuẩn hóa, namespace nguồn; chưa entity-match tự động |
| native_restaurant_id | string/null | ID quán crawler cung cấp |
| restaurant_url | string/null | URL gốc quán để truy vết |
| restaurant_name | string/null | Tên quán nếu crawler cung cấp; không suy diễn từ slug |
| address | string/null | Địa chỉ crawler cung cấp |
| city | string/null | Tên thành phố chuẩn hóa từ bằng chứng nhận diện được |
| city_raw | string/null | Thành phố trong raw |
| city_source | string/null | record, restaurant_url hoặc address |
| search_area | string[] | Danh sách khu vực tìm kiếm của crawler, có thể khác city; thiếu thì [] |
| text_raw | string | Bình luận nguyên gốc, nội bộ vì có thể chứa thông tin cá nhân |
| text_clean | string | Unicode NFC, làm sạch và che mẫu thông tin liên hệ; giữ phủ định, emoji |
| text_sha256 | string | Hash chính xác text_clean, khóa kiểm tra nhãn có còn ứng với nội dung |
| language_hint | string | vi nếu tìm thấy ký tự tiếng Việt đặc trưng; und nếu chưa xác định; không phải language detector |
| rating | number/null | Điểm review hợp lệ ở thang gốc |
| rating_raw | number/string/null | Điểm review trước parse |
| rating_scale | integer | 10 Foody, 5 Maps |
| rating_fraction | number/null | rating/rating_scale; metadata, không dùng làm X cho sentiment |
| restaurant_rating | number/null | Điểm tổng hợp quán nếu có, tách khỏi rating review |
| published_at | ISO 8601/null | Ngày đăng parse được đổi về UTC+7; đọc cùng date_precision |
| published_at_raw | string/null | Chuỗi thời gian crawler cung cấp |
| date_precision | string | datetime, second, minute, day, relative, edited_or_relative, unknown |
| collected_at | ISO 8601/null | Thời điểm thu thập nếu parse được, đổi về UTC+7; không thay thế ngày đăng |
| device | string/null | Thiết bị đăng review nếu nguồn cung cấp; không tự đoán |
| weak_sentiment | string/null | Nhãn heuristic từ rating: negative, neutral, positive |
| sentiment | string/null | Nhãn người: negative, neutral, positive, mixed, uncertain |
| label_source | string/null | human nếu đã nhập nhãn sentiment người; null nếu chưa |
| annotator | string (optional) | Mã người gán nhãn, chỉ xuất hiện sau import annotation |
| aspects | array | Danh sách {aspect,sentiment} do người gán |
| training_eligible | boolean | Có nội dung hữu ích và không là bản sao dài trong cùng quán |
| duplicate_of | string/null | review_id được giữ cho training nếu bản ghi là bản sao dài cùng quán |
| split | string | train, validation hoặc test theo nhóm quán và trùng văn bản |
| split_group_id | string | ID nhóm liên thông dùng để giữ cùng một split |
| quality_flags | string[] | Vấn đề/giả định: thiếu city, rating sai, ngày giả định timezone, trùng, v.v. |
| provenance | string[] | Các đường dẫn input tương đối trong raw đã đóng góp vào bản ghi |

File model input gồm `review_id`, `restaurant_id`, `split_group_id`, `source`, `text`, `split`, `label`, `sentiment`, `label_source`. Chỉ text và label là X/y; các ID/nguồn/split dùng kiểm tra và đánh giá theo lát cắt. Label source trong weak_train là `rating_heuristic`; trong ba file có nhãn người là `human`.

SQLite chỉ đưa những cột thường truy vấn ra ngoài `record_json`; các trường còn lại vẫn lưu đầy đủ bên trong JSON. `training_eligible` là 0/1 trong SQL. View `monthly_rating_observations` gộp theo source, city, tháng lấy từ ISO; count là số review có ngày và rating hợp lệ, không phải số lượt khách hoặc tổng review trên nền tảng.
