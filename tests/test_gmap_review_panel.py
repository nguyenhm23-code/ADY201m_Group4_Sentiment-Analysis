"""Regressions from the September 27 overview/preview diagnostics."""
import threading
import unittest
from unittest.mock import MagicMock, patch

from src.ingestion import GGMap as gmap
from src.ingestion.ingestion_runtime import CrawlCancelled


class ReviewPanelTests(unittest.TestCase):
    def test_long_reviews_keep_scrolling_until_new_ids_arrive(self):
        clock = [0]
        index = [0]
        old, new = MagicMock(), MagicMock()
        old.get_attribute.return_value = 'old'
        new.get_attribute.return_value = 'new'
        class Wait:
            def until(self, predicate):
                clock[0] += 8
                index[0] += 1
                raise gmap.TimeoutException()
        place = {'id':'place', 'url':'https://www.google.com/maps/place/Test/'}
        with patch.object(gmap, 'open_reviews'), \
             patch.object(gmap, 'review_panel_state', return_value={'full':True}), \
             patch.object(gmap, 'cards', side_effect=lambda _: [new if index[0] >= 4 else old]), \
             patch.object(gmap, 'read_card', side_effect=lambda _,card: {'review_id':card.get_attribute('data-review-id'),'rating':5,'text':'Ngon'}), \
             patch.object(gmap, 'scroll_reviews'), patch.object(gmap, 'WebDriverWait', return_value=Wait()), \
             patch.object(gmap, 'review_scroll_state', side_effect=lambda _: {'bottom':False,'top':index[0]*400}), \
             patch.object(gmap.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(gmap.time, 'sleep'):
            rows, reason = gmap.collect_reviews(MagicMock(),place,2,MagicMock(),idle_seconds=10,max_place_seconds=60)
        self.assertEqual(reason,'max_reviews')
        self.assertEqual(len(rows),2)
        self.assertGreater(clock[0],10)

    def test_city_accepts_postcode_but_not_street_substring(self):
        for address in ('114 Lý Tự Trọng, Bến Thành, Hồ Chí Minh 700000, Việt Nam',
                        'Quận 1, TP. Hồ Chí Minh 70000, Việt Nam',
                        'Quan 1, Ho Chi Minh City 700000, Vietnam'):
            self.assertEqual(gmap.city_from_address(address), 'Hồ Chí Minh')
        self.assertIsNone(gmap.city_from_address('1 Đường Hồ Chí Minh, Xã Khác, Việt Nam'))
        self.assertEqual(gmap.city_from_address('84 Đống Đa, Quy Nhơn, Gia Lai, Việt Nam'), 'Quy Nhơn')

    def test_overview_three_cards_never_pass_as_full_reviews(self):
        driver = MagicMock()
        waiter = MagicMock()
        waiter.until.side_effect = gmap.TimeoutException()
        preview = {'full': False, 'cards': 3, 'selected_tab': 'Tổng quan', 'more': True}
        with patch.object(gmap, 'review_panel_state', return_value=preview), \
             patch.object(gmap, 'review_control', return_value=None), \
             patch.object(gmap, 'WebDriverWait', return_value=waiter):
            with self.assertRaisesRegex(gmap.CrawlError, 'đầy đủ'):
                gmap.open_reviews(driver)
        driver.refresh.assert_called_once()
        self.assertLessEqual(waiter.until.call_count, 4)

    def test_persistent_login_dialog_stops_after_one_reload(self):
        driver = MagicMock()
        with patch.object(gmap, 'review_panel_state', return_value={'full': False, 'login_required': True}), \
             patch.object(gmap, 'WebDriverWait') as wait:
            with self.assertRaisesRegex(gmap.ReviewAccessError, 'đăng nhập'):
                gmap.open_reviews(driver, headless=True, login_wait=0)
        wait.assert_not_called()
        driver.refresh.assert_called_once()

    def test_full_panel_does_not_click_any_expander(self):
        with patch.object(gmap, 'review_panel_state', return_value={'full': True}), \
             patch.object(gmap, 'review_control') as control:
            gmap.open_reviews(MagicMock())
        control.assert_not_called()

    def test_cancel_before_clicking_or_refreshing(self):
        stop = threading.Event()
        stop.set()
        driver = MagicMock()
        with self.assertRaises(CrawlCancelled):
            gmap.open_reviews(driver, stop_event=stop)
        driver.refresh.assert_not_called()
        driver.execute_script.assert_not_called()

    def test_resume_crosses_old_cards_before_reaching_new_review(self):
        clock = [0]
        cards = []
        for rid in ('old1', 'old2', 'old3', 'new'):
            card = MagicMock()
            card.get_attribute.return_value = rid
            cards.append(card)
        index = [0]
        class Wait:
            def until(self, predicate):
                clock[0] += 8
                index[0] += 1
                raise gmap.TimeoutException()
        initial = [{'ID Review': rid, 'ID Quán': 'place'} for rid in ('old1', 'old2', 'old3')]
        place = {'id': 'place', 'url': 'https://www.google.com/maps/place/Test/'}
        with patch.object(gmap, 'open_reviews'), \
             patch.object(gmap, 'review_panel_state', return_value={'full': True}), \
             patch.object(gmap, 'cards', side_effect=lambda _: [cards[index[0]]]), \
             patch.object(gmap, 'read_card', return_value={'review_id': 'new', 'rating': 5, 'text': 'Ngon'}), \
             patch.object(gmap, 'scroll_reviews'), patch.object(gmap, 'current_ids', return_value=set()), \
             patch.object(gmap, 'WebDriverWait', return_value=Wait()), \
             patch.object(gmap, 'review_scroll_state', return_value={'bottom': False}), \
             patch.object(gmap.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(gmap.time, 'sleep'):
            rows, reason = gmap.collect_reviews(MagicMock(), place, 4, MagicMock(), initial,
                                                idle_seconds=10, max_place_seconds=60)
        self.assertEqual(reason, 'max_reviews')
        self.assertEqual(len(rows), 4)
        self.assertGreater(clock[0], 10)


if __name__ == '__main__':
    unittest.main()
