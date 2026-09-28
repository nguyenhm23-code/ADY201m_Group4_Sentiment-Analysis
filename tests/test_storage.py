import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.storage import lake


class Response(io.BytesIO):
    def release_conn(self):
        pass


class MemoryClient:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.buckets = set()
        self.fail_key = None

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length, content_type=None):
        if key == self.fail_key:
            raise OSError('Simulated connection loss')
        payload = data.read(length)
        assert len(payload) == length
        self.objects[bucket, key] = payload
        self.writes.append(key)

    def fput_object(self, bucket, key, path, content_type=None):
        payload = Path(path).read_bytes()
        self.put_object(bucket, key, io.BytesIO(payload), len(payload), content_type)

    def get_object(self, bucket, key):
        return Response(self.objects[bucket, key])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.raw = self.folder / 'raw'
        (self.raw / 'foody').mkdir(parents=True)
        (self.raw / 'gmap').mkdir()
        self.client = MemoryClient()
        self.bucket = 'test-raw'
        self.write('foody/foody_da-nang_dataset.json', [{
            'ID Quán': '1', 'URL Quán': 'https://www.foody.vn/da-nang/test',
            'Thành Phố': 'da-nang', 'Bình Luận': 'Món ăn ngon và phục vụ chu đáo.',
            'Điểm Đánh Giá': '8', 'Ngày Giờ': '01/02/2025 12:00'}])

    def write(self, path, value):
        target = self.raw / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def upload(self):
        return lake.upload_snapshot(self.client, self.bucket, self.raw)

    def test_cli_returns_failure_when_processing_rejects_rows(self):
        with patch.object(lake, 'make_client', return_value=self.client), \
             patch.object(lake, 'process_snapshot', return_value={'processing': {'rejections': 1}}):
            self.assertEqual(1, lake.main(['process']))

    def test_upload_filters_profiles_status_queue_and_commits_last(self):
        self.write('gmap/.chrome_profile/secret.json', {'session': 'private'})
        self.write('gmap/gmap_test.status.json', {})
        self.write('gmap/gmap_test.place.json', {})
        self.write('gmap/places_queue.json', {'queue': []})
        self.write('gmap/diagnostics/gmap_test.json', {})
        self.write('gmap/gmap_test.partial.json', [])
        result = self.upload()
        self.assertEqual(result['files'], 2)
        self.assertEqual(self.client.writes[-1], 'raw/latest.json')
        self.assertEqual(self.client.writes[-2], f'raw/snapshots/{result["snapshot_id"]}/manifest.json')
        self.assertEqual(self.upload()['snapshot_id'], result['snapshot_id'])

    def test_round_trip_checks_bytes_and_is_idempotent(self):
        snapshot = self.upload()['snapshot_id']
        result = lake.download_snapshot(self.client, self.bucket, self.folder / 'cache')
        self.assertEqual(result['snapshot_id'], snapshot)
        restored = Path(result['raw_dir']) / 'foody/foody_da-nang_dataset.json'
        self.assertEqual(restored.read_bytes(), (self.raw / 'foody/foody_da-nang_dataset.json').read_bytes())
        self.assertEqual(lake.download_snapshot(self.client, self.bucket, self.folder / 'cache'), result)

    def test_interrupted_blob_upload_preserves_previous_pointer(self):
        self.upload()
        previous = self.client.objects[self.bucket, 'raw/latest.json']
        self.write('gmap/gmap_new.json', [{'new': 'review'}])
        checksum = lake.sha((self.raw / 'gmap/gmap_new.json').read_bytes())
        self.client.fail_key = f'raw/blobs/{checksum}.json'
        with self.assertRaises(OSError):
            self.upload()
        self.assertEqual(self.client.objects[self.bucket, 'raw/latest.json'], previous)

    def test_invalid_json_never_publishes(self):
        (self.raw / 'gmap/gmap_bad.json').write_text('{', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.upload()
        self.assertEqual(self.client.objects, {})

    def test_corrupt_blob_does_not_publish_cache(self):
        snapshot = self.upload()['snapshot_id']
        blob = next(key for key in self.client.objects if '/blobs/' in key[1])
        self.client.objects[blob] = b'[]'
        with self.assertRaises(ValueError):
            lake.download_snapshot(self.client, self.bucket, self.folder / 'cache')
        self.assertFalse((self.folder / 'cache/snapshots' / snapshot).exists())

    def test_tampered_cache_is_rejected(self):
        self.upload()
        result = lake.download_snapshot(self.client, self.bucket, self.folder / 'cache')
        cached = Path(result['raw_dir']) / 'foody/foody_da-nang_dataset.json'
        cached.write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'has changed'):
            lake.download_snapshot(self.client, self.bucket, self.folder / 'cache')

    def test_extra_cached_dataset_is_rejected(self):
        self.upload()
        result = lake.download_snapshot(self.client, self.bucket, self.folder / 'cache')
        (Path(result['raw_dir']) / 'foody/extra_dataset.json').write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            lake.download_snapshot(self.client, self.bucket, self.folder / 'cache')

    def test_manifest_path_traversal_and_duplicate_paths_rejected(self):
        for value in ('../foody/x_dataset.json', '/foody/x_dataset.json',
                      'C:/foody/x_dataset.json', 'foody\\x_dataset.json',
                      'foody/../x_dataset.json'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lake.valid_relative_path(value)
        entry = {'path': 'foody/x_dataset.json', 'sha256': 'a' * 64, 'bytes': 2}
        manifest = {'schema_version': 1, 'files': [entry, {**entry, 'path': 'foody/X_dataset.json'}]}
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            lake.validate_manifest(manifest, lake.sha(lake.encoded(manifest)))

    def test_process_restores_lake_then_writes_sqlite(self):
        self.upload()
        result = lake.process_snapshot(self.client, self.bucket, self.folder / 'cache', self.folder / 'processed')
        self.assertEqual(result['processing']['normalized_rows'], 1)
        database = Path(result['processing']['output']) / 'reviews.sqlite'
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)
        self.assertTrue((self.folder / 'processed/latest.json').is_file())


if __name__ == '__main__':
    unittest.main()
