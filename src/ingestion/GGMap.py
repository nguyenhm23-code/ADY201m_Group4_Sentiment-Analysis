"""Google Maps review crawler -> Foody-compatible JSON."""

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import undetected_chromedriver as uc
if __package__:
    from .ingestion_runtime import CrawlCancelled, check_cancelled, close_driver
    from .chrome_version import make_chrome
    from .browser_health import diagnostic_budget, connection_broken
else:
    from ingestion_runtime import CrawlCancelled, check_cancelled, close_driver
    from chrome_version import make_chrome
    from browser_health import diagnostic_budget, connection_broken
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

LOG = logging.getLogger(__name__)

DEFAULT_URL = (
    "https://www.google.com/maps/place/Ch%C3%B2i+Chill/"
    "@13.7968284,109.2193517,15z/data=!4m8!3m7!"
    "1s0x316f6b000b5c14f1:0x3aeebb5c913dc9fc!"
    "8m2!3d13.7957087!4d109.2156351!9m1!1b1!16s%2Fg%2F11z94yphmj"
)

CARD = "div.jftiEf[data-review-id]"

REVIEW_WORDS = ("bài đánh giá", "đánh giá", "reviews", "review")
MORE_WORDS = (
    "xem các bài đánh giá khác",
    "bài đánh giá khác",
    "xem tất cả bài đánh giá",
    "xem tất cả đánh giá",
    "other reviews",
    "more reviews",
    "all reviews",
    "see all reviews",
)
BAD_WORDS = ("viết bài đánh giá", "write a review", "đánh giá của bạn", "your review")


class CrawlError(RuntimeError):
    pass


class ReviewAccessError(CrawlError):
    """The current browser session cannot access the full review list."""


class SafeChrome(uc.Chrome):
    def quit(self):
        if getattr(self, "_closed", False):
            return

        try:
            super().quit()
        except OSError as exc:
            if getattr(exc, "winerror", None) != 6:
                raise

        self._closed = True

    def __del__(self):
        try:
            self.quit()
        except Exception:
            pass


def parse_rating(raw):
    match = re.search(r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?![\d.,])", str(raw or ""))
    value = float(match.group(1).replace(",", ".")) if match else None
    return value if value is not None and 1 <= value <= 5 else None


def place_id_from_url(url):
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)

    for key in ("query_place_id", "cid"):
        if params.get(key):
            return params[key][0]

    # Photo URLs may contain an image !1s before the restaurant's !1s.
    place_match = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)(?:!|/|\?|$)", unquote(url), re.I)
    if place_match:
        return place_match.group(1)
    match = re.search(r"!1s([^!/?]+)", unquote(url))

    if match:
        return match.group(1)

    canonical = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "&".join(
                f"{key}={value[0]}"
                for key, value in sorted(params.items())
                if key in ("q", "query", "query_place_id", "cid")
            ),
            "",
        )
    )

    return "url_" + hashlib.sha256(canonical.encode()).hexdigest()[:20]


def city_from_address(address):
    aliases = {
        "quy nhơn": "Quy Nhơn",
        "quy nhon": "Quy Nhơn",
        "hà nội": "Hà Nội",
        "hanoi": "Hà Nội",
        "hồ chí minh": "Hồ Chí Minh",
        "ho chi minh": "Hồ Chí Minh",
        "ho chi minh city": "Hồ Chí Minh",
        "đà nẵng": "Đà Nẵng",
        "cần thơ": "Cần Thơ",
        "hải phòng": "Hải Phòng",
        "huế": "Huế",
        "nha trang": "Nha Trang",
        "đà lạt": "Đà Lạt",
        "vũng tàu": "Vũng Tàu",
        "biên hòa": "Biên Hòa",
    }

    for part in reversed((address or "").split(",")):
        token = re.sub(r"^(?:thành phố\s+|tp\.?\s*)", "", part.strip(), flags=re.I).casefold()
        # Maps appends a postal code to the city in many Vietnamese addresses.
        token = re.sub(r"\s+\d{5,6}$", "", token).strip()

        if token in aliases:
            return aliases[token]

    return None


def review_id(raw, rating, text):
    if raw.get("review_id"):
        return str(raw["review_id"])

    payload = [raw.get("author_url"), raw.get("username"), rating, raw.get("date"), text]

    return "sha256_" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode()
    ).hexdigest()


def normalize_review(raw, place):
    rating = parse_rating(raw.get("rating"))
    text = (raw.get("text") or "").strip()
    dt = raw.get("datetime")

    try:
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00")).isoformat() if dt else None
    except ValueError:
        dt = None

    return {
        "Thành Phố": place.get("city"),
        "ID Quán": place["id"],
        "URL Quán": place["url"],
        "Tên User": raw.get("username") or None,
        "Điểm Đánh Giá": rating,
        "Thiết Bị": None,
        "Ngày Giờ": raw.get("date") or None,
        "Bình Luận": text,
        "Nguồn": "Google Maps",
        "ID Review": review_id(raw, rating, text),
        "Tên Quán": place.get("name"),
        "Địa Chỉ": place.get("address"),
        "Điểm Trung Bình Quán": place.get("rating"),
        "Tổng Số Đánh Giá Quán": place.get("review_count"),
        "Vĩ Độ": place.get("latitude"),
        "Kinh Độ": place.get("longitude"),
        "Khu Vực Tìm Kiếm": place.get("search_area"),
        "Nguồn Thành Phố": place.get("city_source"),
        "Ngày Đăng ISO": dt,
        "Thời Điểm Thu Thập": place.get("collected_at"),
        "Thang Điểm": 5,
    }


def write_json(path, payload):
    if isinstance(payload, list) and not payload:
        raise ValueError("Không ghi đè dữ liệu bằng danh sách rỗng.")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None

    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temp = Path(file.name)
            json.dump(payload, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp, path)

    finally:
        if temp:
            temp.unlink(missing_ok=True)


def first_text(context, *selectors):
    for selector in selectors:
        for element in context.find_elements(By.CSS_SELECTOR, selector):
            try:
                text = (element.text or "").strip()

                if text:
                    return text
            except StaleElementReferenceException:
                pass

    return None


def label(element):
    try:
        return (
            (element.get_attribute("aria-label") or "")
            + " "
            + (element.text or "")
        ).strip()
    except StaleElementReferenceException:
        return ""


def cards(driver):
    result = []

    for card in driver.find_elements(By.CSS_SELECTOR, CARD):
        try:
            if card.is_displayed():
                result.append(card)
        except StaleElementReferenceException:
            pass

    return result


def safe_click(driver, element):
    try:
        element.click()
        return
    except (ElementClickInterceptedException, ElementNotInteractableException):
        pass

    driver.execute_script(
        """
        arguments[0].scrollIntoView({
            block: 'center'
        });
        """,
        element,
    )

    WebDriverWait(driver, 5).until(
        lambda _: element.is_displayed() and element.is_enabled()
    )

    try:
        element.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", element)


def find_by_words(driver, selector, words, bad=()):
    for element in driver.find_elements(By.CSS_SELECTOR, selector):
        try:
            if not element.is_displayed():
                continue

            text = label(element).casefold()

            if (
                text
                and any(word in text for word in words)
                and not any(word in text for word in bad)
            ):
                return element

        except StaleElementReferenceException:
            pass

    return None


def review_tab(driver):
    return find_by_words(
        driver,
        "[role='tab'], button[jsaction*='tabs.tabClick']",
        REVIEW_WORDS,
        BAD_WORDS,
    )


def more_reviews_button(driver):
    button = find_by_words(
        driver,
        "button, [role='button']",
        MORE_WORDS,
        BAD_WORDS,
    )

    if button:
        return button

    for element in driver.find_elements(By.CSS_SELECTOR, "button, [role='button']"):
        text = label(element).casefold()

        if "đánh giá" in text and "khác" in text and re.search(r"\d+", text):
            return element

    return None


def limited_mode(driver):
    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").casefold()
    except WebDriverException:
        return False

    return "chế độ bị hạn chế" in body or "limited view of google maps" in body


def review_ui(driver):
    return bool(cards(driver) or review_tab(driver) or more_reviews_button(driver))


def login_button(driver):
    return find_by_words(
        driver,
        "button, [role='button'], a",
        ("đăng nhập", "sign in"),
    )


def wait_review_access(driver, timeout=120, headless=False):
    if review_ui(driver):
        return

    if not limited_mode(driver):
        return

    if headless or timeout <= 0:
        raise CrawlError(
            "Google Maps đang ở chế độ bị hạn chế. "
            "Phiên này không cung cấp review; kiểm tra diagnostics hoặc hồ sơ Chrome."
        )

    button = login_button(driver)

    if button:
        LOG.warning(
            "Maps đang ở chế độ bị hạn chế. "
            "Đang mở trang đăng nhập; "
            "hãy đăng nhập Google trong Chrome."
        )

        try:
            safe_click(driver, button)
        except WebDriverException:
            pass

    else:
        LOG.warning("Maps đang ở chế độ bị hạn chế. Hãy đăng nhập Google trong Chrome.")

    end = time.time() + timeout

    while time.time() < end:
        if "google.com/maps" in driver.current_url and review_ui(driver):
            LOG.info("Đã có quyền xem review.")
            return

        time.sleep(1)

    raise CrawlError(
        "Hết thời gian chờ; phiên Google hiện tại vẫn không cung cấp review."
    )


def place_snapshot(driver):
    """Read the rendered business panel in one call, including offscreen text."""
    return driver.execute_script(r"""
        const clean = value => (value || '').replace(/\s+/g, ' ').trim();
        const visible = el => el.getClientRects().length &&
            getComputedStyle(el).display !== 'none' &&
            getComputedStyle(el).visibility !== 'hidden';
        const panels = Array.from(document.querySelectorAll('[role="main"]'));
        for (const panel of panels.filter(visible)) {
            const heading = panel.querySelector('h1.DUwDvf, h1.fontHeadlineLarge, h1');
            const address = panel.querySelector('[data-item-id="address"]');
            const tabs = Array.from(panel.querySelectorAll('[role="tab"]'));
            const reviewTab = tabs.some(el => /đánh giá|reviews?/i.test(
                (el.getAttribute('aria-label') || '') + ' ' + el.textContent));
            // Reject search results and photo-only panels.
            if (!address && !reviewTab) continue;
            const name = clean(heading && heading.textContent) ||
                         clean(panel.getAttribute('aria-label'));
            if (!name) continue;
            let addressText = address && address.querySelector('.Io6YTe');
            const labels = Array.from(panel.querySelectorAll('[aria-label]'))
                .filter(el => !el.closest('div.jftiEf[data-review-id]') && visible(el))
                .map(el => clean(el.getAttribute('aria-label')));
            return {name: name, panel_label: clean(panel.getAttribute('aria-label')),
                    name_source: heading ? 'heading_text_content' : 'main_aria_label',
                    address: clean(addressText && addressText.textContent) ||
                             clean(address && address.getAttribute('aria-label'))
                                 .replace(/^(địa chỉ|address)\s*:\s*/i, '') || null,
                    labels: labels, url: location.href, review_tab: reviewTab};
        }
        return null;
    """)


def _place_name_in_url(url):
    match = re.search(r'/maps/place/([^/]+)', urlsplit(url).path)
    return unquote(match.group(1).replace('+', ' ')) if match else None


def _same_place_name(left, right):
    def normalized(value):
        return re.sub(r'[^\w]+', '', value.casefold(), flags=re.UNICODE)
    return normalized(left) == normalized(right)


def load_place(driver, url, city=None, *, stop_event=None, timeout=30, attempts=2):
    expected_name = _place_name_in_url(url)
    expected_id = place_id_from_url(url)
    trace = {'requested_url': url, 'expected_name': expected_name, 'attempts': 0}
    driver._gmap_load_trace = trace

    def ready(d):
        check_cancelled(stop_event)
        snapshot = place_snapshot(d)
        trace['snapshot'] = snapshot
        if not snapshot:
            trace['reason'] = 'Không thấy khung thông tin quán có địa chỉ hoặc tab đánh giá.'
            return False
        actual_id = place_id_from_url(snapshot['url'])
        if expected_id.startswith('0x') and actual_id != expected_id:
            trace['reason'] = 'Trang chưa chuyển tới đúng ID quán yêu cầu.'
            return False
        # The URL can update before the old business panel has been replaced.
        if expected_name and not _same_place_name(expected_name, snapshot['name']):
            actual_name = _place_name_in_url(snapshot['url'])
            # Accept Google's canonical rename only when URL and panel agree,
            # and both URLs carry the same stable Maps ID.
            canonical_rename = (expected_id.startswith('0x') and actual_id == expected_id
                                and actual_name and actual_name != expected_name
                                and _same_place_name(actual_name, snapshot['name']))
            if not canonical_rename:
                trace['reason'] = 'Khung thông tin vẫn hiển thị tên quán khác.'
                return False
        trace['reason'] = 'ready'
        return snapshot

    snapshot = None
    for attempt in range(1, attempts + 1):
        check_cancelled(stop_event)
        trace['attempts'] = attempt
        try:
            driver.get(url)
        except TimeoutException:
            # Maps can render the business panel while other requests time out.
            LOG.warning('Tải trang quá thời gian; kiểm tra khung quán đã hiển thị.')
        try:
            snapshot = WebDriverWait(driver, timeout, poll_frequency=0.5,
                ignored_exceptions=(StaleElementReferenceException,)).until(ready)
            break
        except TimeoutException:
            if attempt < attempts:
                LOG.warning('Chưa xác nhận được trang quán (%s); tải lại lần %s/%s.',
                            trace.get('reason'), attempt + 1, attempts)
    if snapshot is None:
        raise CrawlError(f"Không xác nhận được trang chi tiết sau {attempts} lần: "
                         f"{trace.get('reason', 'không có dữ liệu')}")

    address = snapshot['address']
    rating = None
    review_count = None
    for text in snapshot['labels']:
        if rating is None and re.fullmatch(r'[\d.,]+\s+(?:sao|stars?)(?:\s+.*)?', text, re.I):
            rating = parse_rating(text)
        match = re.fullmatch(r'([\d.,\s]+)\s+(?:bài đánh giá|đánh giá|reviews?)', text, re.I)
        if review_count is None and match:
            digits = re.sub(r'\D', '', match.group(1))
            review_count = int(digits) if digits else None
    coords = re.search(r'!3d(-?[\d.]+)!4d(-?[\d.]+)', unquote(snapshot['url']))
    resolved_city = city or city_from_address(address)
    return {
        'id': expected_id if not expected_id.startswith('url_') else place_id_from_url(snapshot['url']),
        'url': url, 'name': snapshot['name'], 'address': address,
        'city': resolved_city,
        'city_source': ('cấu hình người dùng' if city else 'địa chỉ Google Maps') if resolved_city else None,
        'rating': rating, 'review_count': review_count,
        'latitude': float(coords.group(1)) if coords else None,
        'longitude': float(coords.group(2)) if coords else None,
        'collected_at': datetime.now(timezone.utc).isoformat(),
    }


def review_panel_state(driver):
    """Distinguish overview previews, a full list, and an access dialog.

    Read DOM text even for controls outside the scroll viewport. Selenium's
    .text/is_displayed can miss the review tab after a previous scroll.
    """
    return driver.execute_script(r"""
        const shown = el => el && el.getClientRects().length &&
            getComputedStyle(el).display !== 'none' &&
            getComputedStyle(el).visibility !== 'hidden';
        const text = el => ((el?.getAttribute('aria-label') || '') + ' ' +
                           (el?.textContent || '')).replace(/\s+/g, ' ').trim();
        const dialogs = [...document.querySelectorAll('[role=dialog], [aria-modal=true]')].filter(shown);
        const login = dialogs.some(el => /đăng nhập|sign in/i.test(text(el))) ||
            location.hostname === 'accounts.google.com';
        const restricted = /chế độ bị hạn chế|limited view of google maps/i.test(document.body.innerText);
        const panels = [...document.querySelectorAll('[role=main]')].filter(shown);
        const panel = panels.find(el => el.querySelector('[role=tab]') ||
            el.querySelector('div.jftiEf[data-review-id]'));
        if (!panel) return {full: false, login_required: login, restricted: restricted,
            cards: 0, selected_tab: null, more: false};
        const tabs = [...panel.querySelectorAll('[role=tab]')];
        const review = tabs.find(el => /^(bài đánh giá|đánh giá|reviews?)$/i.test(el.textContent.trim()));
        const selected = tabs.find(el => el.getAttribute('aria-selected') === 'true');
        const buttons = [...panel.querySelectorAll('button, [role=button]')].filter(shown);
        const more = buttons.some(el => !el.closest('div.jftiEf[data-review-id]') &&
            /bài đánh giá khác|xem tất cả (bài )?đánh giá|other reviews|more reviews|all reviews/i.test(text(el)));
        const sort = buttons.some(el => !el.closest('div.jftiEf[data-review-id]') &&
            (((el.getAttribute('aria-haspopup') === 'true' || el.getAttribute('aria-haspopup') === 'menu') &&
            /sắp xếp|sort reviews|sort by|most relevant|phù hợp nhất|mới nhất|newest|highest rating|lowest rating|xếp hạng cao nhất|xếp hạng thấp nhất/i.test(text(el))) ||
            /^(sort|sắp xếp)$/i.test(el.getAttribute('data-value') || '')));
        const count = [...panel.querySelectorAll('div.jftiEf[data-review-id]')].filter(shown).length;
        const selectedReview = !!review && review.getAttribute('aria-selected') === 'true';
        // The selected tab alone also occurs in the five-review preview UI.
        // Its expander can briefly disappear while repainting: require toolbar.
        return {full: !login && !more && count > 0 && sort && (selectedReview || !tabs.length),
            login_required: login, restricted: restricted, cards: count,
            selected_tab: selected ? text(selected) : null,
            review_selected: selectedReview, sort: sort, more: more};
    """)


def review_control(driver, kind):
    """Locate offscreen tab/expander inside the current business panel."""
    return driver.execute_script(r"""
        const shown = el => el.getClientRects().length &&
            getComputedStyle(el).visibility !== 'hidden';
        const panels = [...document.querySelectorAll('[role=main]')].filter(shown);
        for (const panel of panels) {
            if (arguments[0] === 'tab') {
                const tab = [...panel.querySelectorAll('[role=tab]')].find(el => shown(el) &&
                    /^(bài đánh giá|đánh giá|reviews?)$/i.test(el.textContent.trim()));
                if (tab) return tab;
            } else {
                const button = [...panel.querySelectorAll('button, [role=button]')].find(el =>
                    shown(el) && !el.closest('div.jftiEf[data-review-id]') &&
                    /bài đánh giá khác|xem tất cả (bài )?đánh giá|other reviews|more reviews|all reviews/i.test(
                        (el.getAttribute('aria-label') || '') + ' ' + el.textContent));
                if (button) return button;
            }
        }
        return null;
    """, kind)


def open_reviews(driver, headless=False, login_wait=120, stop_event=None, timeout=45):
    """Open and verify the full list, with bounded retries and one page reload."""
    deadline = time.monotonic() + timeout
    trace = []
    driver._gmap_review_trace = trace

    def state():
        check_cancelled(stop_event)
        current = review_panel_state(driver)
        if current.get('login_required'):
            raise ReviewAccessError(
                'Google Maps yêu cầu đăng nhập hoặc hạn chế xem đầy đủ đánh giá. '
                'Mở --login bằng đúng --profile-dir rồi chạy lại; không coi review xem trước là đầy đủ.')
        return current

    def wait_for(predicate, seconds=10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            return WebDriverWait(driver, min(seconds, remaining), poll_frequency=0.4,
                ignored_exceptions=(StaleElementReferenceException,)).until(lambda _: predicate(state()))
        except TimeoutException:
            return predicate(state())

    def activate(kind, predicate):
        """A successful WebDriver click is not proof that Maps changed tabs."""
        for method in ('native', 'dom'):
            if time.monotonic() >= deadline:
                return False
            button = review_control(driver, kind)
            if button is None:
                return False
            action = {'control': kind, 'method': method, 'label': label(button)}
            trace[-1].setdefault('actions', []).append(action)
            if method == 'native':
                # Let WebDriver scroll to the element. An extra smooth scroll
                # can move the target between the coordinate calculation/click.
                safe_click(driver, button)
            else:
                driver.execute_script('arguments[0].click();', button)
            changed = wait_for(predicate, 4)
            action['after'] = state()
            action['changed'] = bool(changed)
            if changed:
                return True
        return False

    access_error = None
    reloaded = False
    for attempt in range(3):
        check_cancelled(stop_event)
        if time.monotonic() >= deadline:
            break
        if not reloaded and (attempt == 2 or access_error is not None):
            LOG.warning('Chưa mở được danh sách đánh giá; tải lại quán một lần.')
            page_timeout = driver.timeouts.page_load
            try:
                driver.set_page_load_timeout(max(0.5, min(10, deadline - time.monotonic())))
                driver.refresh()
            except TimeoutException:
                pass
            finally:
                driver.set_page_load_timeout(page_timeout)
            reloaded = True
        try:
            current = state()
            trace.append({'attempt': attempt + 1, 'before': current})
            if current['full']:
                return
            # Wait for the business panel to finish replacing the loading shell.
            if not current.get('selected_tab'):
                wait_for(lambda s: s.get('selected_tab') or s['full'], 5)
                current = state()
            if not current.get('review_selected'):
                activate('tab', lambda s: s['full'] or s.get('review_selected'))
            current = state()
            if current['full']:
                return
            if current.get('more'):
                if activate('more', lambda s: s['full']):
                    return
            if wait_for(lambda s: s['full']):
                return
            trace[-1]['after'] = state()
        except ReviewAccessError as exc:
            # A cold profile may initially render the logged-out shell. Retry
            # the same page once; a persistent login dialog is an access error.
            access_error = exc
            trace.append({'attempt': attempt + 1, 'access_error': str(exc)})
            if reloaded:
                raise
        except (StaleElementReferenceException, ElementClickInterceptedException,
                ElementNotInteractableException):
            LOG.warning('Khung đánh giá thay đổi khi bấm; tìm lại nút.')
    if access_error is not None:
        raise access_error
    current = state()
    if current.get('restricted'):
        raise ReviewAccessError('Google Maps đang ở chế độ bị hạn chế và không mở được danh sách '
                                'đánh giá đầy đủ. Đăng nhập bằng đúng --profile-dir rồi chạy lại.')
    raise CrawlError('Không mở được danh sách đánh giá đầy đủ trong thời gian cho phép; '
                     f"tab={current.get('selected_tab')!r}, cards={current.get('cards')}, "
                     f"more={current.get('more')}. Xem diagnostics.")


def read_card(driver, card):
    buttons = card.find_elements(
        By.CSS_SELECTOR,
        "button.w8nwRe, button[jsaction*='expandReview']",
    )

    for button in buttons:
        text = label(button).casefold()

        if "xem thêm" in text or "more" in text:
            try:
                safe_click(driver, button)
                time.sleep(0.1)
            except WebDriverException:
                pass

            break

    username = first_text(card, ".d4r55") or card.get_attribute("aria-label")
    rating = None

    for element in card.find_elements(
        By.CSS_SELECTOR,
        "span.kvMYJc, [role='img'][aria-label]",
    ):
        text = element.get_attribute("aria-label") or ""

        if "sao" in text.casefold() or "star" in text.casefold():
            rating = parse_rating(text)

            if rating is not None:
                break

    text = first_text(card, "span.wiI7pd", ".MyEned span") or ""
    date = first_text(card, "span.rsqaWe", ".rsqaWe")

    authors = card.find_elements(
        By.CSS_SELECTOR,
        "button[data-href*='/maps/contrib/'], a[href*='/maps/contrib/']",
    )

    author_url = ""

    if authors:
        author_url = (
            authors[0].get_attribute("data-href")
            or authors[0].get_attribute("href")
            or ""
        )

    dates = card.find_elements(By.CSS_SELECTOR, "time[datetime], [datetime]")

    if rating is None and not text:
        raise CrawlError("Review card không có rating/comment.")

    return {
        "review_id": card.get_attribute("data-review-id"),
        "username": username,
        "rating": rating,
        "date": date,
        "text": text,
        "author_url": author_url,
        "datetime": dates[0].get_attribute("datetime") if dates else None,
    }


def scroll_container(driver, card):
    # The nearest scrolling ancestor belongs to the review list.
    # A larger outer page can also scroll, but scrolling it does not load reviews.
    return driver.execute_script(
        """
        let node = arguments[0].parentElement;

        while (node && node !== document.body) {
            const style = getComputedStyle(node);

            if (
                node.clientHeight > 0 &&
                node.scrollHeight > node.clientHeight + 3 &&
                ['auto', 'scroll', 'overlay'].includes(style.overflowY)
            ) {
                return node;
            }

            node = node.parentElement;
        }

        return null;
        """,
        card,
    )


def scroll_reviews(driver, card):
    box = scroll_container(driver, card)

    if box is None:
        return None

    distance = driver.execute_script('return Math.min(650, Math.max(250, arguments[0].clientHeight * .7));', box)
    # Wheel input follows the same lazy-load path as a user scrolling the panel.
    ActionChains(driver).scroll_from_origin(ScrollOrigin.from_element(box), 0, int(distance)).perform()


def current_ids(driver):
    # One browser call per poll instead of one WebDriver call per old review.
    return set(
        driver.execute_script(
            """
            return Array.from(document.querySelectorAll(arguments[0]))
                .filter(el => el.getClientRects().length)
                .map(el => el.getAttribute('data-review-id'))
                .filter(Boolean);
            """,
            CARD,
        )
    )


def review_scroll_state(driver):
    visible = cards(driver)

    if not visible:
        return None

    box = scroll_container(driver, visible[-1])

    if box is None:
        return None

    return driver.execute_script(
        """
        const el = arguments[0];

        return {
            top: el.scrollTop,
            height: el.scrollHeight,
            bottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 3,
            loading: Array.from(
                el.querySelectorAll('[role=progressbar], [aria-busy=true]')
            ).some(n => n.getClientRects().length)
        };
        """,
        box,
    )


def collect_reviews(
    driver,
    place,
    limit,
    checkpoint,
    initial=None,
    headless=False,
    login_wait=120,
    idle_seconds=20,
    max_place_seconds=300,
    poll_seconds=4,
    stop_event=None,
):
    if min(idle_seconds, max_place_seconds, poll_seconds) <= 0:
        raise ValueError("Các giới hạn chờ phải lớn hơn 0.")
    check_cancelled(stop_event)

    started = time.monotonic()

    seen = {
        row["ID Review"]: row
        for row in (initial or [])
        if row.get("ID Review")
    }

    seen = dict(list(seen.items())[:limit])

    if len(seen) >= limit:
        return list(seen.values()), "max_reviews"

    open_reviews(
        driver,
        headless,
        min(login_wait, max_place_seconds),
        stop_event=stop_event,
        timeout=min(45, max_place_seconds),
    )

    last_progress = time.monotonic()
    bottom_rounds = 0
    previous_bottom = None
    traversed = set()
    panel_recoveries = 0
    scroll_frontier = 0

    # Resume must be able to scroll past historical IDs to reach new reviews.
    # Only IDs visited for the first time in THIS traversal reset the idle timer;
    # recycling the same cards cannot keep a stalled place alive indefinitely.
    for round_no in range(1, 1501):
        check_cancelled(stop_event)
        if time.monotonic() - started >= max_place_seconds:
            return list(seen.values()), "time_limit"

        before = len(seen)
        if not review_panel_state(driver).get('full'):
            if panel_recoveries >= 1:
                return list(seen.values()), 'review_panel_lost'
            panel_recoveries += 1
            LOG.warning('Rời danh sách đánh giá; mở lại một lần, giữ checkpoint.')
            open_reviews(driver, headless, login_wait, stop_event,
                         timeout=min(30, max_place_seconds - (time.monotonic() - started)))
        visible = cards(driver)

        if not visible:
            raise CrawlError("Review cards biến mất trong lúc crawl.")

        for card in visible:
            if (
                len(seen) >= limit
                or time.monotonic() - started >= max_place_seconds
            ):
                break

            try:
                rid = card.get_attribute("data-review-id")
                if rid and rid not in traversed:
                    traversed.add(rid)
                    last_progress = time.monotonic()

                if rid and rid in seen:
                    continue

                row = normalize_review(read_card(driver, card), place)
                seen[row["ID Review"]] = row

            except StaleElementReferenceException:
                continue

            # Parsing errors must remain visible; never silently discard them.

        if len(seen) > before:
            checkpoint(list(seen.values()))
            last_progress = time.monotonic()
            bottom_rounds = 0

        LOG.info(
            "Vòng %s: %s review duy nhất (+%s).",
            round_no,
            len(seen),
            len(seen) - before,
        )

        if len(seen) >= limit:
            return list(seen.values()), "max_reviews"

        expected = place.get("review_count")

        if expected is not None and expected > 0 and len(seen) >= expected:
            return list(seen.values()), "displayed_review_count"

        elapsed = time.monotonic() - started
        idle = time.monotonic() - last_progress

        if elapsed >= max_place_seconds:
            return list(seen.values()), "time_limit"

        if idle >= idle_seconds:
            return list(seen.values()), "stalled"

        try:
            scroll_reviews(driver, visible[-1])

            # Poll against ALL saved IDs, not just the currently mounted cards.
            known = set(seen)

            WebDriverWait(
                driver,
                min(
                    poll_seconds,
                    idle_seconds - idle,
                    max_place_seconds - elapsed,
                ),
                poll_frequency=0.4,
                ignored_exceptions=(StaleElementReferenceException,),
            ).until(
                lambda d: bool(current_ids(d) - known)
            )

            bottom_rounds = 0
            previous_bottom = None

        except TimeoutException:
            try:
                position = review_scroll_state(driver)
            except StaleElementReferenceException:
                position = None

            if position and position["bottom"] and not position["loading"]:
                signature = (position["height"], len(seen))

                bottom_rounds = (
                    bottom_rounds + 1
                    if signature == previous_bottom
                    else 1
                )

                previous_bottom = signature

                if bottom_rounds >= 3:
                    # Stable bottom is only the end of available UI results.
                    # It is not proof that every published review was loaded.
                    return list(seen.values()), "end_of_visible_reviews"

            else:
                bottom_rounds = 0
                previous_bottom = None
            if position and position.get('top', 0) > scroll_frontier + 3:
                # Long/photo-heavy reviews can take many scrolls before new
                # IDs appear. Count forward traversal, never oscillation.
                scroll_frontier = position['top']
                last_progress = time.monotonic()

            LOG.info(
                "Chưa có review mới; chờ tối đa %.0fs nữa ở quán này.",
                max(
                    0,
                    idle_seconds
                    - (time.monotonic() - last_progress),
                ),
            )

        except StaleElementReferenceException:
            continue

    return list(seen.values()), "round_limit"


def default_output_dir():
    folder = Path(__file__).resolve().parent

    if folder.name == "ingestion" and folder.parent.name == "src":
        return folder.parent.parent / "data" / "raw" / "gmap"

    return folder / "data" / "gmap"


def output_for_url(url, directory):
    key = re.sub(
        r"[^A-Za-z0-9_-]",
        "_",
        place_id_from_url(url),
    )[:100]

    return Path(directory) / f"gmap_{key}.json"


def diagnostics(driver, output):
    """Best effort only: diagnostic failure must never erase partial crawl success."""
    if driver is None:
        return {}
    directory = output.parent / "diagnostics"
    stem = directory / f"{output.stem}_{datetime.now():%Y%m%d_%H%M%S}"
    result = {}
    state = {"place_load": getattr(driver, "_gmap_load_trace", None),
             "review_open_attempts": getattr(driver, "_gmap_review_trace", None)}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if getattr(driver, '_transport_broken', False):
            state['capture_error'] = 'ChromeDriver connection broken; skipped browser commands.'
        else:
            try:
                with diagnostic_budget(driver):
                    # Small structured evidence first; /source may stall on a busy renderer.
                    state.update(url=driver.current_url, review_panel=review_panel_state(driver),
                                 review_scroll=review_scroll_state(driver))
                    png = stem.with_suffix('.png')
                    if driver.save_screenshot(str(png)):
                        result['png'] = str(png)
                    html = stem.with_suffix('.html')
                    html.write_text(driver.page_source, encoding='utf-8')
                    result['html'] = str(html)
            except Exception as exc:
                state['capture_error'] = f'{type(exc).__name__}: {exc}'
                if connection_broken(exc):
                    driver._transport_broken = True
        state_path = stem.with_suffix('.state.json')
        write_json(state_path, state)
        result['state'] = str(state_path)
    except Exception as exc:
        LOG.warning('Không lưu được diagnostics; giữ nguyên kết quả crawl: %s', exc)
    return result


def make_driver(headless=False, chrome_major=None, profile_dir=None):
    options = uc.ChromeOptions()
    options.add_argument("--lang=vi-VN")
    options.add_argument("--window-size=1440,1000")

    profile = (
        Path(profile_dir)
        if profile_dir
        else default_output_dir() / ".chrome_profile"
    )

    profile.mkdir(parents=True, exist_ok=True)

    driver = make_chrome(
        'gmap', driver_class=SafeChrome,
        options=options,
        chrome_major=chrome_major,
        headless=headless,
        profile_dir=profile,
    )

    driver.set_page_load_timeout(60)
    return driver


def scrape_gmap_reviews(
    place_url,
    max_reviews=100,
    *,
    city=None,
    output_path=None,
    headless=False,
    chrome_major=None,
    search_area=None,
    profile_dir=None,
    browser=None,
    login_wait=120,
    idle_seconds=20,
    max_place_seconds=300,
    stop_event=None,
    resume=True,
):
    if (
        isinstance(max_reviews, bool)
        or not isinstance(max_reviews, int)
        or max_reviews < 1
    ):
        raise ValueError("max_reviews phải là số nguyên > 0.")

    output = (
        Path(output_path)
        if output_path
        else output_for_url(
            place_url,
            default_output_dir(),
        )
    )

    partial = output.with_suffix(".partial.json")
    status = output.with_suffix(".status.json")

    state = {
        "status": "running",
        "url": place_url,
        "count": 0,
        "requested_limit": max_reviews,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output": str(output),
    }

    driver = browser
    owns_driver = browser is None
    recovered = {}

    def checkpoint(rows):
        if rows:
            # Refresh may stop before reaching historical cards. Keep those
            # rows in every checkpoint, including when only a partial file exists.
            merged = dict(recovered)
            merged.update((row['ID Review'], row) for row in rows)
            rows = list(merged.values())
            write_json(partial, rows)

            state.update(
                count=len(rows),
                checkpoint=str(partial),
            )

            write_json(status, state)

    write_json(status, state)

    try:
        if driver is None:
            driver = make_driver(
                headless,
                chrome_major,
                profile_dir,
            )

        place = load_place(
            driver, place_url, city, stop_event=stop_event,
        )

        place["search_area"] = search_area
        state["place"] = place

        write_json(
            output.with_suffix(".place.json"),
            {
                "Nguồn": "Google Maps",
                "ID Quán": place["id"],
                "Tên Quán": place.get("name"),
                "URL Quán": place["url"],
                "Địa Chỉ": place.get("address"),
                "Thành Phố": place.get("city"),
                "Nguồn Thành Phố": place.get("city_source"),
                "Vĩ Độ": place.get("latitude"),
                "Kinh Độ": place.get("longitude"),
                "Điểm Trung Bình Quán": place.get("rating"),
                "Tổng Số Đánh Giá Quán": place.get("review_count"),
                "Thang Điểm": 5,
                "Khu Vực Tìm Kiếm": search_area,
                "Thời Điểm Thu Thập": place.get("collected_at"),
            },
        )

        for saved_path in (output, partial):
            if not saved_path.exists():
                continue
            old = json.loads(saved_path.read_text(encoding="utf-8"))
            if not isinstance(old, list) or any(
                not isinstance(row, dict) or not row.get('ID Review')
                or row.get('ID Quán') != place['id'] for row in old
            ):
                raise CrawlError(f'Dữ liệu cũ không hợp lệ; giữ nguyên để kiểm tra: {saved_path}')
            recovered.update((row['ID Review'], row) for row in old)
        for row in recovered.values():
            if not row.get('Thành Phố'):
                resolved = city_from_address(row.get('Địa Chỉ'))
                city_source = 'địa chỉ Google Maps'
                if not resolved:
                    resolved = place.get('city')
                    city_source = place.get('city_source')
                if resolved:
                    row['Thành Phố'] = resolved
                    row['Nguồn Thành Phố'] = city_source
        initial = list(recovered.values()) if resume else []
        if resume:
            LOG.info("Khôi phục dữ liệu đã lưu: %s review.", len(initial))

        if place.get('review_count') == 0 and not initial:
            state.update(status='no_reviews', stop_reason='displayed_zero_reviews',
                         finished_at=datetime.now(timezone.utc).isoformat())
            write_json(status, state)
            return []

        if initial:
            checkpoint(initial)

        rows, reason = collect_reviews(
            driver,
            place,
            max_reviews,
            checkpoint,
            initial,
            headless=headless,
            login_wait=login_wait,
            idle_seconds=idle_seconds,
            max_place_seconds=max_place_seconds,
            stop_event=stop_event,
        )

        # A lower requested limit must not truncate previously saved raw reviews.
        if recovered:
            merged = dict(recovered)
            merged.update((row['ID Review'], row) for row in rows)
            rows = list(merged.values())

        if not rows:
            raise CrawlError("Không lấy được review nào.")

        if reason not in (
            "max_reviews",
            "displayed_review_count",
        ):
            checkpoint(rows)

            state.update(
                status="partial",
                count=len(rows),
                stop_reason=reason,
                finished_at=datetime.now(
                    timezone.utc
                ).isoformat(),
                diagnostics=diagnostics(driver, output),
            )

            write_json(status, state)

            LOG.warning(
                "Lưu một phần: %s review (%s) -> %s; chuyển quán tiếp theo.",
                len(rows),
                reason,
                partial,
            )

            return rows

        write_json(output, rows)
        partial.unlink(missing_ok=True)
        state.pop("checkpoint", None)

        state.update(
            status="completed",
            count=len(rows),
            stop_reason=reason,
            finished_at=datetime.now(
                timezone.utc
            ).isoformat(),
        )

        write_json(status, state)

        LOG.info(
            "Đã lưu %s review -> %s",
            len(rows),
            output,
        )

        return rows

    except (
        Exception,
        KeyboardInterrupt,
    ) as exc:
        if driver is not None and connection_broken(exc):
            driver._transport_broken = True
        diagnostic_files = {}
        if not isinstance(exc, (KeyboardInterrupt, CrawlCancelled)):
            try:
                diagnostic_files = diagnostics(driver, output)
            except Exception as diagnostic_error:
                LOG.warning('Không lưu được chẩn đoán; giữ lỗi gốc: %s', diagnostic_error)
        state.update(
            status=(
                "interrupted"
                if isinstance(
                    exc,
                    (KeyboardInterrupt, CrawlCancelled),
                )
                else "failed"
            ),
            error=str(exc) or type(exc).__name__,
            error_type=type(exc).__name__,
            finished_at=datetime.now(
                timezone.utc
            ).isoformat(),
            diagnostics=diagnostic_files,
        )

        write_json(status, state)
        raise

    finally:
        if driver is not None and owns_driver:
            try:
                driver.quit()
            except (
                OSError,
                WebDriverException,
            ):
                pass


def run_gmap(
    place_urls=None,
    max_reviews=100,
    *,
    city=None,
    output_dir=None,
    headless=False,
    chrome_major=None,
    profile_dir=None,
    login_wait=120,
    idle_seconds=20,
    max_place_seconds=300,
):
    urls = list(
        dict.fromkeys(
            [DEFAULT_URL]
            if place_urls is None
            else place_urls
        )
    )

    if not urls:
        raise ValueError("Danh sách URL rỗng.")

    directory = (
        Path(output_dir)
        if output_dir
        else default_output_dir()
    )

    if len(urls) == 1:
        LOG.info(
            "Danh sách chỉ có 1 quán. "
            "Dùng gmap_pipeline.py để tìm quán theo khu vực."
        )

    total, failures, results = 0, [], []

    driver = make_driver(
        headless,
        chrome_major,
        profile_dir,
    )

    try:
        for index, url in enumerate(urls, 1):
            LOG.info(
                "[GMAP %s/%s] %s",
                index,
                len(urls),
                url,
            )

            output = output_for_url(
                url,
                directory,
            )

            try:
                if getattr(driver, '_transport_broken', False):
                    close_driver(driver)
                    driver = make_driver(headless, chrome_major, profile_dir)
                rows = scrape_gmap_reviews(
                    url,
                    max_reviews,
                    city=city,
                    output_path=output,
                    headless=headless,
                    browser=driver,
                    login_wait=login_wait,
                    idle_seconds=idle_seconds,
                    max_place_seconds=max_place_seconds,
                )

                total += len(rows)

                state = json.loads(
                    output.with_suffix(
                        ".status.json"
                    ).read_text(
                        encoding="utf-8"
                    )
                )

                results.append(
                    {
                        "url": url,
                        "status": state["status"],
                        "count": len(rows),
                        "stop_reason": state.get(
                            "stop_reason"
                        ),
                    }
                )

            except Exception as exc:
                failures.append(url)

                results.append(
                    {
                        "url": url,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

                LOG.error(
                    "Quán lỗi; chuyển quán tiếp theo: %s",
                    exc,
                )

            write_json(
                directory / "batch_status.json",
                {
                    "places": results,
                    "count": total,
                },
            )

    finally:
        driver.quit()

    if failures:
        raise CrawlError(
            f"Đã duyệt {len(urls)} quán; "
            f"{len(failures)} quán lỗi. "
            f"Đã giữ {total} review; "
            "xem batch_status.json."
        )

    return total


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--url",
        action="append",
    )

    parser.add_argument(
        "--max-reviews",
        type=int,
        default=100,
    )

    parser.add_argument("--city")

    parser.add_argument(
        "--output-dir",
        type=Path,
    )

    parser.add_argument(
        "--headless",
        action="store_true",
    )

    parser.add_argument(
        "--profile-dir",
        type=Path,
    )

    parser.add_argument(
        "--chrome-major",
        type=int,
    )

    parser.add_argument(
        "--login-wait",
        type=int,
        default=120,
    )

    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=20,
        help="Dừng quán khi không lưu thêm review mới trong số giây này.",
    )

    parser.add_argument(
        "--max-place-seconds",
        type=int,
        default=300,
        help="Giới hạn thời gian vòng thu thập review mỗi quán.",
    )

    args = parser.parse_args()

    if min(
        args.idle_seconds,
        args.max_place_seconds,
    ) < 1:
        parser.error(
            "Giới hạn thời gian phải lớn hơn 0."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        run_gmap(
            args.url,
            args.max_reviews,
            city=args.city,
            output_dir=args.output_dir,
            headless=args.headless,
            chrome_major=args.chrome_major,
            profile_dir=args.profile_dir,
            login_wait=args.login_wait,
            idle_seconds=args.idle_seconds,
            max_place_seconds=args.max_place_seconds,
        )

        return 0

    except KeyboardInterrupt:
        return 130

    except Exception as exc:
        LOG.error(
            "%s: %s",
            type(exc).__name__,
            exc,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
