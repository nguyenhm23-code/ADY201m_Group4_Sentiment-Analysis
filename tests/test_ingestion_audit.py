"""Offline regressions for persistence, area isolation, and bounded Maps batches."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion import GGMap as gmap
from src.ingestion import crawler, gmap_pipeline as pipeline
from src.ingestion.ingestion_runtime import write_json


def foody_row(text, rating='8'):
    return {'URL Quán': 'https://www.foody.vn/da-nang/test', 'Tên User': 'test',
            'Ngày Giờ': '01/09/2026 10:00', 'Bình Luận': text, 'Điểm Đánh Giá': rating}


class IngestionAuditTests(unittest.TestCase):
    def test_foody_rejects_cookie_dependent_homepage(self):
        with self.assertRaises(ValueError):
            crawler.category_city('https://www.foody.vn/')
        self.assertIn('https://www.foody.vn/ho-chi-minh', crawler.DEFAULT_CATEGORIES)

    def test_foody_discovery_keeps_requested_city(self):
        driver = MagicMock()
        links = ['https://www.foody.vn/da-nang/other', 'https://www.foody.vn/ho-chi-minh/right']
        driver.find_elements.return_value = [MagicMock(get_attribute=MagicMock(return_value=url)) for url in links]
        with patch.object(crawler, 'pause'):
            urls = crawler.get_restaurant_urls('https://www.foody.vn/ho-chi-minh', 1, driver=driver)
        self.assertEqual(urls, links[1:])

    def test_foody_rerun_preserves_old_reviews_and_updates_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'foody_da-nang_dataset.json'
            write_json(output, [foody_row('older'), foody_row('same', '7')])
            with patch.object(crawler, 'make_chrome'), patch.object(crawler, 'pause'), \
                 patch.object(crawler, 'get_restaurant_urls', return_value=['https://www.foody.vn/da-nang/test']), \
                 patch.object(crawler, 'scrape_foody_to_json', return_value=[foody_row('same', '9'), foody_row('new')]):
                result = crawler.run_foody(['https://www.foody.vn/da-nang'], 1, output_dir=folder)
            rows = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1]['Điểm Đánh Giá'], '9')
            self.assertEqual(result['new_reviews'], 1)

    def test_foody_corrupt_history_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'foody_da-nang_dataset.json'
            output.write_text('{bad', encoding='utf-8')
            with patch.object(crawler, 'make_chrome'), patch.object(crawler, 'get_restaurant_urls') as discover:
                result = crawler.run_foody(['https://www.foody.vn/da-nang'], 1, output_dir=folder)
            self.assertEqual(output.read_text(encoding='utf-8'), '{bad')
            self.assertEqual(len(result['errors']), 1)
            discover.assert_not_called()

    def test_config_rejects_string_arrays(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.json'
            write_json(config, {'target_restaurants': 2, 'max_reviews_per_restaurant': 2,
                                'keywords': 'quán ăn', 'areas': [{'name': 'Hồ Chí Minh'}]})
            with self.assertRaises(ValueError):
                pipeline.load_config(config)

    def test_region_quota_remains_interleaved(self):
        config = {'target_restaurants': 4, 'areas': [{'name': 'Bình Định'}, {'name': 'Hồ Chí Minh'}],
                  'keywords': ['quán ăn', 'cà phê']}
        queries = list(pipeline.build_queries(config))
        self.assertEqual([area for area, _ in queries], ['Bình Định', 'Hồ Chí Minh'] * 2)
        self.assertEqual(pipeline.area_targets(config), {'Bình Định': 2, 'Hồ Chí Minh': 2})

    def test_corrupt_resume_status_does_not_abort_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'gmap_test.json'
            output.with_suffix('.status.json').write_text('{bad', encoding='utf-8')
            self.assertFalse(pipeline._resume_done({'status': 'completed'}, output, 10))

    def test_maps_partial_moves_to_next_place_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            config = {'target_restaurants': 2, 'max_reviews_per_restaurant': 100,
                      'areas': [{'name': 'A'}, {'name': 'B'}]}
            places = {name: {'id': name, 'url': f'https://www.google.com/maps/place/{name}/',
                            'name': name, 'areas': [name], 'status': 'pending'} for name in ('A', 'B')}
            write_json(directory / 'places_queue.json', {'version': 1, 'queries': {}, 'places': places})
            def scrape(url, limit, **kwargs):
                write_json(kwargs['output_path'].with_suffix('.status.json'),
                           {'status': 'partial', 'stop_reason': 'stalled', 'checkpoint': 'partial.json'})
                return [{'ID Review': 'review'}]
            with patch.object(pipeline, 'browser_alive', return_value=True), \
                 patch.object(gmap, 'scrape_gmap_reviews', side_effect=scrape) as scrape_mock:
                code = pipeline._run_pipeline(config, directory, lambda **kwargs: MagicMock(), crawl_only=True)
            state = json.loads((directory / 'places_queue.json').read_text(encoding='utf-8'))
            self.assertEqual(scrape_mock.call_count, 2)
            self.assertEqual(code, 1)
            self.assertEqual(state['last_run']['incomplete'], 2)
            self.assertEqual(state['last_run']['status'], 'partial')

    def test_maps_discovery_shortfall_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            write_json(directory / 'places_queue.json', {'version': 1, 'queries': {}, 'places': {
                'A': {'id': 'A', 'url': 'https://www.google.com/maps/place/A/', 'name': 'A',
                      'areas': ['A'], 'status': 'pending'}}})
            config = {'target_restaurants': 2, 'max_reviews_per_restaurant': 100,
                      'areas': [{'name': 'A'}, {'name': 'B'}]}
            code = pipeline._run_pipeline(config, directory, lambda **kwargs: MagicMock(),
                                          crawl_only=True, discover_only=True)
            state = json.loads((directory / 'places_queue.json').read_text(encoding='utf-8'))
            self.assertEqual(code, 1)
            self.assertEqual(state['last_run']['shortfall'], 1)

    def test_maps_restores_completed_and_partial_without_truncating_history(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'gmap_test.json'
            rows = [{'ID Quán': 'place', 'ID Review': str(index)} for index in range(3)]
            write_json(output, rows[:2])
            write_json(output.with_suffix('.partial.json'), rows[1:])
            place = {'id': 'place', 'url': 'https://www.google.com/maps/place/Test/', 'review_count': 3}
            def collect(*args, **kwargs):
                self.assertEqual(len(args[4]), 3)
                return args[4][:1], 'max_reviews'
            with patch.object(gmap, 'load_place', return_value=place), \
                 patch.object(gmap, 'collect_reviews', side_effect=collect):
                result = gmap.scrape_gmap_reviews(place['url'], 1, browser=MagicMock(), output_path=output)
            self.assertEqual(len(result), 3)
            self.assertEqual(len(json.loads(output.read_text(encoding='utf-8'))), 3)

    def test_maps_zero_reviews_is_not_a_selector_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'gmap_test.json'
            place = {'id': 'place', 'url': 'https://www.google.com/maps/place/Test/', 'review_count': 0}
            with patch.object(gmap, 'load_place', return_value=place), \
                 patch.object(gmap, 'collect_reviews') as collect:
                result = gmap.scrape_gmap_reviews(place['url'], browser=MagicMock(), output_path=output)
            collect.assert_not_called()
            self.assertEqual(result, [])
            self.assertEqual(json.loads(output.with_suffix('.status.json').read_text(encoding='utf-8'))['status'], 'no_reviews')

    def test_maps_refresh_fetches_again_and_preserves_historical_reviews(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'gmap_test.json'
            write_json(output, [{'ID Quán': 'place', 'ID Review': 'old'}])
            place = {'id': 'place', 'url': 'https://www.google.com/maps/place/Test/', 'review_count': 2}
            def collect(*args, **kwargs):
                self.assertEqual(args[4], [])
                return [{'ID Quán': 'place', 'ID Review': 'new'}], 'max_reviews'
            with patch.object(gmap, 'load_place', return_value=place), \
                 patch.object(gmap, 'collect_reviews', side_effect=collect):
                result = gmap.scrape_gmap_reviews(place['url'], 1, browser=MagicMock(),
                                                   output_path=output, resume=False)
            self.assertEqual({row['ID Review'] for row in result}, {'old', 'new'})

    def test_maps_invalid_history_is_preserved_and_marked_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'gmap_test.json'
            output.write_text('[123]', encoding='utf-8')
            place = {'id': 'place', 'url': 'https://www.google.com/maps/place/Test/', 'review_count': 2}
            with patch.object(gmap, 'load_place', return_value=place), \
                 patch.object(gmap, 'diagnostics', return_value={}), \
                 patch.object(gmap, 'collect_reviews') as collect:
                with self.assertRaises(gmap.CrawlError):
                    gmap.scrape_gmap_reviews(place['url'], browser=MagicMock(), output_path=output)
            collect.assert_not_called()
            self.assertEqual(output.read_text(encoding='utf-8'), '[123]')
            self.assertEqual(json.loads(output.with_suffix('.status.json').read_text(encoding='utf-8'))['status'], 'failed')

    def test_maps_late_painted_cards_are_accepted_after_timeout(self):
        waiter = MagicMock()
        waiter.until.side_effect = [True, gmap.TimeoutException()]
        with patch.object(gmap, 'wait_review_access'), patch.object(gmap, 'review_tab', return_value=None), \
             patch.object(gmap, 'more_reviews_button', return_value=None), \
             patch.object(gmap, 'WebDriverWait', return_value=waiter), \
             patch.object(gmap, 'cards', return_value=[MagicMock()]):
            gmap.open_reviews(MagicMock())
        self.assertEqual(waiter.until.call_count, 2)

    def test_maps_delayed_panel_retries_once(self):
        waiter = MagicMock()
        waiter.until.side_effect = [True, gmap.TimeoutException(), True]
        with patch.object(gmap, 'wait_review_access'), patch.object(gmap, 'review_tab', return_value=None), \
             patch.object(gmap, 'more_reviews_button', return_value=None), \
             patch.object(gmap, 'WebDriverWait', return_value=waiter), \
             patch.object(gmap, 'cards', return_value=[]):
            gmap.open_reviews(MagicMock())
        self.assertEqual(waiter.until.call_count, 3)

    def test_maps_stable_bottom_exits_after_three_polls(self):
        card = MagicMock()
        card.get_attribute.return_value = 'id'
        waiter = MagicMock()
        waiter.until.side_effect = gmap.TimeoutException()
        place = {'id': 'place', 'url': 'https://www.google.com/maps/place/Test/'}
        with patch.object(gmap, 'open_reviews'), patch.object(gmap, 'cards', return_value=[card]), \
             patch.object(gmap, 'read_card', return_value={'review_id': 'id', 'rating': 5, 'text': 'Ngon'}), \
             patch.object(gmap, 'scroll_reviews'), patch.object(gmap, 'WebDriverWait', return_value=waiter), \
             patch.object(gmap, 'review_scroll_state', return_value={'bottom': True, 'loading': False, 'height': 500}):
            rows, reason = gmap.collect_reviews(MagicMock(), place, 100, MagicMock())
        self.assertEqual(len(rows), 1)
        self.assertEqual(reason, 'end_of_visible_reviews')
        self.assertEqual(waiter.until.call_count, 3)


if __name__ == '__main__':
    unittest.main()
