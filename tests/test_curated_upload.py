import json
import tempfile
import unittest
from pathlib import Path
from src.processing import cleaner
from src.storage import lake
from test_storage import MemoryClient


class CuratedTests(unittest.TestCase):
    def test_city_uses_foody_url_over_wrong_record(self):
        row = cleaner.normalize({'Thành Phố':'ho-chi-minh', 'URL Quán':'https://www.foody.vn/da-nang/a',
            'Bình Luận':'Ngon', 'Điểm Đánh Giá':8}, 'foody', 'foody/foody_ho-chi-minh_dataset.json', cleaner.read_config())
        self.assertEqual(row['city'], 'Đà Nẵng')
        self.assertIn('record_city_mismatch', row['quality_flags'])

    def test_upload_processed_verifies_and_publishes_last(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'raw/gmap').mkdir(parents=True)
            (root/'raw/gmap/gmap_a.json').write_text(json.dumps([
                {'ID Quán':'a', 'ID Review':'1','Bình Luận':'Ngon', 'Điểm Đánh Giá':5},
                {'ID Quán':'a', 'ID Review':'1','Bình Luận':'Ngon', 'Điểm Đánh Giá':5},
                {'ID Quán':'a', 'ID Review':'2','Bình Luận':'','Điểm Đánh Giá':3}]), encoding='utf-8')
            run = cleaner.run_pipeline(root/'raw', root/'processed')
            self.assertTrue(run['row_reconciliation_ok'])
            self.assertEqual(run['normalized_rows'],2)
            client=MemoryClient()
            result=lake.upload_processed(client,'test',run['output'])
            self.assertTrue(result['verified_sha256'])
            self.assertEqual(client.writes[-1],'processed/latest.json')
            before=client.objects['test','processed/latest.json']
            client.fail_key=f'{result["prefix"]}/gmap.json'
            with self.assertRaises(OSError):
                lake.upload_processed(client,'test',run['output'])
            self.assertEqual(client.objects['test','processed/latest.json'],before)
            (Path(run['output'])/'gmap.json').write_text('[]')
            with self.assertRaises(ValueError):
                lake.upload_processed(client,'test',run['output'])


if __name__ == '__main__':
    unittest.main()
