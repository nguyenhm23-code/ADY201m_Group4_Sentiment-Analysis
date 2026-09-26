import json
import time
import random 
import os
import undetected_chromedriver as uc 
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Khai báo đường dẫn lưu Profile Chrome để giữ nguyên tài khoản đăng nhập
PROFILE_DIR = os.path.join(os.getcwd(), "chrome_profile")
# Khai báo thư mục chứa các file .txt cho gọn gàng
CHECKPOINT_DIR = "checkpoints"
os.makedirs(CHECKPOINT_DIR, exist_ok=True)

def get_restaurant_urls(category_url, target_count=300, profile_path=PROFILE_DIR):
    options = uc.ChromeOptions()
    # Truyền user_data_dir vào để lưu cookie/tài khoản sau khi đăng nhập
    driver = uc.Chrome(options=options, user_data_dir=profile_path, version_main=154) 

    driver.get(category_url) 
    restaurant_urls = []

    print(f"\n ---Bắt đầu thu thập link cho {category_url} ---")
    
    # LƯU Ý: Nếu là lần chạy đầu tiên chưa có tài khoản, hãy bỏ comment dòng dưới 
    # để có 60s tự đăng nhập bằng tay vào Foody. Lần sau chạy thì comment lại.
    # print("Đang chờ 60s để bạn đăng nhập thủ công (nếu cần)...")

    try:
        while len(restaurant_urls) < target_count:
            # 1. Cuộn xuống cuối trang
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(2.0, 3.0)) 
            
            # 2. Tìm và click nút "Xem thêm" nếu nó xuất hiện (thử nhiều kiểu selector)
            # Selector đầu tiên khớp CHÍNH XÁC với nút Angular thật trên Foody:
            # <a class="fd-btn-more" ng-click="LoadMore()"><label>Xem thêm</label></a>
            MORE_BUTTON_XPATHS = [
                "//a[contains(@class, 'fd-btn-more')]",
                "//*[@ng-click='LoadMore()']",
                "//*[contains(translate(., 'XEM THÊM', 'xem thêm'), 'xem thêm')]",  # bắt cả trường hợp text nằm trong label/span con
                "//button[contains(translate(text(), 'XEM THÊM', 'xem thêm'), 'xem thêm')]",
                "//*[contains(@class, 'more') or contains(@class, 'load-more') or contains(@class, 'btn-more')]",
            ]

            clicked = False
            for xpath in MORE_BUTTON_XPATHS:
                try:
                    xem_them_btn = driver.find_element(By.XPATH, xpath)
                    btn_class = xem_them_btn.get_attribute("class")
                    btn_visible = xem_them_btn.is_displayed()
                    print(f"    [DEBUG] Tìm thấy ứng viên nút 'Xem thêm' -> tag={xem_them_btn.tag_name} class='{btn_class}' visible={btn_visible} (selector: {xpath})")

                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", xem_them_btn)

                    # Đợi tối đa 5s cho nút thực sự clickable (Angular có thể chưa gắn xong event listener)
                    try:
                        WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    except:
                        pass  # Không sao, vẫn thử click bằng JS bên dưới

                    # Thử click kiểu "thật" (native) trước, nếu lỗi thì fallback sang JS click
                    try:
                        xem_them_btn.click()
                        print("    [DEBUG] Đã click nút 'Xem thêm' (native click)...")
                    except Exception as click_err:
                        print(f"    [DEBUG] Native click lỗi ({click_err}), thử JS click...")
                        driver.execute_script("arguments[0].click();", xem_them_btn)
                        print("    [DEBUG] Đã click nút 'Xem thêm' (JS click)...")

                    time.sleep(random.uniform(2.0, 3.0))
                    clicked = True
                    break
                except Exception as e:
                    print(f"    [DEBUG] Selector '{xpath}' không match hoặc lỗi: {e}")
                    continue

            if not clicked:
                print("    [DEBUG] Không tìm thấy/không click được nút 'Xem thêm' ở vòng lặp này (đã thử hết các selector).")

            link_elements = driver.find_elements(By.CSS_SELECTOR, "a.ng-binding")
            previous_count = len(restaurant_urls) 
            for elem in link_elements:
                link = elem.get_attribute('href')
                # CHỈ LẤY LINK QUÁN: Loại bỏ các link có chứa chữ /binh-luan
                if link and (link not in restaurant_urls) and ('/binh-luan' not in link):
                    restaurant_urls.append(link)
            
            print(f"Đã thu thập được: {len(restaurant_urls)}/{target_count} links")

            if len(restaurant_urls) == previous_count:
                if not clicked:
                    print("Đã cuộn đến cuối trang, không click được nút 'Xem thêm' và không tìm thấy thêm nhà hàng.")
                else:
                    print("Đã click 'Xem thêm' nhưng không có link mới nào xuất hiện.")
                break 
            
        return restaurant_urls[:target_count]
    finally:
        driver.quit()

def scrape_foody_to_json(url, driver):
    scraped_data = []
    
    # --- LÀM SẠCH URL ---
    clean_url = url.split('?')[0].rstrip('/')
    
    # Nếu link lỡ dính đuôi /binh-luan-xxxx thì cắt bỏ toàn bộ phần đó để lấy link gốc quán
    if '/binh-luan' in clean_url:
        clean_url = clean_url.split('/binh-luan')[0]
        
    review_endpoint = f"{clean_url}/binh-luan"
        
    url_parts = clean_url.split('/') 
    city = url_parts[3] if len(url_parts) > 3 else "khong ro"
    id_restaurant = url_parts[4] if len(url_parts) > 4 else "khong ro"

    try:
        # Truy cập thẳng vào trang danh sách bình luận
        driver.get(review_endpoint) 
        time.sleep(random.uniform(3.0, 4.5)) 
        print(f"  -> Đang tải trang: {review_endpoint}")

        # --- BƯỚC 2: Liên tục tải thêm bình luận + bóc tách ngay sau mỗi lần load ---
        # Lưu vào dict theo key duy nhất để: (a) không trùng lặp, (b) không mất data
        # nếu Foody thay thế (replace) DOM thay vì cộng dồn (append) khi chuyển trang.
        seen_reviews = {}

        def extract_current_reviews():
            """Bóc tách toàn bộ review đang có trên DOM tại thời điểm gọi hàm."""
            current_reviews = driver.find_elements(By.CSS_SELECTOR, 'li.review-item')
            for review in current_reviews:
                try:
                    user = review.find_element(By.CSS_SELECTOR, 'a.ru-username').text.strip()
                except:
                    continue  # Không có username thì bỏ qua, khả năng cao là item lỗi/rỗng

                try: rating = review.find_element(By.CSS_SELECTOR, 'div.review-points span').text.strip()
                except: rating = "Không có điểm"
                try: device = review.find_element(By.CSS_SELECTOR, 'a.ru-device').text.strip().replace('via', '').strip()
                except: device = "Không rõ thiết bị"
                try: post_time = review.find_element(By.CSS_SELECTOR, 'span.ru-time').text.strip()
                except: post_time = "Không rõ"
                try:
                    comment_elem = review.find_element(By.CSS_SELECTOR, 'div.rd-des span')
                    comment = comment_elem.get_attribute("textContent").strip()
                except:
                    comment = "Không bình luận"

                # Key duy nhất cho 1 review: dùng user + thời gian + 50 ký tự đầu comment
                key = f"{user}|{post_time}|{comment[:50]}"
                if key not in seen_reviews:
                    seen_reviews[key] = {
                        "Thành Phố": city,
                        "ID Quán": id_restaurant,
                        "URL Quán": clean_url,
                        "Tên User": user,
                        "Điểm Đánh Giá": rating,
                        "Thiết Bị": device,
                        "Ngày Giờ": post_time,
                        "Bình Luận": comment
                    }
            return len(current_reviews)

        # Nhiều kiểu selector khác nhau cho nút "tải thêm" / "trang sau", vì Foody
        # có thể đổi class hoặc chuyển từ nút "Xem thêm" sang phân trang dạng số.
        MORE_BUTTON_XPATHS = [
            "//*[contains(@class, 'fd-btn-more')]",
            "//*[contains(translate(., 'XEM THÊM', 'xem thêm'), 'xem thêm')]",
            "//a[contains(@class,'paging') and contains(@class,'next')]",
            "//a[contains(@class,'pagingNext')]",
            "//li[contains(@class,'active')]/following-sibling::li[1]/a",
        ]

        last_review_count = 0
        stuck_count = 0
        STUCK_LIMIT = 2       # tăng ngưỡng để chịu được mạng/server chậm
        MAX_LOOPS = 500       # phanh an toàn tuyệt đối, tránh vòng lặp vô hạn nếu logic có sai sót
        loop_index = 0

        # Bóc tách ngay batch đầu tiên trước khi vào vòng lặp load thêm
        extract_current_reviews()

        while True:
            loop_index += 1
            if loop_index > MAX_LOOPS:
                print(f"    [DEBUG] Đạt giới hạn an toàn {MAX_LOOPS} vòng lặp, dừng để tránh treo vô hạn.")
                break

            # Cuộn xuống cuối trang
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(2.0, 3.0))

            # Thử lần lượt các selector nút "xem thêm" / "trang sau"
            clicked = False
            for xpath in MORE_BUTTON_XPATHS:
                try:
                    more_btn = driver.find_element(By.XPATH, xpath)

                    # Chẩn đoán: in thuộc tính của nút để biết nó có bị disable/ẩn ngầm hay không
                    try:
                        btn_class = more_btn.get_attribute("class")
                        btn_disabled = more_btn.get_attribute("disabled")
                        btn_visible = more_btn.is_displayed()
                        print(f"    [DEBUG] Nút tìm thấy -> class='{btn_class}' disabled='{btn_disabled}' visible={btn_visible}")
                    except:
                        pass

                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", more_btn)
                    time.sleep(1)
                    driver.execute_script("arguments[0].click();", more_btn)
                    print(f"    [DEBUG] Đã click nút theo selector: {xpath}")
                    time.sleep(random.uniform(2.5, 4.0))  # Chờ data mới đổ về sau khi bấm
                    clicked = True
                    break
                except:
                    continue

            if not clicked:
                print("    [DEBUG] Không tìm thấy nút tải thêm nào ở vòng lặp này.")

            # Bóc tách ngay sau mỗi lần load, để không mất data nếu trang bị thay thế
            current_count = extract_current_reviews()
            total_collected = len(seen_reviews)

            # QUAN TRỌNG: tính "kẹt" dựa trên việc số lượng có THỰC SỰ tăng hay không,
            # bất kể có click được nút hay không -- vì click "thành công" (không lỗi)
            # không có nghĩa là nút đó còn tác dụng (có thể đã bị disable/ẩn ngầm
            # nhưng selector vẫn match do còn sót lại text/class cũ trên DOM).
            if current_count == last_review_count:
                stuck_count += 1
                print(f"    [DEBUG] Số lượng không đổi ({current_count}) sau khi click={clicked} -> stuck_count={stuck_count}/{STUCK_LIMIT}")
                # Mồi load trang bằng cách cuộn ngược lên một chút rồi cuộn lại xuống đáy
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight - 600);")
                time.sleep(1)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)

                # Nếu nhiều lần thử mà số lượng bình luận không tăng thêm -> Đã cạn data (hoặc bị site giới hạn)
                if stuck_count >= STUCK_LIMIT:
                    print(f"    [DEBUG] Dừng thu thập: số lượng bình luận không tăng sau {STUCK_LIMIT} lần thử liên tiếp.")
                    break
            else:
                stuck_count = 0
                last_review_count = current_count
                print(f"    + DOM hiện có {current_count} thẻ | Đã gom được tổng cộng {total_collected} bình luận duy nhất...")

        # --- BƯỚC 3: Gộp toàn bộ review đã gom được (từ mọi lần load) ---
        scraped_data = list(seen_reviews.values())
        print(f"  => Đã tải xong đáy trang. Thu thập được tổng cộng {len(scraped_data)} bình luận.")
                
    except Exception as e:
        print(f"Lỗi ở link {review_endpoint} : {e}") 

    return scraped_data

if __name__ == "__main__":
    category_urls = [ 
        "https://www.foody.vn/ho-chi-minh", 
        "https://www.foody.vn/ha-noi", 
        "https://www.foody.vn/can-tho",
        "https://www.foody.vn/khanh-hoa",
        "https://www.foody.vn/vung-tau",
        "https://www.foody.vn/hai-phong",
        "https://www.foody.vn/binh-thuan"
    ]

    for category_url in category_urls:
        city_name = category_url.strip('/').split('/')[-1]
            
        file_name = f"foody_{city_name}_dataset.json"
        
        # Lưu các file txt vào trong thư mục checkpoints
        links_file = os.path.join(CHECKPOINT_DIR, f"foody_{city_name}_links.txt")
        progress_file = os.path.join(CHECKPOINT_DIR, f"foody_{city_name}_progress.txt")

        # --- CHECKPOINT 1: Khôi phục danh sách Link ---
        if os.path.exists(links_file):
            print(f"\n[Checkpoint] Tìm thấy file {links_file}, đang đọc danh sách links...")
            with open(links_file, 'r', encoding='utf-8') as f:
                urls_to_scrape = [line.strip() for line in f if line.strip()]
        else:
            urls_to_scrape = get_restaurant_urls(category_url, target_count=300, profile_path=PROFILE_DIR)
            with open(links_file, 'w', encoding='utf-8') as f:
                for u in urls_to_scrape:
                    f.write(f"{u}\n")

        # --- CHECKPOINT 2: Nạp JSON cũ ---
        all_dataset = []
        if os.path.exists(file_name):
            try:
                with open(file_name, 'r', encoding='utf-8') as f:
                    all_dataset = json.load(f)
                print(f"[Checkpoint] Đã nạp {len(all_dataset)} bình luận cũ từ {file_name}")
            except json.JSONDecodeError:
                pass

        # --- CHECKPOINT 3: Nạp danh sách ĐÃ CÀO XONG ---
        processed_links = set()
        if os.path.exists(progress_file):
            with open(progress_file, 'r', encoding='utf-8') as f:
                processed_links = set(line.strip() for line in f if line.strip())

        # Sử dụng chung Profile Chrome cho driver chính
        main_driver = uc.Chrome(user_data_dir=PROFILE_DIR, version_main=154) 
        total_links = len(urls_to_scrape)

        for index, url in enumerate(urls_to_scrape):
            if url in processed_links:
                print(f"[{index + 1}/{total_links}] (Bỏ qua) Đã cào: {url}")
                continue

            print(f"[{index + 1}/{total_links}] Đang cào: {url}")
            data_of_one_res = scrape_foody_to_json(url, main_driver) 
            
            if data_of_one_res:
                all_dataset.extend(data_of_one_res) 
                with open(file_name, 'w', encoding= 'utf-8') as file:
                    json.dump(all_dataset, file, ensure_ascii=False, indent=4)
            
            with open(progress_file, 'a', encoding='utf-8') as f:
                f.write(f"{url}\n")
            processed_links.add(url)
            
            time_sleep = random.uniform(3.5, 6.5) 
            print(f"-> Đã update. Nghỉ {time_sleep:.2f}s")
            time.sleep(time_sleep) 

        main_driver.quit() 
        print(f"Hoàn tất thành phố {city_name}")