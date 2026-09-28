"""Foody output contract, numeric parsing and missing-data regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import NoSuchElementException
from src.ingestion import crawler
from src.ingestion.ingestion_runtime import write_json


URL = 'https://www.foody.vn/ho-chi-minh/test'
REVIEW_URL = URL + '/binh-luan-123'


def element(text):
    return MagicMock(text=text, get_attribute=MagicMock(return_value=text))


def restaurant_driver(values):
    driver = MagicMock()
    driver.find_elements.side_effect = lambda by, selector: (
        [element(values[selector])] if selector in values else [])
    return driver


class FoodySchemaTests(unittest.TestCase):
    def test_number_formats(self):
        for text, integer, expected in (
            ('7.4', False, 7.4), ('6,9', False, 6.9), ('0', False, 0.0),
            ('30.000đ', True, 30000), ('300,000 ₫', True, 300000),
            ('26.1K', True, 26100), ('1,2M', True, 1200000),
            ('1\xa0234', True, 1234), ('0', True, 0),
            ('{{Model.Total}}', True, None), ('', True, None),
            ('Không rõ', True, None), ('-1', True, None),
            ('11', False, None), ('7.4.2', False, None),
        ):
            with self.subTest(text=text):
                self.assertEqual(crawler.foody_number(text, integer=integer), expected)

    def test_restaurant_fields_match_requested_values_and_types(self):
        driver = restaurant_driver({
            'tr:has(#positionPointBar) b': '7.4',
            'tr:has(#pricePointBar) b': '6.9',
            'tr:has(#foodPointBar) b': '7.2',
            'tr:has(#servicePointBar) b': '6.5',
            'tr:has(#atmospherePointBar) b': '7.0',
            '.ratings-boxes-points b': '7.0',
            '.micro-timesopen > span:not([class])': '\xa010:00 - 22:00',
            '.res-common-minmaxprice': '\n30.000đ - 300.000đ\n',
            '.total-views > span': '26.1K',
            '.ratings-boxes .summary b, .microsite-review-count': '269',
            '.ratings-numbers b.exellent': '24',
            '.ratings-numbers b.good': '169',
            '.ratings-numbers b.average': '42',
            '.ratings-numbers b.bad': '34',
        })
        self.assertEqual(crawler.read_foody_restaurant(driver), {
            'Điểm Vị Trí': 7.4, 'Điểm Giá Cả': 6.9, 'Điểm Chất Lượng': 7.2,
            'Điểm Phục Vụ': 6.5, 'Điểm Không Gian': 7.0, 'Điểm Trung Bình Quán': 7.0,
            'Giờ Mở Cửa': '10:00', 'Giờ Đóng Cửa': '22:00',
            'Giá Thấp Nhất': 30000, 'Giá Cao Nhất': 300000, 'Lượt Xem': 26100,
            'Tổng Số Bình Luận': 269, 'Số Bình Luận Tuyệt Vời': 24,
            'Số Bình Luận Khá Tốt': 169, 'Số Bình Luận Trung Bình': 42,
            'Số Bình Luận Kém': 34,
        })

    def test_missing_metadata_is_null_not_zero(self):
        info = crawler.read_foody_restaurant(restaurant_driver({}))
        self.assertEqual(len(info), 16)
        self.assertTrue(all(value is None for value in info.values()))

    def test_zero_statistics_are_not_treated_as_missing(self):
        info = crawler.read_foody_restaurant(restaurant_driver({
            '.ratings-numbers b.bad': '0', '.total-views > span': '0',
        }))
        self.assertEqual(info['Số Bình Luận Kém'], 0)
        self.assertEqual(info['Lượt Xem'], 0)

    def test_each_review_gets_metadata_and_full_photo_count(self):
        card = MagicMock()
        values = {
            'a.ru-username': 'Vũ Phan', 'div.review-points span': '1.0',
            'a.ru-device': 'via MobileWeb', 'span.ru-time': '19/12/2019 20:55',
            'div.rd-des span': 'Mình đặt khoai nghiền nhưng quán lại giao salad.',
            '.ru-stats a[href*="/binh-luan-"], .rd-title[href*="/binh-luan-"]': REVIEW_URL,
            'div.review-points': 'review_123',
        }
        def find(by, selector):
            if selector not in values:
                raise NoSuchElementException(selector)
            return element(values[selector])
        card.find_element.side_effect = find
        driver = MagicMock()
        driver.find_elements.side_effect = lambda by, selector: [card] if selector == 'li.review-item' else []
        for count in (0, 15, None):
            with self.subTest(count=count), patch.object(crawler, 'read_foody_photo_counts',
                    return_value={REVIEW_URL: count} if count is not None else {}):
                row = crawler.read_foody_reviews(URL, driver)[0]
                self.assertEqual(row['Số Ảnh Bình Luận'], count)
                self.assertEqual(row['Điểm Đánh Giá'], '1.0')
                self.assertEqual(row['Thiết Bị'], 'MobileWeb')
                self.assertEqual(row['Thành Phố'], 'ho-chi-minh')
                self.assertIn('Giá Cao Nhất', row)
                self.assertIn('Số Bình Luận Kém', row)
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / 'output.json'
                    write_json(path, [row])
                    self.assertEqual(json.loads(path.read_text(encoding='utf-8')), [row])
                    self.assertIn('Vũ Phan', path.read_text(encoding='utf-8'))
        # A review without a title still has its rating's native review ID.
        values.pop('.ru-stats a[href*="/binh-luan-"], .rd-title[href*="/binh-luan-"]')
        with patch.object(crawler, 'read_foody_photo_counts', return_value={'review_123': 29}):
            self.assertEqual(crawler.read_foody_reviews(URL, driver)[0]['Số Ảnh Bình Luận'], 29)


if __name__ == '__main__':
    unittest.main()
