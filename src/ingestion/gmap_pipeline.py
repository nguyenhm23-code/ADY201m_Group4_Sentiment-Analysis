"""Discover Google Maps places by area, then crawl reviews with a persistent queue."""

import argparse
import json
import logging
import time
from contextlib import ExitStack
from itertools import zip_longest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

if __package__:
    from . import GGMap as gmap
    from .ingestion_runtime import CrawlCancelled, check_cancelled, close_driver, pause, resume_order
else:
    import GGMap as gmap
    from ingestion_runtime import CrawlCancelled, check_cancelled, close_driver, pause, resume_order

LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('Cấu hình phải là JSON object.')
    for key in ('target_restaurants', 'max_reviews_per_restaurant'):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(f'{key} phải là số nguyên dương.')
    for key in ('idle_seconds', 'max_place_seconds'):
        if key in config and (type(config[key]) is not int or config[key] < 1):
            raise ValueError(f'{key} phải là số nguyên dương.')
    if (not isinstance(config.get('areas'), list) or not config['areas']
            or not isinstance(config.get('keywords'), list) or not config['keywords']):
        raise ValueError('Cần ít nhất một khu vực và một từ khóa.')
    if any(not isinstance(k, str) or not k.strip() for k in config['keywords']):
        raise ValueError('Từ khóa phải là chuỗi không rỗng.')
    for area in config['areas']:
        if not isinstance(area, dict) or not isinstance(area.get('name'), str) or not area['name'].strip():
            raise ValueError('Mỗi khu vực cần name không rỗng.')
        terms = area.get('search_areas') or [area['name']]
        if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError('search_areas phải chứa các chuỗi không rỗng.')
    names = [area['name'] for area in config['areas']]
    if len(set(names)) != len(names):
        raise ValueError('Tên các khu vực phải khác nhau.')
    return config


def build_queries(config):
    groups = []
    seen = set()
    for area in config['areas']:
        queries = []
        for local in area.get('search_areas') or [area['name']]:
            for keyword in config['keywords']:
                query = f'{keyword} ở {local}'
                key = (area['name'], query)
                if key not in seen:
                    seen.add(key)
                    queries.append(key)
        groups.append(queries)
    for row in zip_longest(*groups):
        for item in row:
            if item is not None:
                yield item


def area_targets(config):
    """Reserve a fair share for every configured area, including on resume."""
    base, extra = divmod(config['target_restaurants'], len(config['areas']))
    return {area['name']: base + (index < extra)
            for index, area in enumerate(config['areas'])}


def area_places(queue, area):
    # A place found in overlapping searches consumes only one area's quota.
    # First discovery owns it; old queues already preserve this order in areas.
    return [item for item in queue['places'].values()
            if item.get('areas') and item['areas'][0] == area]


def selected_places(queue, targets):
    groups = [area_places(queue, area)[:limit] for area, limit in targets.items()]
    return [item for row in zip_longest(*groups) for item in row if item is not None]


def browser_alive(driver):
    if getattr(driver, '_transport_broken', False) is True:
        return False
    try:
        return driver.execute_script('return 1;') == 1
    except Exception:
        return False


def _feed(driver):
    return next((node for node in driver.find_elements(By.CSS_SELECTOR, '[role="feed"]')
                 if node.is_displayed()), None)


def add_place(queue, url, name, area, query):
    parsed = urlsplit(url)
    if parsed.hostname not in ('www.google.com', 'google.com') or '/maps/place/' not in parsed.path:
        return False
    key = gmap.place_id_from_url(url)
    is_new = key not in queue['places']
    item = queue['places'].setdefault(key, {
        'id': key, 'url': url, 'name': name, 'areas': [], 'queries': [],
        'status': 'pending', 'attempts': 0,
    })
    if area not in item['areas']:
        item['areas'].append(area)
    if query not in item['queries']:
        item['queries'].append(query)
    return is_new


def discover(driver, queue, queue_path, area, query, target, stop_event=None):
    # target is this area's quota, not the global queue length.
    record = queue['queries'].setdefault(query, {})
    record.update(status='running', area=area, started_at=now())
    gmap.write_json(queue_path, queue)
    driver.get('https://www.google.com/maps/search/' + quote_plus(query) + '/?hl=vi')
    try:
        feed = WebDriverWait(driver, 25, ignored_exceptions=(StaleElementReferenceException,)).until(_feed)
    except TimeoutException as exc:
        raise gmap.CrawlError(f'Không có danh sách kết quả cho {query!r}; kiểm tra trang chặn/cookie.') from exc
    no_new, found_here = 0, set()
    for _ in range(80):
        check_cancelled(stop_event)
        before = len(found_here)
        feed = _feed(driver)
        if feed is None:
            raise gmap.CrawlError('Khung kết quả tìm kiếm biến mất.')
        for link in feed.find_elements(By.CSS_SELECTOR, 'a[href*="/maps/place/"]'):
            try:
                url = link.get_attribute('href')
                name = link.get_attribute('aria-label') or link.text
                if url:
                    found_here.add(gmap.place_id_from_url(url))
                    add_place(queue, url, name, area, query)
            except StaleElementReferenceException:
                continue
            if len(area_places(queue, area)) >= target:
                record.update(status='target_reached', count=len(found_here), finished_at=now())
                gmap.write_json(queue_path, queue)
                return
        record['count'] = len(found_here)
        gmap.write_json(queue_path, queue)
        text = feed.text.casefold()
        if any(marker in text for marker in (
            "you've reached the end of the list", 'bạn đã xem hết danh sách', 'đã đến cuối danh sách'
        )):
            record['status'] = 'end_of_list'
            break
        no_new = no_new + 1 if len(found_here) == before else 0
        if no_new >= 5:
            # This is a bounded attempt, not a claim of geographic completeness.
            record['status'] = 'stalled'
            break
        driver.execute_script('arguments[0].scrollTop = arguments[0].scrollHeight;', feed)
        pause(1.5, stop_event)
    else:
        record['status'] = 'round_limit'
    record['finished_at'] = now()
    gmap.write_json(queue_path, queue)
    LOG.info('Tìm %s: %s quán trong lượt, tổng %s quán duy nhất (%s).',
             query, len(found_here), len(queue['places']), record['status'])


def _resume_done(item, output, limit):
    status_path = output.with_suffix('.status.json')
    if not status_path.exists():
        return False
    try:
        state = json.loads(status_path.read_text(encoding='utf-8'))
        if state.get('status') == 'no_reviews':
            # The per-place status may have been committed just before a crash
            # prevented the queue from being updated.
            item['status'] = 'no_reviews'
            return True
        if state.get('status') != 'completed' or not output.exists():
            return False
        rows = json.loads(output.read_text(encoding='utf-8'))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return False
        expected = state.get('place', {}).get('review_count')
        return bool(rows) and (len(rows) >= limit or (type(expected) is int and len(rows) >= expected))
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        LOG.warning('Không đọc được trạng thái cũ %s; thử lại quán, không dừng cả batch: %s', status_path, exc)
        return False


def run_pipeline(config, output_dir=None, *, headless=False, chrome_major=None,
                 discover_only=False, crawl_only=False, refresh=False, profile_dir=None, stop_event=None):
    # One browser per batch avoids 1,000 Chrome startups and keeps the same session.
    with ExitStack() as stack:
        holder = []
        def close_browser():
            if holder:
                driver = holder.pop()
                close_driver(driver)
        stack.callback(close_browser)
        def browser(restart=False):
            check_cancelled(stop_event)
            if restart:
                close_browser()
            if not holder:
                driver = gmap.make_driver(headless, chrome_major, profile_dir)
                holder.append(driver)
            return holder[0]
        return _run_pipeline(config, output_dir, browser, discover_only=discover_only,
                             crawl_only=crawl_only, refresh=refresh, headless=headless, stop_event=stop_event)


def _run_pipeline(config, output_dir, browser, *, discover_only=False, crawl_only=False, refresh=False, headless=False, stop_event=None):
    directory = Path(output_dir) if output_dir else gmap.default_output_dir()
    queue_path = directory / 'places_queue.json'
    queue = json.loads(queue_path.read_text(encoding='utf-8')) if queue_path.exists() else {
        'version': 1, 'queries': {}, 'places': {},
    }
    if (not isinstance(queue, dict) or queue.get('version') != 1
            or not isinstance(queue.get('places'), dict) or not isinstance(queue.get('queries'), dict)):
        raise ValueError('places_queue.json không đúng định dạng; không ghi đè.')
    target = config['target_restaurants']
    targets = area_targets(config)
    limit = config['max_reviews_per_restaurant']
    failures = []
    if not crawl_only:
        for area, query in build_queries(config):
            check_cancelled(stop_event)
            if len(area_places(queue, area)) >= targets[area]:
                continue
            if not refresh and queue['queries'].get(query, {}).get('status') == 'end_of_list':
                continue
            for attempt in range(1, 3):
                check_cancelled(stop_event)
                record = queue['queries'].setdefault(query, {})
                record.update(attempts=record.get('attempts', 0) + 1)
                try:
                    driver = browser()
                    if not browser_alive(driver):
                        LOG.warning('Phiên Chrome mất kết nối; khởi động lại trước khi tìm kiếm.')
                        driver = browser(restart=True)
                    LOG.info('Tìm [%s]: %s/%s quán; %s (lần %s/2).',
                             area, len(area_places(queue, area)), targets[area], query, attempt)
                    discover(driver, queue, queue_path, area, query, targets[area], stop_event=stop_event)
                    record.pop('error', None)
                    gmap.write_json(queue_path, queue)
                    break
                except CrawlCancelled:
                    record.update(status='interrupted', finished_at=now())
                    gmap.write_json(queue_path, queue)
                    raise
                except Exception as exc:
                    record.update(status='failed', area=area, error=str(exc), finished_at=now())
                    gmap.write_json(queue_path, queue)
                    LOG.error('Tìm kiếm lỗi (%s/2): %s: %s', attempt, query, exc)
            else:
                failures.append(query)
                LOG.warning('Đã thử hai lần; chuyển truy vấn/khu vực tiếp theo.')
    selected = selected_places(queue, targets)
    queue['last_run'] = {'started_at': now(), 'target': target, 'discovered': len(queue['places']),
                         'selected': len(selected), 'area_targets': targets,
                         'area_counts': {area: len(area_places(queue, area)) for area in targets}}
    gmap.write_json(queue_path, queue)
    if not queue['places']:
        raise gmap.CrawlError('Không tìm thấy quán nào; không có dữ liệu để crawl.')
    LOG.info('Chọn %s/%s quán cho lượt này; giữ tổng %s quán trong hàng đợi.',
             len(selected), target, len(queue['places']))
    for area, quota in targets.items():
        count = len(area_places(queue, area))
        LOG.info('[%s] Có %s quán, chọn tối đa %s.', area, count, quota)
        if count < quota:
            LOG.warning('[%s] Còn thiếu %s quán so với mục tiêu.', area, quota - count)
    if discover_only:
        shortfall = sum(max(0, quota - len(area_places(queue, area))) for area, quota in targets.items())
        queue['last_run'].update(finished_at=now(), failures=len(failures), shortfall=shortfall,
                                 status='partial' if failures or shortfall else 'completed')
        gmap.write_json(queue_path, queue)
        return 1 if failures or shortfall else 0
    consecutive_failures = 0
    work = selected if refresh else resume_order(selected, queue.get('last_processed_id'))
    skipped = 0
    for index, item in enumerate(work, 1):
        check_cancelled(stop_event)
        output = gmap.output_for_url(item['url'], directory)
        if not refresh and _resume_done(item, output, limit):
            if item.get('status') != 'no_reviews':
                item['status'] = 'completed'
            skipped += 1
            LOG.info('[%s/%s] Bỏ qua quán đã hoàn tất: %s', index, len(work), item['name'])
            continue
        LOG.info('[%s/%s] %s', index, len(selected), item['name'])
        item.update(status='running', attempts=item.get('attempts', 0) + 1)
        gmap.write_json(queue_path, queue)
        try:
            driver = browser()
            if not browser_alive(driver):
                driver = browser(restart=True)
            rows = gmap.scrape_gmap_reviews(item['url'], limit, output_path=output,
                search_area=item['areas'], browser=driver, headless=headless,
                login_wait=0, idle_seconds=config.get('idle_seconds', 20),
                max_place_seconds=config.get('max_place_seconds', 300), stop_event=stop_event,
                resume=not refresh)
            state = json.loads(output.with_suffix('.status.json').read_text(encoding='utf-8'))
            item.update(status=state['status'], count=len(rows),
                        stop_reason=state.get('stop_reason'),
                        output=state.get('checkpoint') if state['status'] == 'partial' else str(output))
            item.pop('error', None)
            consecutive_failures = 0
        except (KeyboardInterrupt, CrawlCancelled):
            item.update(status='interrupted', finished_at=now())
            gmap.write_json(queue_path, queue)
            raise
        except Exception as exc:
            item.update(status='failed', error=str(exc))
            failures.append(item['id'])
            consecutive_failures += 1
            LOG.error('Giữ tiến độ và chuyển quán tiếp theo: %s', exc)
        item['finished_at'] = now()
        queue['last_processed_id'] = item['id']
        gmap.write_json(queue_path, queue)
        if consecutive_failures >= 3:
            LOG.error('Dừng sau 3 quán lỗi liên tiếp; kiểm tra diagnostics trước khi chạy lại.')
            break
    incomplete = sum(item.get('status') not in ('completed', 'no_reviews') for item in selected)
    shortfall = sum(max(0, quota - len(area_places(queue, area))) for area, quota in targets.items())
    queue['last_run'].update(finished_at=now(), failures=len(failures), incomplete=incomplete, skipped=skipped,
                             shortfall=shortfall,
                             status='partial' if failures or incomplete or shortfall else 'completed')
    gmap.write_json(queue_path, queue)
    return 1 if failures or incomplete or shortfall else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('gmap_areas.json'))
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--chrome-major', type=int)
    parser.add_argument('--target-restaurants', type=int, help='Ghi đè mục tiêu số quán trong cấu hình.')
    parser.add_argument('--max-reviews', type=int, help='Ghi đè số review tối đa mỗi quán.')
    parser.add_argument('--profile-dir', type=Path, help='Dùng một thư mục hồ sơ Chrome riêng, không dùng hồ sơ đang mở.')
    parser.add_argument('--login', action='store_true', help='Mở hồ sơ riêng để bạn đăng nhập thủ công rồi thoát.')
    parser.add_argument('--refresh', action='store_true', help='Crawl lại cả quán đã hoàn tất.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--discover-only', action='store_true')
    mode.add_argument('--crawl-only', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        if args.login:
            if not args.profile_dir or args.headless:
                parser.error('--login cần --profile-dir và không dùng --headless.')
            driver = gmap.make_driver(False, args.chrome_major, args.profile_dir)
            try:
                driver.get('https://www.google.com/maps/?hl=vi')
                input('Tự đăng nhập Google Maps trong Chrome nếu cần. Xong thì nhấn Enter ở đây để đóng: ')
            finally:
                driver.quit()
            return 0
        config = load_config(args.config)
        for arg, key in ((args.target_restaurants, 'target_restaurants'),
                         (args.max_reviews, 'max_reviews_per_restaurant')):
            if arg is not None:
                if arg < 1:
                    parser.error('Giới hạn quán/review phải lớn hơn 0.')
                config[key] = arg
        return run_pipeline(config, args.output_dir, headless=args.headless,
                            chrome_major=args.chrome_major, discover_only=args.discover_only,
                            crawl_only=args.crawl_only, refresh=args.refresh, profile_dir=args.profile_dir)
    except KeyboardInterrupt:
        LOG.warning('Đã dừng; chạy lại cùng lệnh để tiếp tục.')
        return 130
    except Exception as exc:
        LOG.error('%s: %s', type(exc).__name__, exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
