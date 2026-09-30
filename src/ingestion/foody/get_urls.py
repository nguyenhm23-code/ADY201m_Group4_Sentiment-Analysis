"""Lấy URL quán Foody và lưu vào extended/checkpoints.

Chạy từ thư mục dự án:
    python src/ingestion/foody/get_urls.py --cities hue --target-count 100
"""

import argparse
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


HERE = Path(__file__).resolve().parent
CHECKPOINT_DIR = HERE / "extended" / "checkpoints"
DEFAULT_CITIES = ("ha-noi", "hai-phong", "quang-ninh", "bac-ninh")

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def click_more(driver):
    """Bấm nút Xem thêm nếu nút còn dùng được."""
    for selector in ("a.fd-btn-more", "a[ng-click='LoadMore()']"):
        for button in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if button.is_displayed() and button.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
                    driver.execute_script("arguments[0].click();", button)
                    return True
            except Exception:
                continue
    return False


def get_restaurant_urls(driver, city, target_count):
    category_url = f"https://www.foody.vn/{city}"
    print(f"\nĐang lấy URL từ {category_url}")
    driver.get(category_url)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(random.uniform(3.0, 4.5))

    urls = []
    seen = set()
    unchanged_rounds = 0

    for round_number in range(1, 501):
        before = len(urls)
        for link in driver.find_elements(By.CSS_SELECTOR, "a.ng-binding[href]"):
            url = (link.get_attribute("href") or "").split("?")[0].rstrip("/")
            parsed = urlparse(url)
            parts = parsed.path.strip("/").split("/")
            if parsed.netloc.lower() != "www.foody.vn" or len(parts) != 2:
                continue
            if parts[0] != city or not parts[1] or "binh-luan" in parts[1]:
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)

        print(f"Lượt {round_number}: {len(urls)}/{target_count} URL")
        if len(urls) >= target_count:
            break

        unchanged_rounds = unchanged_rounds + 1 if len(urls) == before else 0
        if unchanged_rounds >= 2:
            print("Hai lượt không có URL mới, dừng lấy link.")
            break

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(2.0, 3.0))
        clicked = click_more(driver)
        print("Đã bấm Xem thêm." if clicked else "Không tìm thấy nút Xem thêm.")
        time.sleep(random.uniform(2.0, 3.0))

    return urls[:target_count]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", nargs="+", default=DEFAULT_CITIES)
    parser.add_argument("--target-count", type=int, default=200)
    parser.add_argument("--profile-dir", type=Path, default=HERE / "chrome_profile")
    parser.add_argument("--fresh", action="store_true", help="Lấy lại URL dù đã có checkpoint")
    args = parser.parse_args()
    if args.target_count < 1:
        parser.error("--target-count phải lớn hơn 0")

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    driver = None
    try:
        for city in args.cities:
            links_file = CHECKPOINT_DIR / f"foody_{city}_links.txt"
            if links_file.exists() and not args.fresh:
                print(f"Đã có {links_file}; dùng --fresh để lấy lại URL.")
                continue

            if driver is None:
                driver = uc.Chrome(user_data_dir=str(args.profile_dir.resolve()), version_main=153)
            urls = get_restaurant_urls(driver, city, args.target_count)
            links_file.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
            print(f"Đã lưu {len(urls)} URL vào {links_file}")
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
