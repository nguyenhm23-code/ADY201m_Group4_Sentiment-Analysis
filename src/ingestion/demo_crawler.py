import json
import random
import time
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

# ==========================================
# CẤU HÌNH CHUNG CHO NHÓM
# ==========================================
# Link danh mục khu vực mẫu (Thành viên có thể thay đổi tùy ý)
CATEGORY_URL = "https://www.foody.vn/ha-noi"
OUTPUT_JSON_FILE = "foody_dataset_shared.json"

# Chuẩn 9 trường dữ liệu quy định của nhóm
# ["Thành Phố", "ID Quán", "URL Quán", "Tên User", "Điểm Đánh Giá", "Thiết Bị", "Ngày Giờ", "Bình Luận"]
# ==========================================


def get_restaurant_urls(category_url, driver, limit=5):
  """Hàm gom danh sách các link quán ăn từ trang danh mục khu vực."""
  print(f"\n--- Đang quét danh mục: {category_url} ---")
  driver.get(category_url)
  time.sleep(3)

  urls = []
  while len(urls) < limit:
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    elements = driver.find_elements(By.CSS_SELECTOR, "a.ng-binding")
    previous_count = len(urls)

    for elem in elements:
      try:
        link = elem.get_attribute("href")
        # Lọc chỉ lấy link quán chính, loại bỏ các link rác
        if (
            link
            and link not in urls
            and len(link.strip("/").split("/")) == 5
            and "/binh-luan-" not in link
        ):
          urls.append(link)
      except:
        continue

    if len(urls) == previous_count:
      break

  print(f"-> Đã gom được {len(urls[:limit])} link quán chuẩn để cào.")
  return urls[:limit]


def scrape_reviews_from_restaurant(url, driver):
  """Hàm vào chi tiết một quán, cào dữ liệu bình luận và đóng gói chuẩn 9 trường."""
  print(f"Đang cào dữ liệu quán: {url}")
  restaurant_data = []

  if "shopeefood.vn" in url:
    return restaurant_data

  # Tách thông tin Thành phố và ID Quán từ URL
  url_parts = url.strip("/").split("/")
  city = url_parts[3] if len(url_parts) > 3 else "khong_ro"
  id_restaurant = url_parts[4] if len(url_parts) > 4 else "khong_ro"

  try:
    driver.get(url)
    time.sleep(3)

    # Cuộn trang và mở rộng thêm bình luận (giới hạn số lần bấm để ổn định)
    for _ in range(5):
      driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
      time.sleep(1.5)
      try:
        btn = driver.find_element(
            By.XPATH,
            "//a[not(ancestor::li)]/*[contains(text(), 'Xem thêm') or"
            " contains(text(), 'xem thêm')]",
        )
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(2)
      except:
        break

    # Lấy danh sách các thẻ review
    reviews = driver.find_elements(By.CSS_SELECTOR, "li.review-item")
    if not reviews:
      return restaurant_data

    for review in reviews:
      try:
        user = (
            review.find_element(By.CSS_SELECTOR, "a.ru-username")
            .text.strip()
        )

        try:
          rating = (
              review.find_element(By.CSS_SELECTOR, "div.review-points span")
              .text.strip()
          )
        except:
          rating = "Không có điểm"

        try:
          device = (
              review.find_element(By.CSS_SELECTOR, "a.ru-device")
              .text.strip()
              .replace("via", "")
              .strip()
          )
        except:
          device = "Không rõ"

        try:
          post_time = (
              review.find_element(By.CSS_SELECTOR, "span.ru-time")
              .text.strip()
          )
        except:
          post_time = "Không rõ"

        try:
          comment_elem = review.find_element(
              By.CSS_SELECTOR, "div.rd-des span"
          )
          raw_comment = comment_elem.get_attribute("textContent").strip()
          # Làm sạch ký tự xuống dòng để file JSON không bị lỗi định dạng
          clean_comment = (
              raw_comment.replace("\n", " ")
              .replace("\r", " ")
              .replace("  ", " ")
          )
        except:
          clean_comment = "Không bình luận"

        # Đóng gói hoàn chỉnh thành một đối tượng JSON đúng 9 trường dữ liệu
        item = {
            "Thành Phố": city,
            "ID Quán": id_restaurant,
            "URL Quán": url,
            "Tên User": user,
            "Điểm Đánh Giá": rating,
            "Thiết Bị": device,
            "Ngày Giờ": post_time,
            "Bình Luận": clean_comment,
        }
        restaurant_data.append(item)
      except:
        continue

    print(f"-> Thu hoạch thành công {len(restaurant_data)} bình luận.")

  except Exception:
    print("-> Bỏ qua quán gặp lỗi kết nối.")

  return restaurant_data


if __name__ == "__main__":
  print("=== CHƯƠNG TRÌNH CÀO DỮ LIỆU FOODY CHUẨN (JSON) ===")

  options = uc.ChromeOptions()
  driver = uc.Chrome(options=options, version_main=153)

  all_collected_data = []

  # Lấy danh sách link quán
  shop_urls = get_restaurant_urls(CATEGORY_URL, driver, limit=100)

  # Vòng lặp cào từng quán
  for shop_url in shop_urls:
    driver.delete_all_cookies()  # Xóa cookie định kỳ chống chặn bot
    shop_data = scrape_reviews_from_restaurant(shop_url, driver)
    all_collected_data.extend(shop_data)
    time.sleep(random.uniform(2, 4))

  driver.quit()

  # Xuất file JSON dùng chung cho nhóm
  with open(OUTPUT_JSON_FILE, mode="w", encoding="utf-8") as f:
    json.dump(all_collected_data, f, ensure_ascii=False, indent=4)

  print(
      f"\n=== HOÀN TẤT! Đã lưu kết quả vào file '{OUTPUT_JSON_FILE}' ==="
  )