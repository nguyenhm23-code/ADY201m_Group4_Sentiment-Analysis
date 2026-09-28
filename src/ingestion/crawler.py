"""Foody crawler, also callable from the parallel ingestion entry point."""
import logging
import json
import random
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
    from .ingestion_runtime import CrawlCancelled, check_cancelled, close_driver, pause, write_json
    from .chrome_version import make_chrome
    from .browser_health import connection_broken
else:
    from ingestion_runtime import CrawlCancelled, check_cancelled, close_driver, pause, write_json
    from chrome_version import make_chrome
    from browser_health import connection_broken

LOG = logging.getLogger(__name__)
DEFAULT_CATEGORIES = ['https://www.foody.vn/da-nang', 'https://www.foody.vn/ho-chi-minh',
                      'https://www.foody.vn/ha-noi', 'https://www.foody.vn/can-tho',
                      'https://www.foody.vn/lam-dong', 'https://www.foody.vn/hue']

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
                        headless=False, chrome_major=None, stop_event=None):
    city = category_city(category_url)
    owns_driver = driver is None
    check_cancelled(stop_event)
    if driver is None:
        driver = make_chrome('foody', headless=headless, chrome_major=chrome_major)
    try:
        driver.get(category_url)
        urls, seen = [], set()
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
                        return urls
                except StaleElementReferenceException:
                    continue
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


def scroll_foody_reviews(driver, *, stop_event=None, max_scrolls=100, max_seconds=600):
    """Move at most 400px at a time, and wait longer at the lazy-load boundary."""
    check_cancelled(stop_event)
    started = time.monotonic()
    previous = foody_review_state(driver)
    idle_rounds = 0
    empty_rounds = 0
    for _ in range(max_scrolls):
        check_cancelled(stop_event)
        if time.monotonic() - started >= max_seconds:
            LOG.warning('Foody hết giới hạn %ss ở quán này; giữ các bình luận đã tải.', max_seconds)
            return
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
        previous = state
        empty_rounds = empty_rounds + 1 if state['bottom'] and not state['count'] and not state['loading'] else 0
        if empty_rounds >= 3:
            LOG.warning('Foody chưa trả HTML bình luận sau ba lần chờ ở cuối trang; không khẳng định quán không có review.')
            return
        if idle_rounds >= 3:
            return
    LOG.warning('Foody đạt giới hạn %s lượt cuộn; trang có thể còn bình luận chưa tải.', max_scrolls)

def scrape_foody_to_json(url, driver, *, stop_event=None):
    check_cancelled(stop_event)
    parsed = urlsplit(url)
    review_path = parsed.path.rstrip('/')
    if not review_path.endswith('/binh-luan'):
        review_path += '/binh-luan'
    review_url = parsed._replace(path=review_path, query='', fragment='').geturl()
    driver.get(review_url)
    wait_foody_reviews(driver, stop_event=stop_event)
    scroll_foody_reviews(driver, stop_event=stop_event)
    parts = urlsplit(url).path.strip('/').split('/')
    city = parts[0] if parts else 'khong ro'
    restaurant = parts[1] if len(parts) > 1 else 'khong ro'
    collected_at = datetime.now(timezone.utc).isoformat()
    name = None
    headings = driver.find_elements(By.CSS_SELECTOR, 'h1')
    if headings:
        name = (headings[0].text or '').strip() or None
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
            if user is None:
                continue
            rows.append({'Thành Phố': city, 'ID Quán': restaurant, 'URL Quán': url,
                'Nguồn': 'Foody', 'Thang Điểm': 10, 'Tên Quán': name,
                'Thời Điểm Thu Thập': collected_at,
                'Tên User': user, 'Điểm Đánh Giá': value('div.review-points span', 'Không có điểm'),
                'Thiết Bị': value('a.ru-device', 'Không rõ thiết bị').replace('via', '').strip(),
                'Ngày Giờ': value('span.ru-time', 'Không rõ'),
                'Bình Luận': value('div.rd-des span', 'Không bình luận', 'textContent')})
        except StaleElementReferenceException:
            LOG.warning('Review Foody thay đổi trong lúc đọc: %s', url)
    return rows

def run_foody(category_urls=None, target_count=100, *, output_dir=None,
              headless=False, chrome_major=None, stop_event=None):
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
    report = {'restaurants': 0, 'reviews': 0, 'new_reviews': 0, 'empty_restaurants': 0,
              'errors': [], 'files': [], 'coverage': 'scroll_loaded_reviews_only'}
    driver = None
    try:
        check_cancelled(stop_event)
        driver = make_chrome('foody', headless=headless, chrome_major=chrome_major)
        for category_url in categories:
            check_cancelled(stop_event)
            city_name = category_city(category_url)
            destination = output / f'foody_{city_name}_dataset.json'
            try:
                rows_by_id = load_existing_reviews(destination)
                urls = get_restaurant_urls(category_url, target_count, driver=driver, stop_event=stop_event)
                if not urls:
                    raise RuntimeError(f'Không tìm thấy URL quán Foody: {category_url}')
                for index, url in enumerate(urls, 1):
                    check_cancelled(stop_event)
                    LOG.info('[Foody %s %s/%s] %s', city_name, index, len(urls), url)
                    try:
                        try:
                            data = scrape_foody_to_json(url, driver, stop_event=stop_event)
                        except Exception as exc:
                            if not connection_broken(exc):
                                raise
                            LOG.warning('Mất phiên Chrome ở quán %s; khởi động lại Chrome và thử lại.',url)
                            close_driver(driver)
                            driver = make_chrome('foody', headless=headless, chrome_major=chrome_major)
                            data = scrape_foody_to_json(url, driver, stop_event=stop_event)
                        report['restaurants'] += 1
                        report['reviews'] += len(data)
                        report['empty_restaurants'] += not bool(data)
                        if data:
                            before = len(rows_by_id)
                            rows_by_id.update((review_key(row), row) for row in data)
                            report['new_reviews'] += len(rows_by_id) - before
                            write_json(destination, list(rows_by_id.values()))
                            if str(destination) not in report['files']:
                                report['files'].append(str(destination))
                    except CrawlCancelled:
                        raise
                    except Exception as exc:
                        report['errors'].append({'url': url, 'error': str(exc)})
                        LOG.exception('Foody lỗi ở quán %s', url)
                    pause(random.uniform(3.5, 6.5), stop_event)
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
