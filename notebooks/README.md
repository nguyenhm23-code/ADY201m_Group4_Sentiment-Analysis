# Notebook và RStudio

Từ gốc dự án: `python -m pip install -r requirements-analysis.txt`, rồi `python -m jupyter lab`.

- `1_Exploration.ipynb`: SQL read-only kiểm tra số lượng, thiếu dữ liệu, split; hai biểu đồ coverage và rating theo nguồn.
- `2_Modeling.ipynb`: mặc định kiểm tra readiness; đổi MODE mới chạy train. Không fake metrics khi chưa có nhãn người.
- `3_EDA.Rmd`: cùng nguồn SQLite, dành cho RStudio. Chưa chạy R trong môi trường rà soát; cần DBI, RSQLite, jsonlite, ggplot2, rmarkdown.

Chạy `python src/processing/cleaner.py` trước nếu chưa có `data/processed/latest.json`. Notebook luôn dùng run_id và đường dẫn tương đối trong repo, không phụ thuộc đường dẫn Windows ghi trong latest.json. Không mở notebook bên ngoài cây thư mục dự án.

Hai notebook Python đã được chạy/kiểm tra theo trạng thái ghi trong báo cáo audit. Các output là số liệu tổng hợp của snapshot lúc chạy, không tự đổi khi crawler cập nhật; dùng Run All để cập nhật. Không coi số review là số khách hoặc rating heuristic là nhãn người.
