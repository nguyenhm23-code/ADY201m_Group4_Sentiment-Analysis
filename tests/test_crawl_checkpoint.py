"""Offline restart/crash regressions; no network or Chrome required."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion import crawler, gmap_pipeline as pipeline, GGMap as gmap
from src.ingestion import main as entry
from src.ingestion.ingestion_runtime import CrawlCancelled, write_json


CITY = 'https://www.foody.vn/da-nang'
URLS = [CITY + '/' + name for name in ('a', 'b', 'c')]


def row(url, text='review'):
    return {'URL Quán': url, 'Tên User': 'user', 'Ngày Giờ': 'today', 'Bình Luận': text}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


class FoodyCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.queue = self.directory / 'places_queue.json'
        self.output = self.directory / 'foody_da-nang_dataset.json'
        self.chrome = self.enterContext(patch.object(crawler, 'make_chrome'))
        self.enterContext(patch.object(crawler, 'pause'))
        self.discover = self.enterContext(patch.object(crawler, 'get_restaurant_urls', return_value=URLS[:2]))

    def run_crawl(self, target=2, **kwargs):
        return crawler.run_foody([CITY], target, output_dir=self.directory, **kwargs)

    def test_second_run_skips_processed_places_without_opening_browser(self):
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=lambda url, *a, **k: [row(url)]) as scrape:
            self.run_crawl()
            scrape.reset_mock()
            self.chrome.reset_mock()
            self.discover.reset_mock()
            result = self.run_crawl()
        scrape.assert_not_called()
        self.chrome.assert_not_called()
        self.discover.assert_not_called()
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['incomplete'], 0)
        self.assertEqual(len(read(self.output)), 2)

    def test_interrupt_restores_current_place_and_preserves_saved_reviews(self):
        def interrupted(url, *args, checkpoint, **kwargs):
            checkpoint([row(url)])
            if url == URLS[1]:
                raise CrawlCancelled('stop')
            return [row(url)]
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=interrupted):
            with self.assertRaises(CrawlCancelled):
                self.run_crawl()
        self.assertEqual(read(self.queue)['places'][URLS[1]]['status'], 'interrupted')
        self.assertEqual(len(read(self.output)), 2)
        with patch.object(crawler, 'scrape_foody_to_json', return_value=[row(URLS[1], 'new')]) as scrape:
            result = self.run_crawl()
        self.assertEqual([call.args[0] for call in scrape.call_args_list], [URLS[1]])
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(len(read(self.output)), 3)

    def test_discovery_checkpoint_survives_interruption(self):
        def discover(*args, checkpoint, **kwargs):
            checkpoint(URLS[:1])
            raise CrawlCancelled('stop during discovery')
        self.discover.side_effect = discover
        with self.assertRaises(CrawlCancelled):
            self.run_crawl()
        self.assertEqual(read(self.queue)['categories']['da-nang']['urls'], URLS[:1])
        def resumed_discovery(*args, initial, **kwargs):
            self.assertEqual(initial, URLS[:1])
            return URLS[:2]
        self.discover.side_effect = resumed_discovery
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=lambda url, *a, **k: [row(url)]):
            self.run_crawl()
        self.assertEqual(len(read(self.queue)['places']), 2)

    def test_limit_and_empty_are_retried_not_marked_processed(self):
        def scrape(url, *args, checkpoint, **kwargs):
            rows = [row(url)] if url == URLS[0] else []
            checkpoint(rows, 'scroll_limit' if rows else 'empty_unconfirmed')
            return rows
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=scrape) as mock:
            result = self.run_crawl()
            self.assertEqual(result['incomplete'], 2)
            mock.reset_mock()
            self.run_crawl()
            self.assertEqual(mock.call_count, 2)

    def test_refresh_merges_history_and_increased_target_keeps_old_places(self):
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=lambda url, *a, **k: [row(url)]):
            self.run_crawl()
        self.discover.return_value = URLS
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=lambda url, *a, **k: [row(url, 'new')]) as scrape:
            result = self.run_crawl(target=3)
            self.assertEqual([call.args[0] for call in scrape.call_args_list], URLS[2:])
            self.assertEqual(result['skipped'], 2)
            scrape.reset_mock()
            self.run_crawl(target=3, refresh=True)
            self.assertEqual(scrape.call_count, 3)
        self.assertEqual(len(read(self.output)), 5)

    def test_missing_data_is_crawled_again_despite_processed_status(self):
        with patch.object(crawler, 'scrape_foody_to_json', side_effect=lambda url, *a, **k: [row(url)]) as scrape:
            self.run_crawl()
            self.output.unlink()
            scrape.reset_mock()
            self.run_crawl()
            self.assertEqual(scrape.call_count, 2)

    def test_short_discovery_reuses_queue_but_empty_discovery_retries(self):
        self.discover.return_value = []
        with self.assertLogs(crawler.LOG, level='ERROR'):
            self.assertTrue(self.run_crawl()['errors'])
        self.discover.return_value = URLS[:1]
        with patch.object(crawler, 'scrape_foody_to_json', return_value=[row(URLS[0])]):
            self.run_crawl()
            self.discover.reset_mock()
            self.assertEqual(self.run_crawl()['skipped'], 1)
            self.discover.assert_not_called()

    def test_corrupt_queue_is_not_overwritten(self):
        self.queue.write_text('{bad', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_crawl()
        self.assertEqual(self.queue.read_text(), '{bad')
        self.chrome.assert_not_called()

    def test_scroll_checkpoints_loaded_cards_before_cancellation(self):
        driver = MagicMock()
        def scroll(*args, checkpoint, **kwargs):
            checkpoint()
            raise CrawlCancelled('stop')
        saved = MagicMock()
        with patch.object(crawler, 'wait_foody_reviews'), \
             patch.object(crawler, 'scroll_foody_reviews', side_effect=scroll), \
             patch.object(crawler, 'read_foody_reviews', return_value=[row(URLS[0])]):
            with self.assertRaises(CrawlCancelled):
                crawler.scrape_foody_to_json(URLS[0], driver, checkpoint=saved)
        self.assertGreaterEqual(saved.call_count, 1)
        self.assertEqual(saved.call_args.args[0], [row(URLS[0])])


class MapsCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.queue = self.directory / 'places_queue.json'
        self.config = {'target_restaurants': 3, 'max_reviews_per_restaurant': 10,
                       'areas': [{'name': 'area'}]}
        self.places = {name: {'id': name, 'name': name, 'url': 'https://www.google.com/maps/place/' + name + '/',
                             'areas': ['area'], 'status': 'pending'} for name in ('a', 'b', 'c')}
        self.enterContext(patch.object(pipeline, 'browser_alive', return_value=True))

    def save_queue(self, **extra):
        write_json(self.queue, {'version': 1, 'places': self.places, 'queries': {}, **extra})

    def partial(self, url, limit, **kwargs):
        write_json(kwargs['output_path'].with_suffix('.status.json'), {'status': 'partial', 'checkpoint': 'saved'})
        return [{'ID Review': '1'}]

    def run_crawl(self, **kwargs):
        return pipeline._run_pipeline(self.config, self.directory, MagicMock(), crawl_only=True, **kwargs)

    def test_legacy_queue_prioritizes_interrupted_and_pending_before_retries(self):
        self.places['a']['status'] = 'partial'
        self.places['c']['status'] = 'running'
        self.save_queue()
        with patch.object(gmap, 'scrape_gmap_reviews', side_effect=self.partial) as scrape:
            self.run_crawl()
        self.assertEqual([call.args[0] for call in scrape.call_args_list],
                         [self.places[name]['url'] for name in ('c', 'b', 'a')])

    def test_retry_cursor_survives_restart(self):
        for item in self.places.values():
            item['status'] = 'partial'
        self.save_queue(last_processed_id='a')
        with patch.object(gmap, 'scrape_gmap_reviews', side_effect=[RuntimeError('offline')] * 3) as scrape:
            self.run_crawl()
        self.assertEqual([call.args[0] for call in scrape.call_args_list],
                         [self.places[name]['url'] for name in ('b', 'c', 'a')])
        self.assertEqual(read(self.queue)['last_processed_id'], 'a')

    def test_completed_and_no_reviews_skip_even_with_stale_queue(self):
        self.config['target_restaurants'] = 2
        self.save_queue()
        output = gmap.output_for_url(self.places['a']['url'], self.directory)
        write_json(output, [{'ID Review': '1'}])
        write_json(output.with_suffix('.status.json'), {'status': 'completed', 'place': {'review_count': 1}})
        output = gmap.output_for_url(self.places['b']['url'], self.directory)
        write_json(output.with_suffix('.status.json'), {'status': 'no_reviews'})
        with patch.object(gmap, 'scrape_gmap_reviews') as scrape:
            self.assertEqual(self.run_crawl(), 0)
            scrape.assert_not_called()
        self.assertEqual(read(self.queue)['last_run']['skipped'], 2)
        with patch.object(gmap, 'scrape_gmap_reviews', side_effect=self.partial) as scrape:
            self.run_crawl(refresh=True)
        self.assertEqual(scrape.call_count, 2)
        self.assertFalse(scrape.call_args.kwargs['resume'])

    def test_refresh_interrupt_keeps_old_partial_reviews(self):
        output = self.directory / 'gmap_test.json'
        old = {'ID Quán': 'place', 'ID Review': 'old'}
        new = {'ID Quán': 'place', 'ID Review': 'new'}
        write_json(output.with_suffix('.partial.json'), [old])
        def collect(driver, place, limit, checkpoint, initial, **kwargs):
            self.assertEqual(initial, [])
            checkpoint([new])
            raise CrawlCancelled('stop')
        with patch.object(gmap, 'load_place', return_value={'id': 'place', 'url': 'url'}), \
             patch.object(gmap, 'collect_reviews', side_effect=collect):
            with self.assertRaises(CrawlCancelled):
                gmap.scrape_gmap_reviews('url', output_path=output, browser=MagicMock(), resume=False)
        self.assertEqual(read(output.with_suffix('.partial.json')), [old, new])


class CheckpointCliTests(unittest.TestCase):
    def test_skipped_foody_is_success_and_refresh_reaches_both_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(crawler, 'run_foody', return_value={
                    'errors': [], 'reviews': 0, 'skipped': 2, 'incomplete': 0}) as foody, \
                 patch.object(entry.logging, 'basicConfig'), \
                 patch.object(pipeline, 'run_pipeline', return_value=0) as maps:
                self.assertEqual(entry.main(['--output-dir', folder, '--refresh']), 0)
            self.assertTrue(foody.call_args.kwargs['refresh'])
            self.assertTrue(maps.call_args.kwargs['refresh'])


if __name__ == '__main__':
    unittest.main()
