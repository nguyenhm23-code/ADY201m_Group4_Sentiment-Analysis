import csv
import json

# Tên file đầu vào (file cũ giữ nguyên) và file đầu ra (file mới)
CSV_FILE = "data/data_member_1_mien_bac.csv"
JSON_FILE = "data/data_member_1_mien_bac.json"

def convert_csv_to_json():
    # Bước 1: Đọc toàn bộ dữ liệu từ file CSV cũ
    data = []
    try:
        # Dùng utf-8-sig để đọc tiếng Việt không bị lỗi font
        with open(CSV_FILE, mode='r', encoding='utf-8-sig') as csv_file:
            # DictReader tự động lấy dòng đầu tiên (8 trường) làm Key
            csv_reader = csv.DictReader(csv_file)
            for row in csv_reader:
                data.append(row)
                
        # Bước 2: Ghi dữ liệu ra file JSON mới
        with open(JSON_FILE, mode='w', encoding='utf-8') as json_file:
            # ensure_ascii=False để giữ tiếng Việt có dấu
            # indent=4 sẽ tự động ngắt dòng, tạo ra cấu trúc 8-9 dòng cho mỗi bản ghi
            json.dump(data, json_file, ensure_ascii=False, indent=4)
            
        print(f"Thành công! Đã chuyển đổi {len(data)} bản ghi sang file {JSON_FILE}")
        print(f"File gốc {CSV_FILE} vẫn được giữ nguyên an toàn.")
        
    except FileNotFoundError:
        print(f"[!] Không tìm thấy file {CSV_FILE}. Hãy kiểm tra lại tên file.")

if __name__ == "__main__":
    convert_csv_to_json()