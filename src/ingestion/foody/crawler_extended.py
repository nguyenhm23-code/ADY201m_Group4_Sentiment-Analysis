"""Cào thông tin quán và bình luận từ danh sách URL đã lưu.

Chạy get_urls.py trước, sau đó chạy từ thư mục dự án:
    python src/ingestion/foody/crawler_extended.py --cities hue --fresh

Mỗi dòng JSON là một bình luận, kèm thông tin của quán tương ứng.
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "extended"
DEFAULT_CITIES = ("ha-noi", "hai-phong", "quang-ninh", "bac-ninh")
API_PAGE_SIZE = 10
POINT_IDS = {
    "positionPointBar": "Điểm Vị Trí",
    "pricePointBar": "Điểm Giá Cả",
    "foodPointBar": "Điểm Chất Lượng",
    "servicePointBar": "Điểm Phục Vụ",
    "atmospherePointBar": "Điểm Không Gian",
}
RATING_CLASSES = {
    "exellent": "Số Bình Luận Tuyệt Vời",  # Foody viết class là exellent.
    "good": "Số Bình Luận Khá Tốt",
    "average": "Số Bình Luận Trung Bình",
    "bad": "Số Bình Luận Kém",
}

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def sleep_briefly(low=1.5, high=2.5):
    time.sleep(random.uniform(low, high))


def log(message):
    print(message, flush=True)


class ReviewTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")


def review_text(html_text):
    parser = ReviewTextParser()
    parser.feed(html_text or "")
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def review_time(raw_date):
    """Foody gửi /Date(milliseconds)/ theo UTC; bình luận hiển thị giờ Việt Nam."""
    match = re.search(r"Date\((\d+)", raw_date or "")
    if not match:
        return "Không rõ"
    vietnam_time = datetime.fromtimestamp(
        int(match.group(1)) / 1000, timezone.utc
    ).astimezone(timezone(timedelta(hours=7)))
    return f"{vietnam_time.day}/{vietnam_time.month}/{vietnam_time.year} {vietnam_time.hour}:{vietnam_time.minute:02d}"


def text_of(root, selector):
    """Đọc textContent để lấy cả nội dung nằm trong thẻ đang ẩn một phần."""
    elements = root.find_elements(By.CSS_SELECTOR, selector)
    return (elements[0].get_attribute("textContent") or "").strip() if elements else ""


def number(text):
    """Điểm Foody dùng dấu chấm thập phân; trường thiếu được giữ là null."""
    match = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(match.group().replace(",", ".")) if match else None


def integer(text):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def views(text):
    """6.0K -> 6000, 1.2M -> 1200000; trả null nếu không đọc được."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*([KMB]?)\b", text or "", re.I)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return round(value * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()])


def price_range(text):
    amounts = re.findall(r"\d[\d.,]*\s*đ", text or "", re.I)
    if len(amounts) < 2:
        return None, None
    return integer(amounts[0]), integer(amounts[1])


def opening_hours(text):
    match = re.search(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", text or "")
    if not match:
        return None, None
    return match.group(1).zfill(5), match.group(2).zfill(5)


def extract_shop_info(driver):
    """Đọc các thẻ thông tin quán trên trang quán hoặc trang bình luận."""
    info = {key: None for key in POINT_IDS.values()}
    for bar_id, field in POINT_IDS.items():
        bars = driver.find_elements(By.ID, bar_id)
        if bars:
            row = bars[0].find_element(By.XPATH, "./ancestor::tr[1]")
            info[field] = number(text_of(row, "td:nth-child(3) b"))

    info["Điểm Trung Bình Quán"] = number(text_of(driver, ".ratings-boxes-points b"))
    opening, closing = opening_hours(text_of(driver, ".micro-timesopen"))
    info["Giờ Mở Cửa"] = opening
    info["Giờ Đóng Cửa"] = closing
    minimum, maximum = price_range(text_of(driver, ".res-common-minmaxprice"))
    info["Giá Thấp Nhất"] = minimum
    info["Giá Cao Nhất"] = maximum
    info["Lượt Xem"] = views(text_of(driver, ".total-views span"))
    info["Tổng Số Bình Luận"] = integer(text_of(driver, ".ratings-boxes .summary b"))
    for css_class, field in RATING_CLASSES.items():
        info[field] = integer(text_of(driver, f".ratings-boxes .ratings-numbers b.{css_class}"))
    return info


def fetch_review_page(driver, res_id, last_id, excluded_ids, is_latest="true", has_picture=""):
    """Gọi endpoint của nút 'Xem thêm bình luận' trong cùng phiên Chrome."""
    script = """
        const done = arguments[arguments.length - 1];
        const body = new URLSearchParams({
            ResId: String(arguments[0]), LastId: String(arguments[1] || ''),
            Count: '10', Type: '1', fromOwner: '',
            isLatest: arguments[3], HasPicture: arguments[4],
            ExcludeIds: arguments[2]
        });
        fetch('/__get/Review/ResLoadMore', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
            body: body.toString()
        }).then(async response => {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            done({data: await response.json()});
        }).catch(error => done({error: String(error)}));
    """
    driver.set_script_timeout(40)
    for attempt in range(1, 4):
        try:
            result = driver.execute_async_script(
                script, res_id, last_id or "", ",".join(map(str, excluded_ids)),
                is_latest, has_picture
            )
            if not isinstance(result, dict) or "data" not in result:
                detail = result.get("error", "Phản hồi API không hợp lệ") if isinstance(result, dict) else result
                raise RuntimeError(detail)
            return result["data"]
        except Exception as error:
            if attempt == 3:
                raise RuntimeError(f"API Foody lỗi sau 3 lần thử: {error}") from error
            log(f"    [API] Lần thử {attempt} thất bại: {error}; thử lại")
            sleep_briefly(2.0, 4.0)


def api_review_record(item, city, shop_slug, shop_url, shop_info):
    owner = item.get("Owner") or {}
    picture_count = item.get("TotalPictures")
    if picture_count is None:
        picture_count = len(item.get("Pictures") or [])
    rating = item.get("AvgRating")
    return {
        "Thành Phố": city,
        "ID Quán": shop_slug,
        "URL Quán": shop_url,
        "ID Bình Luận": item.get("Id"),
        "Tên User": owner.get("DisplayName") or owner.get("Username") or "Không rõ",
        "Điểm Đánh Giá": f"{float(rating):.1f}" if rating is not None else "Không có điểm",
        "Thiết Bị": item.get("DeviceName") or "Không rõ thiết bị",
        "Ngày Giờ": review_time(item.get("CreatedDate")),
        "Bình Luận": review_text(item.get("Description")) or "Không bình luận",
        "Số Ảnh Bình Luận": int(picture_count),
        **shop_info,
    }


def load_review_group(driver, res_id, excluded_ids, is_latest, has_picture):
    """Tải tối đa 20 trang; một nhóm giữ nguyên ExcludeIds khi chuyển trang."""
    reviews = {}
    last_id = ""
    seen_pages = set()

    for page_number in range(1, 21):
        data = fetch_review_page(
            driver, res_id, last_id, excluded_ids, is_latest, has_picture
        )
        items = data.get("Items") or []
        page_ids = tuple(item.get("Id") for item in items)
        if not items or page_ids in seen_pages:
            break
        seen_pages.add(page_ids)

        for item in items:
            review_id = item.get("Id")
            if review_id is not None:
                reviews[review_id] = item

        next_last_id = data.get("LastId")
        if next_last_id is None or str(next_last_id) == str(last_id):
            break
        last_id = str(next_last_id)
        if page_number * API_PAGE_SIZE >= int(data.get("Total") or 0):
            break
        sleep_briefly(0.5, 1.0)

    return reviews


def collect_all_reviews(driver, context, city, shop_slug, shop_url, shop_info):
    """Lấy nhiều nhóm/bộ lọc và bỏ bình luận trùng theo ID Foody."""
    expected = context.get("expected")
    expected = int(expected) if expected is not None else None
    records = {}
    modes = (
        ("true", "", "mới nhất"),
        ("false", "", "thứ tự khác"),
        ("false", "false", "không ảnh"),
        ("false", "true", "có ảnh"),
    )

    for is_latest, has_picture, mode_name in modes:
        if expected is not None and len(records) >= expected:
            break

        excluded_ids = []
        seen_in_mode = set()
        for group_number in range(1, 51):
            group = load_review_group(
                driver, context["res_id"], excluded_ids, is_latest, has_picture
            )
            new_ids = [review_id for review_id in group if review_id not in seen_in_mode]
            if not new_ids:
                break

            excluded_ids.extend(new_ids)
            seen_in_mode.update(new_ids)
            for review_id in new_ids:
                if review_id not in records:
                    records[review_id] = api_review_record(
                        group[review_id], city, shop_slug, shop_url, shop_info
                    )
            log(f"    [API] {mode_name}, nhóm {group_number}: tổng {len(records)} bình luận")
            if expected is not None and len(records) >= expected:
                break

    return list(records.values()), expected


def scrape_shop(driver, url):
    shop_url = url.split("?")[0].rstrip("/").split("/binh-luan")[0]
    parts = urlparse(shop_url).path.strip("/").split("/")
    if len(parts) != 2:
        raise ValueError(f"URL quán không hợp lệ: {url}")
    city, shop_slug = parts
    log(f"  [Quán] Đọc thông tin: {shop_url}")
    driver.get(shop_url)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    sleep_briefly(3.0, 4.5)
    shop_info = extract_shop_info(driver)

    log(f"  [Review] Mở trang: {shop_url}/binh-luan")
    driver.get(f"{shop_url}/binh-luan")
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    sleep_briefly(3.0, 4.5)
    # Một số thẻ chỉ xuất hiện ở trang bình luận; dùng giá trị ở trang quán làm dự phòng.
    review_page_info = extract_shop_info(driver)
    shop_info.update({key: value for key, value in review_page_info.items() if value is not None})
    if all(value is None for value in shop_info.values()):
        raise RuntimeError("Trang chưa tải được thông tin quán")
    found_fields = sum(value is not None for value in shop_info.values())
    log(f"  [Quán] Đọc được {found_fields}/{len(shop_info)} trường thông tin")

    context = driver.execute_script("""
        const data = window.initDataReviews;
        return data ? {res_id: data.ResId,
                       expected: data.Summary ? data.Summary.Total : null} : null;
    """)
    if not context or not context.get("res_id"):
        raise RuntimeError("Không đọc được initDataReviews/ResId để phân trang API")
    log(f"  [Review] Foody hiển thị {context.get('expected') or '?'} bình luận; bắt đầu tải qua API")
    records, expected = collect_all_reviews(
        driver, context, city, shop_slug, shop_url, shop_info
    )
    log(f"  [Review] Hoàn tất quán: {len(records)} bình luận duy nhất")
    if expected is not None and len(records) < expected:
        log(f"  [Cảnh báo] API công khai chỉ trả {len(records)}/{expected} bình luận; phần còn lại không hiện qua các bộ lọc đã thử")
    return records, expected


def write_json(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(records, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", nargs="+", default=DEFAULT_CITIES)
    parser.add_argument("--profile-dir", type=Path, default=HERE / "chrome_profile")
    parser.add_argument("--fresh", action="store_true", help="Cào lại từ đầu và ghi lại JSON/checkpoint")
    args = parser.parse_args()

    checkpoints = DATA_DIR / "checkpoints"
    city_urls = {}
    for city in args.cities:
        links_file = checkpoints / f"foody_{city}_links.txt"
        if not links_file.exists():
            parser.error(f"Chưa có {links_file}. Hãy chạy get_urls.py trước.")
        city_urls[city] = [
            line.strip() for line in links_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    driver = uc.Chrome(user_data_dir=str(args.profile_dir.resolve()), version_main=153)
    try:
        for city, urls in city_urls.items():
            log(f"\nBắt đầu cào {city}: {len(urls)} quán")
            progress_file = checkpoints / f"foody_{city}_progress_api_v2.txt"
            output_file = DATA_DIR / f"foody_{city}_dataset.json"
            if args.fresh:
                write_json(output_file, [])
                progress_file.write_text("", encoding="utf-8")
            records = json.loads(output_file.read_text(encoding="utf-8")) if output_file.exists() else []
            completed = set(progress_file.read_text(encoding="utf-8").splitlines()) if progress_file.exists() else set()
            log(f"Đã cào {len(completed)}/{len(urls)} quán; JSON có {len(records)} bình luận")

            for index, url in enumerate(urls, 1):
                if url in completed:
                    log(f"[{index}/{len(urls)}] Bỏ qua: {url}")
                    continue
                log(f"[{index}/{len(urls)}] Đang cào: {url}")
                try:
                    current, expected = scrape_shop(driver, url)
                except Exception as error:
                    log(f"  Cào thất bại, sẽ thử lại lần sau: {error}")
                    continue
                if not current:
                    log("  Chưa đọc được bình luận; sẽ thử lại lần sau")
                    continue

                # Thay dữ liệu cũ của quán khi chạy lại, tránh bình luận trùng.
                records = [record for record in records if record.get("URL Quán") != url]
                records.extend(current)
                write_json(output_file, records)
                log(f"  Đã lưu {len(current)} bình luận vào {output_file}")

                if expected is None or len(current) >= expected:
                    with progress_file.open("a", encoding="utf-8") as stream:
                        stream.write(url + "\n")
                    completed.add(url)
                else:
                    log(f"  Mới có {len(current)}/{expected} bình luận; sẽ thử lại lần sau")
                pause = random.uniform(3.5, 6.5)
                log(f"  Nghỉ {pause:.1f} giây")
                time.sleep(pause)
            log(f"Hoàn tất {city}: {len(completed)}/{len(urls)} quán, {len(records)} bình luận")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
