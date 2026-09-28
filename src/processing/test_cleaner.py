"""Offline regression tests: no browser, network or third-party dependencies."""
import importlib.util
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cleaner_under_test', HERE / 'cleaner.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def review(place='a', key='1', text='Đồ ăn ngon, nhân viên thân thiện!', rating=5):
    return {'ID Quán': place, 'ID Review': key, 'Bình Luận': text,
            'Điểm Đánh Giá': rating, 'Ngày Giờ': '4 tháng trước',
            'URL Quán': 'https://www.google.com/maps/place/' + place}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / 'raw'
        self.out = self.root / 'processed'
        (self.raw / 'gmap').mkdir(parents=True)
        (self.raw / 'foody').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.raw / relative
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def run_data(self, data=None, **kwargs):
        if data is not None:
            self.write('gmap/gmap_test.json', data)
        result = c.run_pipeline(self.raw, self.out, **kwargs)
        folder = Path(result['output'])
        rows = [json.loads(line) for line in (folder / 'reviews.jsonl').read_text(encoding='utf-8').splitlines()]
        return result, folder, rows

    def test_preserves_sentiment_and_masks_contacts(self):
        text = c.clean_text('<p>KHÔNG ngon 😡 &amp; quá tệ!</p> 0901 234 567 a.b@example.com https://example.com')
        self.assertIn('KHÔNG ngon 😡 & quá tệ!', text)
        for value in ('<PHONE>', '<EMAIL>', '<URL>'):
            self.assertIn(value, text)
        for value in ('0901', 'example.com', '<p>'):
            self.assertNotIn(value, text)
        self.assertEqual('', c.clean_text('Không bình luận'))

    def test_dates_are_honest(self):
        self.assertEqual((None, 'relative'), c.parse_date('4 tháng trước')[:2])
        self.assertEqual((None, 'edited_or_relative'), c.parse_date('Thời gian chỉnh sửa: 2 năm trước', '2024-01-01')[:2])
        self.assertEqual('2023-10-25T20:37:00+07:00', c.parse_date('25/10/2023 20:37')[0])
        self.assertEqual('day', c.parse_date('3/7/2022')[1])
        self.assertEqual('second', c.parse_date('3/7/2022 10:11:12')[1])
        self.assertIsNone(c.parse_date('31/2/2024')[0])

    def test_native_scales_and_city(self):
        f = review(rating='6,5')
        f.update({'URL Quán': 'https://www.foody.vn/da-nang/quan-a', 'Thành Phố': 'da-nang'})
        row = c.normalize(f, 'foody', 'foody/foody_ho-chi-minh-city_dataset.json', c.read_config())
        self.assertEqual(('Đà Nẵng', 6.5, 10, 'neutral'), (row['city'], row['rating'], row['rating_scale'], row['weak_sentiment']))
        self.assertIn('filename_city_mismatch', row['quality_flags'])
        for invalid in (True, 'NaN', '5 stars', 6, 4.5):
            self.assertIsNone(c.normalize(review(rating=invalid), 'gmap', 'x', c.read_config())['rating'])
        self.assertIsNone(c.normalize(review(), 'gmap', 'x', c.read_config())['city'])

    def test_search_regions_normalize_string_and_list_without_guessing_city(self):
        for area in ('Bình Định', ['Bình Định', 'Bình Định']):
            row = c.normalize({**review(), 'Khu Vực Tìm Kiếm': area}, 'gmap', 'x', c.read_config())
            self.assertEqual(['Bình Định'], row['search_area'])
            self.assertIsNone(row['city'])

    def test_dedup_final_partial_and_ignored_sidecars(self):
        row = review()
        self.write('gmap/gmap_a.json', [row])
        self.write('gmap/gmap_a.partial.json', [row])
        self.write('gmap/gmap_a.status.json', {'reviews': 1})
        self.write('gmap/gmap_a.place.json', {})
        self.write('gmap/places_queue.json', [row])
        result, _, rows = self.run_data()
        self.assertEqual(1, result['normalized_rows'])
        self.assertEqual(1, result['duplicate_ids_removed'])
        self.assertEqual(2, len(rows[0]['provenance']))

    def test_revisions_prefer_actual_latest_instant(self):
        older = {**review(text='Old longer text about this restaurant'), 'Thời Điểm Thu Thập': '2026-01-01T10:00:00+07:00'}
        newer = {**review(text='New'), 'Thời Điểm Thu Thập': '2026-01-01T04:00:00+00:00'}
        self.write('gmap/gmap_a.json', [older])
        _, _, rows = self.run_data([newer])
        self.assertEqual('New', rows[0]['text_clean'])
        self.assertIn('review_revision_conflict', rows[0]['quality_flags'])

    def test_restaurant_and_duplicate_holdout(self):
        text = 'Món ăn rất ngon và nhân viên phục vụ chu đáo, không gian sạch sẽ, giá cả cũng hợp lý.'
        data = [review('a', '1', text), review('a', '2', text), review('a', '3', 'Không ngon'),
                review('b', '4', text), review('c', '5', text.replace('hợp lý', 'hợp lí')),
                review('d', '6', 'Ngon'), review('e', '7', 'Ngon')]
        result, _, rows = self.run_data(data)
        grouped = [r for r in rows if r['native_restaurant_id'] in ('a', 'b', 'c')]
        self.assertEqual(1, len({r['split_group_id'] for r in grouped}))
        self.assertEqual(1, len({r['split'] for r in grouped}))
        self.assertEqual(6, result['eligible_text_rows'])
        short = [r for r in rows if r['native_restaurant_id'] in ('d', 'e')]
        self.assertEqual(2, len({r['split_group_id'] for r in short}))

    def test_verified_cross_source_aliases(self):
        self.write('foody/foody_da-nang_dataset.json', [review('same-foody')])
        _, _, initial = self.run_data([review('same-maps')])
        config = self.root / 'config.json'
        config.write_text(json.dumps({'restaurant_aliases': {r['restaurant_id']: 'verified-restaurant' for r in initial}}))
        _, _, rows = self.run_data(config_path=config)
        self.assertEqual(1, len({r['split_group_id'] for r in rows}))

    def test_empty_text_remains_in_master_only(self):
        result, folder, _ = self.run_data([review(text='Không bình luận')])
        self.assertEqual(1, result['normalized_rows'])
        self.assertEqual(0, result['eligible_text_rows'])
        self.assertEqual('', (folder / 'annotation_queue.jsonl').read_text())

    def test_annotations_no_rating_leakage_and_sql_integrity(self):
        data = [review(str(i), str(i), f'Món ăn ở đây khá ngon {i}') for i in range(100)]
        _, folder, rows = self.run_data(data)
        annotations = self.root / 'annotations.jsonl'
        c.jsonl(annotations, [{'review_id': r['review_id'], 'text_sha256': r['text_sha256'],
                             'sentiment': 'positive', 'annotator': 'reviewer-1'} for r in rows])
        result, folder, _ = self.run_data(annotations=annotations)
        self.assertEqual(0, result['exports']['weak_train'])
        self.assertEqual(100, sum(result['exports'][s] for s in ('train', 'validation', 'test')))
        groups = {}
        for split in ('train', 'validation', 'test'):
            exported = [json.loads(line) for line in (folder / f'{split}.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(exported)
            groups[split] = {r['split_group_id'] for r in exported}
            for row in exported:
                self.assertNotIn('rating', row)
                self.assertEqual('human', row['label_source'])
        self.assertFalse(groups['train'] & groups['validation'] | groups['train'] & groups['test'] | groups['validation'] & groups['test'])
        with closing(sqlite3.connect(folder / 'reviews.sqlite')) as db:
            self.assertEqual('ok', db.execute('PRAGMA integrity_check').fetchone()[0])
            self.assertEqual(100, db.execute('SELECT count(*) FROM reviews').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT count(*) FROM monthly_rating_observations').fetchone()[0])

    def test_idempotency_rejections_and_failed_import_preserves_latest(self):
        result, folder, rows = self.run_data([review(), 'bad row'])
        self.assertEqual(1, result['rejections'])
        old = (self.out / 'latest.json').read_bytes()
        repeat, _, _ = self.run_data()
        self.assertEqual(result['run_id'], repeat['run_id'])
        ann = self.root / 'annotations.jsonl'
        c.jsonl(ann, [{'review_id': rows[0]['review_id'], 'text_sha256': 'stale', 'sentiment': 'positive', 'annotator': 'a'}])
        with self.assertRaisesRegex(ValueError, 'Text changed'):
            self.run_data(annotations=ann)
        self.assertEqual(old, (self.out / 'latest.json').read_bytes())
        self.write('gmap/gmap_test.json', {'bad': 'file'})
        with self.assertRaisesRegex(ValueError, 'No valid'):
            self.run_data()
        self.assertEqual(old, (self.out / 'latest.json').read_bytes())
        self.assertTrue(folder.exists())

    def test_weak_labels_never_export_holdout_and_mixed_is_not_neutral(self):
        result, folder, rows = self.run_data([review(str(i), str(i), f'Món này ngon {i}') for i in range(100)])
        weak = [json.loads(line) for line in (folder / 'weak_train.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(weak)
        self.assertEqual({'train'}, {r['split'] for r in weak})
        self.assertLess(len(weak), len(rows))
        selected = weak[0]['review_id']
        row = next(r for r in rows if r['review_id'] == selected)
        ann = self.root / 'annotations.jsonl'
        c.jsonl(ann, [{'review_id': selected, 'text_sha256': row['text_sha256'], 'sentiment': 'mixed', 'annotator': 'a'}])
        result2, _, _ = self.run_data(annotations=ann)
        self.assertEqual(result['exports']['weak_train'] - 1, result2['exports']['weak_train'])
        self.assertEqual(0, result2['exports']['train'])

    def test_database_failure_does_not_publish_incomplete_snapshot(self):
        self.run_data([review()])
        old = (self.out / 'latest.json').read_bytes()
        self.write('gmap/gmap_test.json', [review(), review('b', '2')])
        with patch.object(c, 'write_database', side_effect=sqlite3.OperationalError('disk failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk failure'):
                self.run_data()
        self.assertEqual(old, (self.out / 'latest.json').read_bytes())
        self.assertEqual(1, len(list((self.out / 'runs').iterdir())))

    def test_malformed_config_reports_validation_error(self):
        config = self.root / 'config.json'
        bad_configs = [{'seed': True}, {'seed': []}, {'weak_thresholds': None},
            {'weak_thresholds': {'foody': {}}},
            {'weak_thresholds': {'foody': {'negative_max': 'bad', 'positive_min': 7},
                                 'gmap': {'negative_max': 2, 'positive_min': 4}}},
            {'near_duplicate_similarity': '0.94'}, {'split_ratios': [True, 0, 0]},
            {'rating_scales': []}]
        for value in bad_configs:
            with self.subTest(config=value):
                config.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    c.read_config(config)
        for token in ('NaN', 'Infinity', '1e999'):
            with self.subTest(token=token):
                config.write_text('{"seed":' + token + '}', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Non-finite'):
                    c.read_config(config)

    def test_nested_fields_and_nonfinite_json_are_quarantined(self):
        data = [review(), review(key='bad-text', text={'html': 'looks like a review'}),
                {**review(key='bad-id'), 'ID Quán': ['bad-id']},
                {**review(key='bad-rating'), 'Điểm Đánh Giá': {'value': 5}}]
        self.write('gmap/gmap_bad.json', [review()])
        (self.raw / 'gmap' / 'gmap_bad.json').write_text('[{"rating": 1e999}]', encoding='utf-8')
        result, folder, _ = self.run_data(data)
        self.assertEqual(1, result['normalized_rows'])
        self.assertEqual(4, result['rejections'])
        rejected = (folder / 'rejected.jsonl').read_text(encoding='utf-8')
        self.assertIn('invalid_field_type', rejected)
        self.assertIn('Non-finite', rejected)

    def test_contact_only_review_does_not_train_on_redaction_tokens(self):
        result, _, rows = self.run_data([review(text='0901 234 567 abc@example.com https://example.com')])
        self.assertEqual('<PHONE> <EMAIL> <URL>', rows[0]['text_clean'])
        self.assertEqual(0, result['eligible_text_rows'])
        self.assertEqual(0, result['exports']['weak_train'])

    def test_calendar_month_is_consistent_in_vietnam_timezone(self):
        row = {**review(), 'Ngày Giờ': '2024-01-31T20:00:00+00:00'}
        _, folder, rows = self.run_data([row])
        self.assertEqual('2024-02-01T03:00:00+07:00', rows[0]['published_at'])
        with closing(sqlite3.connect(folder / 'reviews.sqlite')) as db:
            self.assertEqual('2024-02', db.execute('SELECT month FROM monthly_rating_observations').fetchone()[0])

    def test_snapshot_artifact_integrity_blocks_silent_reuse(self):
        _, folder, _ = self.run_data([review()])
        before = (self.out / 'latest.json').read_bytes()
        (folder / 'train.jsonl').write_text('modified outside pipeline', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Snapshot integrity check failed'):
            self.run_data()
        self.assertEqual(before, (self.out / 'latest.json').read_bytes())

    def test_annotation_hash_matches_imported_bytes_when_file_changes(self):
        _, _, rows = self.run_data([review()])
        annotation = self.root / 'annotations.jsonl'
        c.jsonl(annotation, [{'review_id': rows[0]['review_id'], 'text_sha256': rows[0]['text_sha256'],
                             'sentiment': 'positive', 'annotator': 'a'}])
        original = annotation.read_bytes()
        importer = c.import_annotations
        def changing_file(records, path, **kwargs):
            annotation.write_text('changed during run', encoding='utf-8')
            return importer(records, path, **kwargs)
        with patch.object(c, 'import_annotations', side_effect=changing_file):
            _, folder, updated = self.run_data(annotations=annotation)
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(original).hexdigest(), manifest['annotation_sha256'])
        self.assertEqual('positive', updated[0]['sentiment'])

    def test_annotation_rejects_contradictory_aspects_and_structured_annotator(self):
        _, _, rows = self.run_data([review()])
        item = {'review_id': rows[0]['review_id'], 'text_sha256': rows[0]['text_sha256'],
                'sentiment': 'positive', 'annotator': 'a'}
        annotation = self.root / 'annotations.jsonl'
        for bad in ({**item, 'annotator': {'user': 'a'}}, {**item, 'aspects': [
                    {'aspect': 'food', 'sentiment': 'positive'}, {'aspect': 'food', 'sentiment': 'negative'}]}):
            c.jsonl(annotation, [bad])
            with self.assertRaisesRegex(ValueError, 'Invalid annotations'):
                self.run_data(annotations=annotation)

    def test_main_integration_preserves_crawl_failure_and_skips_interrupt(self):
        main_path = HERE / 'main.py'
        runtime = HERE / 'ingestion_runtime.py'
        if not main_path.exists():
            main_path = HERE.parent / 'ingestion' / 'main.py'
            runtime = main_path.parent / 'ingestion_runtime.py'
        ingestion = self.root / 'src' / 'ingestion'
        processing = self.root / 'src' / 'processing'
        ingestion.mkdir(parents=True)
        processing.mkdir()
        shutil.copy2(main_path, ingestion / 'main.py')
        shutil.copy2(runtime, ingestion / 'ingestion_runtime.py')
        shutil.copy2(HERE / 'cleaner.py', processing / 'cleaner.py')
        sys.path.insert(0, str(ingestion))
        try:
            spec = importlib.util.spec_from_file_location('ingestion_main_test', ingestion / 'main.py')
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.write('gmap/gmap_test.json', [review()])
            def completed(jobs, stop, summary):
                mod.write_json(summary, {'sources': {'foody': {'status': 'failed'}, 'gmap': {'status': 'finished'}}})
                return 1
            args = ['--preprocess', '--output-dir', str(self.raw), '--processed-dir', str(self.out)]
            with patch.object(mod, 'run_jobs', side_effect=completed):
                self.assertEqual(1, mod.main(args))
            status = json.loads((self.raw / 'ingestion_status.json').read_text(encoding='utf-8'))
            self.assertEqual('finished', status['preprocessing']['status'])
            self.assertEqual('failed', status['sources']['foody']['status'])
            old = (self.out / 'latest.json').read_bytes()
            with patch.object(mod, 'run_jobs', return_value=130):
                self.assertEqual(130, mod.main(args))
            self.assertEqual(old, (self.out / 'latest.json').read_bytes())
        finally:
            sys.path.pop(0)


if __name__ == '__main__':
    unittest.main()
