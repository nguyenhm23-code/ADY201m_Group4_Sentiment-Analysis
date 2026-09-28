import unittest
from unittest.mock import patch
import undetected_chromedriver as uc
from src.ingestion import chrome_version


class ChromeVersionTests(unittest.TestCase):
    def test_default_driver_uses_installed_major(self):
        with patch.object(uc,'find_chrome_executable',return_value='chrome.exe'), \
             patch.object(chrome_version,'installed_chrome_major',return_value=153), \
             patch.object(chrome_version,'_make_chrome') as factory:
            chrome_version.make_chrome('gmap')
        self.assertEqual(factory.call_args.kwargs['chrome_major'],153)
        self.assertEqual(factory.call_args.kwargs['options'].binary_location,'chrome.exe')

    def test_explicit_major_is_preserved(self):
        with patch.object(uc,'find_chrome_executable',return_value='chrome.exe'), \
             patch.object(chrome_version,'installed_chrome_major') as detect, \
             patch.object(chrome_version,'_make_chrome') as factory:
            chrome_version.make_chrome('foody',chrome_major=152)
        detect.assert_not_called()
        self.assertEqual(factory.call_args.kwargs['chrome_major'],152)


if __name__=='__main__':
    unittest.main()
