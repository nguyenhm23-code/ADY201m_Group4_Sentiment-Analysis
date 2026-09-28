# Hai baseline phân loại cảm xúc

`model.py` dùng TF-IDF ký tự 2–5 gram và hai bộ phân loại: Logistic Regression, Linear SVM. Input chỉ là text; không đưa số sao, ID quán hoặc nguồn vào đặc trưng. Vectorizer chỉ fit trên train. Tham số model cố định để tạo baseline đầu tiên, chưa tối ưu siêu tham số.

```powershell
python -m pip install -r requirements.txt
python src/modeling/model.py --weak-baseline
```

Lệnh trên chỉ train hai model thử từ `weak_train.jsonl`, lưu `.joblib` và `metrics.json` có trạng thái `weak_baseline_not_evaluated`; không tạo accuracy/F1 giả từ nhãn rating.

Sau khi gán nhãn người và chạy lại cleaner:

```powershell
python src/modeling/model.py --snapshot data/processed/runs/RUN_ID
```

Mặc định đọc run_id từ `data/processed/latest.json` nếu không truyền snapshot. Model yêu cầu train/validation/test có đủ ba lớp nhãn người; kiểm tra ID trùng, quán/nhóm trùng qua các tập, và bình luận dài trùng qua các tập. Các kiểm tra này là điều kiện tối thiểu, chưa đảm bảo kích thước hoặc tính đại diện của dữ liệu.

Hai model được so sánh bằng macro-F1 trên validation. Chọn model bằng validation; chỉ model được chọn được chấm trên test, có confusion matrix, F1/precision/recall từng lớp và kết quả theo nguồn. Không lặp chỉnh model dựa vào test. Mỗi lần chạy tạo thư mục riêng trong `data/models/`, lưu run_id dataset, hash file input, phiên bản scikit-learn và seed.

Dataset hiện chưa có nhãn người nên lệnh mặc định sẽ dừng với thông báo cần gán nhãn. Model thử chưa đủ để kết luận chất lượng dự đoán cảm xúc thực tế. Hai baseline này chưa giải quyết dự báo xu hướng theo thời gian; dữ liệu Maps hiện chỉ có ngày tương đối.

Tài liệu API đã đối chiếu: [TfidfVectorizer](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html), [LogisticRegression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html), [LinearSVC](https://scikit-learn.org/stable/modules/generated/sklearn.svm.LinearSVC.html).
