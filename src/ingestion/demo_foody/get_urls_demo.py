import os
import time
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

URL_FILE = "urls.txt"
TARGET_TOTAL_URLS = 1500 
CITIES = ["ha-noi", "hai-phong", "quang-ninh", "bac-ninh", "can-tho", "khanh-hoa", "binh-duong", "dong-nai", "hue", "dien-bien", "vung-tau"]  # Mở rộng thêm các tỉnh thành lớn để gom đủ số lượng link khủng

# 4 danh mục có số lượng quán lớn nhất trên Foody
CATEGORIES = [
    "quan-an",        
    "cafes-giai-tri", 
    "an-vat-via-he",  
    "nha-hang"        
]

# Danh sách đen loại bỏ link hệ thống, menu rác
BLACK_LIST = [
    "o-dau", "giao-hang", "an-gi", "suu-tap", "bo-suu-tap", "binh-luan", "blogs", "khuyen-mai", 
    "ung-dung-mobile", "bao-mat-thong-tin", "quy-dinh", "lien-he", "top-thanh-vien", "bai-viet", 
    "su-kien", "bang-xep-hang", "dia-diem", "dang-nhap", "dang-ky", "hinh-anh", "khu-vuc", 
    "video", "coupon", "thu-vien", "danh-sach", "shopeefood"
]

def extract_links_to_file():
    existing_urls = set()
    if os.path.exists(URL_FILE):
        with open(URL_FILE, "r", encoding="utf-8") as f:
            existing_urls = {line.strip() for line in f if line.strip()}

    print(f"[*] Khởi động trình duyệt trinh sát... (Đang có sẵn {len(existing_urls)} link)")
    driver = uc.Chrome()
    
    try:
        for city in CITIES:
            for category in CATEGORIES:
                if len(existing_urls) >= TARGET_TOTAL_URLS:
                    break
                    
                cat_url = f"https://www.foody.vn/{city}/{category}"
                print(f"\n--- Đang quét: {cat_url} ---")
                
                try:
                    driver.get(cat_url)
                    time.sleep(3.5)

                    for _ in range(15):
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                        time.sleep(1.5)

                    elements = driver.find_elements(By.TAG_NAME, "a")
                    found_in_session = 0
                    
                    with open(URL_FILE, "a", encoding="utf-8") as file:
                        for elem in elements:
                            try:
                                href = elem.get_attribute('href')
                                if not href or f"/{city}/" not in href:
                                    continue
                                    
                                clean_link = href.split('?')[0].rstrip('/')
                                parts = clean_link.split(f"/{city}/")
                                
                                if len(parts) == 2:
                                    slug = parts[1]
                                    if "/" not in slug and "-" in slug and slug not in BLACK_LIST:
                                        if clean_link not in existing_urls:
                                            existing_urls.add(clean_link)
                                            file.write(clean_link + "\n")
                                            found_in_session += 1
                                            
                                            if len(existing_urls) >= TARGET_TOTAL_URLS:
                                                break
                            except:
                                continue
                    
                    print(f" -> Nhặt thêm {found_in_session} link. Tổng kho: {len(existing_urls)}/{TARGET_TOTAL_URLS}")
                except Exception as e:
                    print(f"[!] Lỗi kết nối danh mục: {e}")
                
    finally:
        driver.quit()
        print(f"\n[+] HOÀN TẤT! File '{URL_FILE}' đã sẵn sàng.")

if __name__ == "__main__":
    extract_links_to_file()