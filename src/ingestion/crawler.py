"""Foody crawler, also callable from the parallel ingestion entry point."""
import logging
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)

if __package__:
    from .ingestion_runtime import CrawlCancelled, check_cancelled, close_driver, pause, write_json, resume_order
    from .chrome_version import make_chrome
    from .browser_health import connection_broken
else:
    from ingestion_runtime import CrawlCancelled, check_cancelled, close_driver, pause, write_json, resume_order
    from chrome_version import make_chrome
    from browser_health import connection_broken

LOG = logging.getLogger(__name__)
DEFAULT_CATEGORIES = ['https://www.foody.vn/da-nang', 'https://www.foody.vn/khanh-hoa',
                      'https://www.foody.vn/ha-noi', 'https://www.foody.vn/gia-lai',
                      'https://www.foody.vn/lam-dong', 'https://www.foody.vn/hue',
                      'https://www.foody.vn/quang-ngai', 'https://www.foody.vn/quang-tri',
                      'https://www.foody.vn/thanh-hoa', 'http://foody.vn/nghe-an',
                      'https://www.foody.vn/ha-tinh', 'https://www.foody.vn/phu-yen',
                      'https://www.foody.vn/dak-lak', 'https://www.foody.vn/dak-nong']

def category_city(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('foody.vn', 'www.foody.vn'):
        raise ValueError('Khu vực phải là URL Foody.')
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 1 or not parts[0]:
        raise ValueError('Dùng URL khu vực rõ ràng, ví dụ https://www.foody.vn/ho-chi-minh; không dùng trang chủ phụ thuộc cookie.')
    return parts[0]

def review_key(row):
    if row.get('ID Review'):
        return ('native', row.get('URL Quán'), str(row['ID Review']))
    return ('fallback', *(row.get(field) for field in ('URL Quán', 'Tên User', 'Ngày Giờ', 'Bình Luận')))

def load_existing_reviews(path):
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get('URL Quán') for row in rows):
        raise ValueError(f'Dataset Foody không đúng định dạng; giữ nguyên, không ghi đè: {path}')
    return {review_key(row): row for row in rows}

def get_restaurant_urls(category_url, target_count=100, *, driver=None,
                        headless=False, chrome_major=None, stop_event=None,
                        initial=None, checkpoint=None):
    city = category_city(category_url)
    owns_driver = driver is None
    check_cancelled(stop_event)
    if driver is None:
        driver = make_chrome('foody', headless=headless, chrome_major=chrome_major)
    try:
        driver.get(category_url)
        urls = list(dict.fromkeys(initial or []))
        seen = set(urls)
        if len(urls) >= target_count:
            return urls[:target_count]
        no_new = 0
        for _ in range(100):
            check_cancelled(stop_event)
            driver.execute_script('window.scrollTo({top: window.scrollY + Math.min(500, window.innerHeight * .7), behavior: "instant"});')
            pause(random.uniform(4, 6), stop_event)
            before = len(urls)
            for element in driver.find_elements(By.CSS_SELECTOR, 'a.ng-binding'):
                try:
                    link = element.get_attribute('href')
                    if not link:
                        continue
                    parsed = urlsplit(link)
                    if parsed.hostname not in ('foody.vn', 'www.foody.vn'):
                        continue
                    if len(parsed.path.strip('/').split('/')) != 2:
                        continue
                    if parsed.path.strip('/').split('/')[0] != city:
                        continue
                    canonical = f'{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip("/")}'
                    if canonical not in seen:
                        urls.append(canonical)
                        seen.add(canonical)
                    if len(urls) >= target_count:
                        if checkpoint:
                            checkpoint(urls)
                        return urls
                except StaleElementReferenceException:
                    continue
            if checkpoint and len(urls) > before:
                checkpoint(urls)
            at_bottom = driver.execute_script('return window.scrollY + window.innerHeight >= Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - 5;')
            no_new = no_new + 1 if len(urls) == before and at_bottom else 0
            if no_new >= 3:
                break
        return urls
    finally:
        if owns_driver:
            close_driver(driver)

def foody_review_state(driver):
    return driver.execute_script("""
        const reviews = [...document.querySelectorAll('li.review-item')];
        const root = document.scrollingElement || document.documentElement;
        const content = document.querySelector('.user-review-content') || document;
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        return {count: reviews.length,
            ready: reviews.filter(el => (el.querySelector('a.ru-username')?.textContent || '').trim()).length,
            height: root.scrollHeight, top: root.scrollTop, viewport: window.innerHeight,
            bottom: root.scrollTop + window.innerHeight >= root.scrollHeight - 5,
            loading: [...content.querySelectorAll('[aria-busy=true], [role=progressbar]:not(.reviewPointBar), .loading-spinner, a.fd-btn-more.loading')].some(visible)};
    """)

def wait_foody_reviews(driver, *, stop_event=None, min_wait=6, timeout=20):
    """Wait for review HTML to settle; a fixed short sleep can miss slow AJAX."""
    previous = None
    quiet = 0
    state = None
    for elapsed in range(1, timeout + 1):
        pause(1, stop_event)
        check_cancelled(stop_event)
        state = foody_review_state(driver)
        signature = (state['count'], state['ready'], state['height'])
        quiet = quiet + 1 if signature == previous and not state['loading'] else 0
        previous = signature
        if (elapsed >= min_wait and quiet >= 3 and not state['loading']
                and state['count'] > 0 and state['ready'] == state['count']):
            return {**state, 'settled': True}
    return {**state, 'settled': False}

def click_more_foody_reviews(driver):
    """Activate list pagination only, never the per-review expand-text link."""
    return driver.execute_script("""
        const button = [...document.querySelectorAll('a.fd-btn-more[ng-click]')]
            .find(e => /(?:^|\\s)LoadMore\\(\\)/.test(e.getAttribute('ng-click') || '') &&
                e.getClientRects().length && !e.classList.contains('loading') &&
                e.getAttribute('aria-disabled') !== 'true');
        if (!button) return false;
        const r = button.getBoundingClientRect();
        if (r.top > innerHeight + 150 || r.bottom < 0) return false;
        button.click();
        return true;
    """) is True


def scroll_foody_reviews(driver, *, stop_event=None, max_scrolls=100, max_seconds=600,
                         checkpoint=None):
    """Move at most 400px at a time, and wait longer at the lazy-load boundary."""
    check_cancelled(stop_event)
    started = time.monotonic()
    previous = foody_review_state(driver)
    if checkpoint:
        checkpoint()
    idle_rounds = 0
    empty_rounds = 0
    for _ in range(max_scrolls):
        check_cancelled(stop_event)
        if time.monotonic() - started >= max_seconds:
            LOG.warning('Foody hết giới hạn %ss ở quán này; giữ các bình luận đã tải.', max_seconds)
            return 'time_limit'
        driver.execute_script(
            'window.scrollTo({top: window.scrollY + Math.min(400, window.innerHeight * .6), behavior: "instant"});')
        pause(random.uniform(1.5, 2.5), stop_event)
        check_cancelled(stop_event)
        state = foody_review_state(driver)
        requested_more = click_more_foody_reviews(driver)

        if requested_more or state['bottom'] or state['loading']:
            remaining = max(1, int(max_seconds - (time.monotonic() - started)))
            state = wait_foody_reviews(driver, stop_event=stop_event, timeout=min(20, remaining))
        else:
            state['settled'] = False
        if state['count'] > previous['count'] or state['height'] > previous['height']:
            idle_rounds = 0
        elif state['bottom'] and state['settled']:
            idle_rounds += 1
        else:
            idle_rounds = 0
        if checkpoint and (state['count'], state['ready']) != (previous['count'], previous['ready']):
            checkpoint()
        previous = state
        empty_rounds = empty_rounds + 1 if state['bottom'] and not state['count'] and not state['loading'] else 0
        if empty_rounds >= 3:
            LOG.warning('Foody chưa trả HTML bình luận sau ba lần chờ ở cuối trang; không khẳng định quán không có review.')
            return 'empty_unconfirmed'
        if idle_rounds >= 3:
            return 'end_of_loaded_reviews'
    LOG.warning('Foody đạt giới hạn %s lượt cuộn; trang có thể còn bình luận chưa tải.', max_scrolls)
    return 'scroll_limit'

def scrape_foody_to_json(url, driver, *, stop_event=None, checkpoint=None):
    check_cancelled(stop_event)
    parsed = urlsplit(url)
    review_path = parsed.path.rstrip('/')
    if not review_path.endswith('/binh-luan'):
        review_path += '/binh-luan'
    review_url = parsed._replace(path=review_path, query='', fragment='').geturl()
    driver.get(review_url)
    def save_visible():
        if checkpoint:
            checkpoint(read_foody_reviews(url, driver))
    try:
        wait_foody_reviews(driver, stop_event=stop_event)
        reason = scroll_foody_reviews(driver, stop_event=stop_event, checkpoint=save_visible)
        rows = read_foody_reviews(url, driver, stop_event=stop_event)
        if checkpoint:
            checkpoint(rows, reason)
        return rows
    except (Exception, KeyboardInterrupt):
        # Cancellation must not prevent saving cards that already reached the DOM.
        try:
            save_visible()
        except Exception:
            LOG.warning('Không đọc thêm được HTML Foody; giữ checkpoint gần nhất.', exc_info=True)
        raise


def read_foody_reviews(url, driver, *, stop_event=None):
    parts = urlsplit(url).path.strip('/').split('/')
    city = parts[0] if parts else 'khong ro'
    restaurant = parts[1] if len(parts) > 1 else 'khong ro'
    collected_at = datetime.now(timezone.utc).isoformat()
    name = None
    headings = driver.find_elements(By.CSS_SELECTOR, 'h1')
    if headings:
        name = (headings[0].text or '').strip() or None
    restaurant_info = read_foody_restaurant(driver)
    photo_counts = read_foody_photo_counts(driver)
    rows = []
    for review in driver.find_elements(By.CSS_SELECTOR, 'li.review-item'):
        check_cancelled(stop_event)
        def value(selector, default, attribute=None):
            try:
                element = review.find_element(By.CSS_SELECTOR, selector)
                result = element.get_attribute(attribute) if attribute else element.text
                return (result or '').strip() or default
            except NoSuchElementException:
                return default
        try:
            user = value('a.ru-username', None)
            if user is None or '{{' in user:
                continue
            photo_count = photo_counts.get(value(
                '.ru-stats a[href*="/binh-luan-"], .rd-title[href*="/binh-luan-"]', '', 'href'))
            if photo_count is None:
                photo_count = photo_counts.get(value('div.review-points', '', 'data-review'))
            rows.append({'Thành Phố': city, 'ID Quán': restaurant, 'URL Quán': url,
                'Nguồn': 'Foody', 'Thang Điểm': 10, 'Tên Quán': name,
                'Thời Điểm Thu Thập': collected_at,
                'Tên User': user, 'Điểm Đánh Giá': value('div.review-points span', 'Không có điểm'),
                'Thiết Bị': value('a.ru-device', 'Không rõ thiết bị').replace('via', '').strip(),
                'Ngày Giờ': value('span.ru-time', 'Không rõ'),
                'Bình Luận': value('div.rd-des span', 'Không bình luận', 'textContent'),
                'Số Ảnh Bình Luận': photo_count,
                **restaurant_info})
        except StaleElementReferenceException:
            LOG.warning('Review Foody thay đổi trong lúc đọc: %s', url)
    return rows


def foody_number(value, *, integer=False):
    """Parse Foody's decimal scores, Vietnamese separators and K/M counters."""
    if value is None:
        return None
    text = str(value).strip().replace('\xa0', ' ')
    match = re.fullmatch(r'([0-9]+(?:[., ][0-9]+)*)\s*([kKmM]?)\s*(?:đ|₫)?', text)
    if not match:
        return None
    number, suffix = match.groups()
    number = number.replace(' ', '')
    if suffix or not integer:
        number = number.replace(',', '.')
    else:
        number = number.replace('.', '').replace(',', '')
    try:
        result = float(number) * {'': 1, 'k': 1000, 'm': 1000000}[suffix.lower()]
    except ValueError:
        return None
    if integer:
        return round(result) if abs(result - round(result)) < 1e-6 else None
    return result if 0 <= result <= 10 else None


def read_foody_restaurant(driver):
    """Read restaurant-level values once per snapshot, independently of reviews."""
    def text(selector):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                value = (element.get_attribute('textContent') or '').strip()
                if value and '{{' not in value:
                    return value
            except StaleElementReferenceException:
                continue
        return None

    result = {}
    for field, bar in (
        ('Điểm Vị Trí', 'position'), ('Điểm Giá Cả', 'price'),
        ('Điểm Chất Lượng', 'food'), ('Điểm Phục Vụ', 'service'),
        ('Điểm Không Gian', 'atmosphere'),
    ):
        result[field] = foody_number(text(f'tr:has(#{bar}PointBar) b'))
    result['Điểm Trung Bình Quán'] = foody_number(text('.ratings-boxes-points b'))
    # Ignore hidden popup templates; only read the displayed summary time range.
    hours = text('.micro-timesopen > span:not([class])') or ''
    times = re.findall(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', hours)
    result['Giờ Mở Cửa'] = f'{int(times[0][0]):02d}:{times[0][1]}' if len(times) >= 2 else None
    result['Giờ Đóng Cửa'] = f'{int(times[1][0]):02d}:{times[1][1]}' if len(times) >= 2 else None
    prices = re.split(r'\s*[-–—]\s*', text('.res-common-minmaxprice') or '')
    result['Giá Thấp Nhất'] = foody_number(prices[0], integer=True) if len(prices) == 2 else None
    result['Giá Cao Nhất'] = foody_number(prices[1], integer=True) if len(prices) == 2 else None
    for field, selector in (
        ('Lượt Xem', '.total-views > span'),
        ('Tổng Số Bình Luận', '.ratings-boxes .summary b, .microsite-review-count'),
        ('Số Bình Luận Tuyệt Vời', '.ratings-numbers b.exellent'),
        ('Số Bình Luận Khá Tốt', '.ratings-numbers b.good'),
        ('Số Bình Luận Trung Bình', '.ratings-numbers b.average'),
        ('Số Bình Luận Kém', '.ratings-numbers b.bad'),
    ):
        result[field] = foody_number(text(selector), integer=True)
    return result


def read_foody_photo_counts(driver):
    """TotalPictures includes photos omitted from the thumbnail preview."""
    counts = driver.execute_script("""
        const result = {};
        const add = model => {
            if (!model || !Number.isInteger(model.TotalPictures)
                    || model.TotalPictures < 0) return;
            if (model.Url) result[new URL(model.Url, location.href).href] = model.TotalPictures;
            if (model.Id) result['review_' + model.Id] = model.TotalPictures;
        };
        // Initial server-rendered cards, then the current lazy-loaded cards.
        if (typeof initDataReviews !== 'undefined')
            (initDataReviews.Items || []).forEach(add);
        if (window.angular) {
            document.querySelectorAll('li.review-item').forEach(card => {
                const scope = angular.element(card).scope();
                if (scope) add(scope.Model);
            });
        }
        return result;
    """)
    return counts if isinstance(counts, dict) else {}

def run_foody(category_urls=None, target_count=100, *, output_dir=None,
              headless=False, chrome_major=None, stop_event=None, refresh=False):
    if type(target_count) is not int or target_count < 1:
        raise ValueError('target_count phải là số nguyên dương.')
    categories = list(dict.fromkeys(DEFAULT_CATEGORIES if category_urls is None else category_urls))
    if not categories:
        raise ValueError('Danh sách khu vực Foody rỗng.')
    for category_url in categories:
        category_city(category_url)
    folder = Path(__file__).resolve().parent
    root = folder.parent.parent if folder.name == 'ingestion' and folder.parent.name == 'src' else folder
    output = Path(output_dir) if output_dir else root / 'data' / 'raw' / 'foody'
    queue_path = output / 'places_queue.json'
    queue = json.loads(queue_path.read_text(encoding='utf-8-sig')) if queue_path.exists() else {
        'version': 1, 'categories': {}, 'places': {},
    }
    if (not isinstance(queue, dict) or queue.get('version') != 1
            or not isinstance(queue.get('categories'), dict) or not isinstance(queue.get('places'), dict)
            or any(not isinstance(item, dict) or item.get('url') != url
                   for url, item in queue['places'].items())
            or any(not isinstance(record, dict) or not isinstance(record.get('urls'), list)
                   or any(url not in queue['places'] for url in record['urls'])
                   for record in queue['categories'].values())):
        raise ValueError('Checkpoint Foody không đúng định dạng; giữ nguyên, không ghi đè.')
    report = {'restaurants': 0, 'reviews': 0, 'new_reviews': 0, 'empty_restaurants': 0,
              'skipped': 0, 'incomplete': 0,
              'errors': [], 'files': [], 'coverage': 'scroll_loaded_reviews_only',
              'queue': str(queue_path)}
    driver = None
    def browser():
        nonlocal driver
        check_cancelled(stop_event)
        if driver is None:
            driver = make_chrome('foody', headless=headless, chrome_major=chrome_major)
        return driver
    try:
        check_cancelled(stop_event)
        for category_url in categories:
            check_cancelled(stop_event)
            city_name = category_city(category_url)
            destination = output / f'foody_{city_name}_dataset.json'
            try:
                rows_by_id = load_existing_reviews(destination)
                category = queue['categories'].setdefault(city_name, {'urls': []})
                def save_urls(urls):
                    for url in urls:
                        if url not in category['urls']:
                            category['urls'].append(url)
                        queue['places'].setdefault(url, {'url': url, 'status': 'pending', 'attempts': 0})
                    write_json(queue_path, queue)
                if refresh or not category['urls'] or (len(category['urls']) < target_count
                               and category.get('discovery_target', 0) < target_count):
                    discovered = get_restaurant_urls(category_url, target_count, driver=browser(),
                        stop_event=stop_event, initial=category['urls'], checkpoint=save_urls)
                    save_urls(discovered)
                    category['discovery_target'] = target_count
                    write_json(queue_path, queue)
                urls = category['urls'][:target_count]
                if not urls:
                    raise RuntimeError(f'Không tìm thấy URL quán Foody: {category_url}')
                saved_urls = {row['URL Quán'] for row in rows_by_id.values()}
                items = [queue['places'][url] for url in urls]
                if not refresh:
                    items = resume_order(items, category.get('last_processed_url'), key='url')
                LOG.info('[Foody %s] Khôi phục %s URL, %s review từ %s.',
                         city_name, len(urls), len(rows_by_id), queue_path)
                for index, item in enumerate(items, 1):
                    check_cancelled(stop_event)
                    url = item['url']
                    if not refresh and item['status'] == 'processed' and url in saved_urls:
                        report['skipped'] += 1
                        LOG.info('[Foody %s %s/%s] Bỏ qua quán đã xử lý: %s', city_name, index, len(items), url)
                        continue
                    LOG.info('[Foody %s %s/%s] %s', city_name, index, len(urls), url)
                    item.update(status='running', attempts=item.get('attempts', 0) + 1)
                    write_json(queue_path, queue)
                    stop_reason = None
                    def checkpoint(data, reason=None):
                        nonlocal stop_reason
                        if data:
                            before = len(rows_by_id)
                            rows_by_id.update((review_key(row), row) for row in data)
                            # Commit data before marking a place as processed.
                            write_json(destination, list(rows_by_id.values()))
                            report['new_reviews'] += len(rows_by_id) - before
                            item['count'] = sum(row['URL Quán'] == url for row in rows_by_id.values())
                            if str(destination) not in report['files']:
                                report['files'].append(str(destination))
                        if reason is not None:
                            stop_reason = reason
                        write_json(queue_path, queue)
                    try:
                        try:
                            data = scrape_foody_to_json(url, browser(), stop_event=stop_event, checkpoint=checkpoint)
                        except Exception as exc:
                            if not connection_broken(exc):
                                raise
                            LOG.warning('Mất phiên Chrome ở quán %s; khởi động lại Chrome và thử lại.',url)
                            close_driver(driver)
                            driver = make_chrome('foody', headless=headless, chrome_major=chrome_major)
                            data = scrape_foody_to_json(url, driver, stop_event=stop_event, checkpoint=checkpoint)
                        report['restaurants'] += 1
                        report['reviews'] += len(data)
                        report['empty_restaurants'] += not bool(data)
                        checkpoint(data)
                        item.update(status='processed' if data and stop_reason in (None, 'end_of_loaded_reviews') else 'partial',
                                    stop_reason=stop_reason or ('end_of_loaded_reviews' if data else 'empty_unconfirmed'))
                        item.pop('error', None)
                    except (CrawlCancelled, KeyboardInterrupt):
                        item['status'] = 'interrupted'
                        write_json(queue_path, queue)
                        raise
                    except Exception as exc:
                        item.update(status='failed', error=str(exc))
                        report['errors'].append({'url': url, 'error': str(exc)})
                        LOG.exception('Foody lỗi ở quán %s', url)
                    item['finished_at'] = datetime.now(timezone.utc).isoformat()
                    category['last_processed_url'] = url
                    write_json(queue_path, queue)
                    pause(random.uniform(3.5, 6.5), stop_event)
                report['incomplete'] += sum(item['status'] != 'processed' for item in items)
            except CrawlCancelled:
                raise
            except Exception as exc:
                report['errors'].append({'url': category_url, 'error': str(exc)})
                LOG.exception('Foody lỗi ở khu vực %s', category_url)
        return report
    finally:
        close_driver(driver)
        write_json(output / 'foody_status.json', report)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_foody()
