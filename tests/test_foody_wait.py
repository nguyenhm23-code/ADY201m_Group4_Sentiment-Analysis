import unittest
from unittest.mock import MagicMock, patch
from src.ingestion import crawler


def state(count=0, ready=None, bottom=True, loading=False):
    return {'count':count, 'ready':count if ready is None else ready, 'height':1000,
            'top':500 if bottom else 0, 'viewport':500, 'bottom':bottom, 'loading':loading}


class FoodyWaitTests(unittest.TestCase):
    def test_waits_for_html_arriving_after_old_sleep_duration(self):
        polls = [0]
        def snapshot(_):
            polls[0] += 1
            return state(2 if polls[0] >= 10 else 0)
        with patch.object(crawler,'pause'), patch.object(crawler,'foody_review_state',side_effect=snapshot):
            result = crawler.wait_foody_reviews(MagicMock())
        self.assertTrue(result['settled'])
        self.assertEqual(result['ready'],2)
        self.assertGreaterEqual(polls[0],13)

    def test_skeleton_cards_do_not_count_as_loaded_html(self):
        with patch.object(crawler,'pause'), patch.object(crawler,'foody_review_state',return_value=state(3,ready=0)):
            result = crawler.wait_foody_reviews(MagicMock(),timeout=10)
        self.assertFalse(result['settled'])

    def test_visible_loader_prevents_early_settle(self):
        with patch.object(crawler,'pause'), patch.object(crawler,'foody_review_state',return_value=state(3,loading=True)):
            result = crawler.wait_foody_reviews(MagicMock(),timeout=10)
        self.assertFalse(result['settled'])

    def test_unchanged_middle_of_page_does_not_trigger_end_of_list(self):
        driver=MagicMock()
        with patch.object(crawler,'pause'), patch.object(crawler,'foody_review_state',return_value=state(3,bottom=False)), \
             patch.object(crawler,'wait_foody_reviews') as wait, self.assertLogs(crawler.LOG,level='WARNING'):
            crawler.scroll_foody_reviews(driver,max_scrolls=5)
        scrolls = [call for call in driver.execute_script.call_args_list if 'window.scrollTo' in call.args[0]]
        self.assertEqual(len(scrolls),5)
        wait.assert_not_called()

    def test_bottom_is_confirmed_three_times_after_waiting(self):
        with patch.object(crawler,'pause'), patch.object(crawler,'foody_review_state',return_value=state(3)), \
             patch.object(crawler,'wait_foody_reviews',return_value={**state(3),'settled':True}) as wait:
            crawler.scroll_foody_reviews(MagicMock(),max_scrolls=10)
        self.assertEqual(wait.call_count,3)


if __name__=='__main__':
    unittest.main()
