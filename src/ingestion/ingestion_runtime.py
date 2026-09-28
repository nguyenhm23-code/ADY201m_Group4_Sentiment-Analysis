"""Shared lifecycle helpers; each source owns its browser and driver binary."""
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)
_START_LOCK = threading.Lock()
_DRIVER_ROOT = Path(tempfile.gettempdir()) / 'sentiment-crawler-drivers'


class CrawlCancelled(Exception):
    pass


def check_cancelled(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise CrawlCancelled('Đã yêu cầu dừng crawl.')


def pause(seconds, stop_event=None):
    if stop_event is None:
        time.sleep(seconds)
    elif stop_event.wait(seconds):
        raise CrawlCancelled('Đã yêu cầu dừng crawl.')


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def close_driver(driver):
    if driver is not None:
        process = getattr(getattr(driver, 'service', None), 'process', None)
        try:
            driver.quit()
        except Exception:
            LOG.warning('Không đóng được trình duyệt.', exc_info=True)
        finally:
            if process is not None:
                try:
                    # UC sends kill() without waiting; Windows can still hold
                    # the executable lock when the next browser is prepared.
                    process.wait(timeout=5)
                except Exception:
                    LOG.warning('ChromeDriver chưa xác nhận kết thúc.', exc_info=True)


def make_chrome(source, *, options=None, headless=False, chrome_major=None,
                profile_dir=None, driver_class=None):
    import undetected_chromedriver as uc

    class SafeChrome(uc.Chrome):
        def quit(self):
            if getattr(self, '_ingestion_closed', False):
                return
            self._ingestion_closed = True
            try:
                super().quit()
            except OSError as exc:
                if getattr(exc, 'winerror', None) != 6:
                    raise
        def __del__(self):
            try:
                self.quit()
            except Exception:
                pass

    if source not in ('foody', 'gmap'):
        raise ValueError('Nguồn trình duyệt không hợp lệ.')
    # A separate Patcher subclass avoids mutating UC's process-wide data_path.
    patcher_type = type('SourcePatcher', (uc.Patcher,),
                        {'data_path': str(_DRIVER_ROOT / source)})
    with _START_LOCK:
        patcher = patcher_type(version_main=chrome_major)
        patcher.auto()
        driver = (driver_class or SafeChrome)(
            options=options or uc.ChromeOptions(), headless=headless,
            version_main=patcher.version_main,
            driver_executable_path=patcher.executable_path,
            user_data_dir=str(Path(profile_dir).resolve()) if profile_dir else None)
        driver._ingestion_patcher = patcher
    try:
        driver.set_page_load_timeout(60)
        return driver
    except Exception:
        close_driver(driver)
        raise
