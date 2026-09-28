import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from selenium.webdriver.remote.client_config import ClientConfig
from selenium.webdriver.remote.remote_connection import RemoteConnection
from urllib3.exceptions import HTTPError
from src.ingestion import GGMap as gmap, browser_health as health, gmap_pipeline


class BrowserHealthTests(unittest.TestCase):
    def test_real_transport_timeout_is_not_retried(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                time.sleep(.4)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        executor = RemoteConnection(client_config=ClientConfig(
            remote_server_addr=f'http://127.0.0.1:{server.server_port}', keep_alive=True))
        self.addCleanup(executor.close)
        driver = SimpleNamespace(command_executor=executor)
        health.configure_transport(driver, command_timeout=.08)
        start = time.monotonic()
        with self.assertRaises(HTTPError):
            executor.execute('getPageSource', {'sessionId': 'test'})
        self.assertLess(time.monotonic() - start, .35)
        self.assertEqual(len(calls), 1)
        self.assertTrue(driver._transport_broken)
        with self.assertRaises(ConnectionError):
            executor.execute('getPageSource', {'sessionId': 'test'})
        self.assertFalse(gmap_pipeline.browser_alive(driver))

    def test_diagnostics_stops_after_connection_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            driver = MagicMock()
            driver._transport_broken = False
            driver.current_url = 'https://www.google.com/maps/'
            driver._gmap_load_trace = None
            driver._gmap_review_trace = None
            with patch.object(gmap, 'review_panel_state', side_effect=ConnectionError('lost')):
                result = gmap.diagnostics(driver, Path(folder)/'gmap_a.json')
            driver.save_screenshot.assert_not_called()
            state = json.loads(Path(result['state']).read_text(encoding='utf-8'))
            self.assertIn('ConnectionError', state['capture_error'])
            self.assertTrue(driver._transport_broken)

    def test_partial_reviews_survive_diagnostic_capture_error(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'gmap_a.json'
            driver = MagicMock()
            driver._transport_broken = False
            driver._gmap_load_trace = None
            driver._gmap_review_trace = None
            driver.current_url = 'https://www.google.com/maps/'
            place = {'id': 'a', 'url': driver.current_url, 'review_count': 100}
            row = {'ID Review': 'one', 'Bình Luận': 'Ngon'}
            with patch.object(gmap, 'load_place', return_value=place), \
                 patch.object(gmap, 'collect_reviews', return_value=([row], 'stalled')), \
                 patch.object(gmap, 'review_panel_state', side_effect=ConnectionError('lost')):
                rows = gmap.scrape_gmap_reviews(place['url'], 100, browser=driver, output_path=output)
            self.assertEqual(len(rows), 1)
            state = json.loads(output.with_suffix('.status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['status'], 'partial')
            self.assertTrue(output.with_suffix('.partial.json').exists())


if __name__ == '__main__':
    unittest.main()
