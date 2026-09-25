import csv
import os
import time
import random
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

CSV_FILE = "data_member_1_mien_bac.csv"
FIELDNAMES = ["Thành Phố", "ID Quán", "URL Quán", "Tên User", "Điểm Đánh Giá", "Thiết Bị", "Ngày Giờ", "Bình Luận"]
TARGET_ROWS = 35000

def get_current_row_count():
    if not os.path.exists(CSV_FILE):
        return 0
    try:
        with open(CSV_FILE, mode="r", encoding="utf-8-sig", errors="ignore") as f:
            return sum(1 for row in csv.reader(f) if row and len(row) >= 8) - 1
    except Exception:
        return 0

def scrape_foody_to_csv(url, driver, seen_feedbacks):
    scraped_data = []
    url_parts = url.strip('/').split('/')
    city = url_parts[3] if len(url_parts) > 3 else "mien-bac"
    id_restaurant = url_parts[4] if len(url_parts) > 4 else url.split('/')[-1]
    
    review_url = f"{url}/binh-luan" if not url.endswith("binh-luan") else url

    try:
        driver.get(review_url)
        time.sleep(random.uniform(3.0, 4.5))

        no_new_review_count = 0
        last_review_count = 0
        
        print(f" -> Đang tải sâu toàn bộ bình luận quán {id_restaurant}...")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.0)
            try:
                btn_more = driver.find_element(By.CSS_SELECTOR, 'a.fd-btn-more, a.btn-load-more, a.more-review')
                if btn_more.is_displayed():
                    driver.execute_script("arguments[0].click();", btn_more)
                    time.sleep(random.uniform(1.5, 2.5))
            except:
                pass

            current_reviews = len(driver.find_elements(By.CSS_SELECTOR, 'li.review-item'))
            if current_reviews == last_review_count:
                no_new_review_count += 1
                if no_new_review_count >= 4:
                    break
            else:
                no_new_review_count = 0
                last_review_count = current_reviews

        reviews = driver.find_elements(By.CSS_SELECTOR, 'li.review-item')
        for review in reviews:
            try:
                user = review.find_element(By.CSS_SELECTOR, 'a.ru-username').text.strip()
                try: rating = review.find_element(By.CSS_SELECTOR, 'div.review-points span').text.strip()
                except: rating = "N/A"
                try: device = review.find_element(By.CSS_SELECTOR, 'a.ru-device').text.strip().replace('via', '').strip()
                except: device = "Web"
                try: post_time = review.find_element(By.CSS_SELECTOR, 'span.ru-time').text.strip()
                except: post_time = "N/A"
                try:
                    comment_elem = review.find_element(By.CSS_SELECTOR, 'div.rd-des span')
                    comment = comment_elem.get_attribute("textContent").strip()
                except:
                    comment = ""

                comment = comment.replace('\n', ' ').replace('\r', ' ')
                if comment and comment != "Xem thêm":
                    dedup_key = (id_restaurant, user, comment)
                    if dedup_key not in seen_feedbacks:
                        seen_feedbacks.add(dedup_key)
                        scraped_data.append({
                            "Thành Phố": city,
                            "ID Quán": id_restaurant,
                            "URL Quán": url,
                            "Tên User": user,
                            "Điểm Đánh Giá": rating,
                            "Thiết Bị": device,
                            "Ngày Giờ": post_time,
                            "Bình Luận": comment
                        })
            except:
                continue

    except Exception as e:
        print(f"[!] Lỗi ở link {url}: {e}")

    if scraped_data:
        with open(CSV_FILE, mode="a", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            writer.writerows(scraped_data)
            
    return len(scraped_data)

if __name__ == "__main__":
    url_file = "urls.txt"
    
    if not os.path.exists(url_file):
        print(f"[!] Không tìm thấy file '{url_file}'. Hãy chạy file get_urls.py trước.")
        exit()

    with open(url_file, "r", encoding="utf-8") as f:
        target_urls = [line.strip() for line in f if line.strip() and line.strip().startswith("http")]

    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()

    current_rows = max(0, get_current_row_count())
    seen_feedbacks = set()
    
    main_driver = uc.Chrome()

    try:
        print(f"=== BẮT ĐẦU CÀO DATA | TỔNG LINK: {len(target_urls)} | HIỆN TẠI: {current_rows}/{TARGET_ROWS} ===")
        
        for index, url in enumerate(target_urls):
            if current_rows >= TARGET_ROWS:
                print(f"\n[!] Đã hoàn thành mục tiêu {TARGET_ROWS} dòng dữ liệu!")
                break
                
            # Bộ lọc né link rác, link chuyển hướng sang ShopeeFood
            skip_keywords = ["bo-suu-tap", "hinh-anh", "filter", "danh-sach", "#", "o-dau", "shopeefood.vn"]
            if any(keyword in url for keyword in skip_keywords):
                print(f"\n[{index + 1}/{len(target_urls)}] Bỏ qua link không hợp lệ: {url}")
                continue

            print(f"\n[{index + 1}/{len(target_urls)}] Đang cào quán: {url.split('/')[-1]}")
            added = scrape_foody_to_csv(url, main_driver, seen_feedbacks)
            current_rows = max(0, get_current_row_count())
            
            print(f" => Thu được {added} feedbacks. Tổng tiến độ hiện tại: {current_rows}/{TARGET_ROWS}")
            time.sleep(random.uniform(3.0, 5.0)) # Độ trễ an toàn chống quét bot

    finally:
        main_driver.quit()
        print(f"\n=== HOÀN TẤT PHIÊN CÀO DATA ===")