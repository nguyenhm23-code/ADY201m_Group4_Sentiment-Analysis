import json
import time
import random 
import undetected_chromedriver as uc 
from selenium.webdriver.common.by import By

def get_restaurant_urls(category_url, target_count = 100):

    options = uc.ChromeOptions()
    driver = uc.Chrome(options= options, version_main= 152) 

    driver.get(category_url) 
    restaurant_urls = []

    print(f"\n ---Bắt đầu thu thập link ---")

    try:
        while len(restaurant_urls) < target_count:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            #time sleep 2.5 - 4s
            time.sleep(random.uniform(2.5, 4)) 

            link_elements = driver.find_elements(By.CSS_SELECTOR, "a.ng-binding")
            previous_count = len(restaurant_urls) 
            for elem in link_elements:
                link = elem.get_attribute('href')
                if link and link not in restaurant_urls:
                    restaurant_urls.append(link)

            if len(restaurant_urls) == previous_count:
                print("Đã cuộn đến cuối trang, không tìm thấy thêm nhà hàng.")
            break
            

        return restaurant_urls[:target_count]
    finally:
        driver.quit()

def scrape_foody_to_json(url, driver):
    scraped_data = []

    url_parts = url.strip('/').split('/') 

    city = url_parts[3] if len(url_parts) > 3 else "khong ro"
    id_restaurant = url_parts[4] if len(url_parts) > 4 else "khong ro"

    try:
        driver.get(url) 
        time.sleep(random.uniform(3.0, 4.5)) 

        reviews = driver.find_elements(By.CSS_SELECTOR, 'li.review-item')
        for review in reviews:
            try:
                user = review.find_element(By.CSS_SELECTOR, 'a.ru-username').text.strip()
                try: rating = review.find_element(By.CSS_SELECTOR, 'div.review-points span').text.strip()
                except: rating = "Không có điểm" 
                try : device = review.find_element(By.CSS_SELECTOR, 'a.ru-device').text.strip().replace('via', '').strip()
                except: device = "Không rõ thiết bị" 
                try: post_time = review.find_element(By.CSS_SELECTOR, 'span.ru-time').text.strip()
                except: post_time = "Không rõ"
                try:
                    comment_elem = review.find_element(By.CSS_SELECTOR, 'div.rd-des span')
                    comment = comment_elem.get_attribute("textContent").strip()
                except:
                    comment = "Không bình luận"

                scraped_data.append({
                    "Thành Phố": city,
                    "ID Quán": id_restaurant,
                    "URL Quán": url,  # Lưu luôn full link cho chắc ăn
                    "Tên User": user,
                    "Điểm Đánh Giá": rating,
                    "Thiết Bị": device,
                    "Ngày Giờ": post_time,
                    "Bình Luận": comment 
                })
            except:
                pass
    except Exception as e:
        print(f"Lỗi ở link {url} : {e}") 

    return scraped_data

if __name__ == "__main__":
    category_urls = [
        'https://www.foody.vn/da-nang', 
        "https://www.foody.vn/", 
        "https://www.foody.vn/ha-noi", 
        "https://www.foody.vn/can-tho"
    ]

    for category_url in category_urls:
        urls_to_scrape = get_restaurant_urls(category_url, target_count= 100)

        city_name = category_url.strip('/').split('/')[-1] 
        if city_name == "www.foody.vn" or city_name == "":
            city_name = "ho-chi-minh-city" 
        file_name = f"foody_{city_name}_dataset.json"

        all_dataset = []
        main_driver = uc.Chrome(version_main= 152) 

        for index, url in enumerate(urls_to_scrape):
            print(f"[{index + 1}/{urls_to_scrape}] Đang cào dữ liệu quán : {url}")

            data_of_one_res = scrape_foody_to_json(url, main_driver) 
            all_dataset.extend(data_of_one_res) 

            with open(file_name, 'w', encoding= 'utf-8') as file:
                json.dump(all_dataset, file, ensure_ascii=False, indent=4)
            print(f"Đã update thành công vào {file_name}")

            time_sleep = random.uniform(3.5, 6.5) 
            print("Nghỉ 3 - 6s")
            time.sleep(time_sleep) 

        main_driver.quit() 
        print(f"Hoàn tất thành phố {city_name}")