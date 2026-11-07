"""Kiểm tra chia lô và khôi phục checkpoint, không cần MinIO/Chrome thật."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.ingestion import minio_upload as upload
from src.ingestion.foody import crawler_extended as crawler


def records(count, start=0, shop="quan-test"):
    return [
        {"URL Quán": f"https://www.foody.vn/hue/{shop}", "ID Bình Luận": index,
         "Bình Luận": "Món ngon 😊"}
        for index in range(start, start + count)
    ]


class UploadTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_file = Path(temporary.name) / "minio_upload_state.json"
        self.objects = {}
        self.sent = []
        self.client = Mock()
        self.client.fput_object.side_effect = self.save_object
        client_patch = patch.object(upload, "create_minio_client", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def save_object(self, bucket, name, file_path, **kwargs):
        payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        self.objects[name] = payload
        self.sent.append((name, payload))

    def new_uploader(self):
        return upload.MinioBatchUploader(self.state_file)


class MinioBatchUploadTests(UploadTestCase):
    def test_4501_records_upload_as_2000_2000_501(self):
        uploader = self.new_uploader()
        uploader.add_records(records(1_999))
        self.assertEqual(self.sent, [])
        uploader.add_records(records(2_502, start=1_999))
        self.assertEqual([len(batch) for _, batch in self.sent], [2_000, 2_000])
        uploader.flush()
        self.assertEqual([len(batch) for _, batch in self.sent], [2_000, 2_000, 501])
        self.assertEqual(len(self.objects), 3)
        self.assertEqual(
            [record["ID Bình Luận"] for _, batch in self.sent for record in batch],
            list(range(4_501)),
        )
        self.assertEqual(uploader.state["pending"], [])

    def test_batch_continues_across_shops_and_restarts(self):
        self.new_uploader().add_records(records(1_999))
        uploader = self.new_uploader()
        uploader.add_records(records(1_999))  # Cào lại cùng quán không tính hai lần.
        uploader.add_records(records(1, shop="quan-khac"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0][1]), 2_000)
        restarted = self.new_uploader()
        restarted.add_records(records(1_999) + records(1, shop="quan-khac"))
        restarted.flush()
        self.assertEqual(len(self.sent), 1)

    def test_failed_upload_keeps_payload_and_retries_same_object(self):
        self.client.fput_object.side_effect = OSError("Mất kết nối")
        with self.assertRaises(upload.BatchUploadError):
            self.new_uploader().add_records(records(2_000))
        checkpoint = json.loads(self.state_file.read_text(encoding="utf-8"))
        object_name = checkpoint["object_name"]
        self.assertEqual(len(checkpoint["pending"]), 2_000)
        self.assertEqual(checkpoint["uploaded_keys"], [])
        self.client.fput_object.side_effect = self.save_object
        self.new_uploader().add_records([])
        self.assertEqual(self.sent[0][0], object_name)
        self.assertEqual(len(self.sent[0][1]), 2_000)

    def test_failed_final_partial_batch_is_retried_before_new_records(self):
        uploader = self.new_uploader()
        uploader.add_records(records(3))
        self.client.fput_object.side_effect = OSError("Mất kết nối")
        with self.assertRaises(upload.BatchUploadError):
            uploader.flush()
        self.client.fput_object.side_effect = self.save_object
        restarted = self.new_uploader()
        restarted.add_records(records(2, start=3))
        self.assertEqual([len(batch) for _, batch in self.sent], [3])
        restarted.flush()
        self.assertEqual([len(batch) for _, batch in self.sent], [3, 2])

    def test_remote_success_then_checkpoint_failure_does_not_create_copy(self):
        real_write = upload.write_json

        def fail_after_upload(path, data):
            if path == self.state_file and data["uploaded_keys"]:
                raise OSError("Không ghi được checkpoint")
            real_write(path, data)

        with patch.object(upload, "write_json", side_effect=fail_after_upload):
            with self.assertRaises(upload.BatchUploadError):
                self.new_uploader().add_records(records(2_000))
        first_name = self.sent[0][0]
        self.new_uploader().add_records([])
        self.assertEqual([name for name, _ in self.sent], [first_name, first_name])
        self.assertEqual(len(self.objects), 1)
        self.assertEqual(len(self.objects[first_name]), 2_000)

    def test_empty_flush_does_not_upload_empty_file(self):
        uploader = self.new_uploader()
        uploader.add_records([])
        uploader.flush()
        self.assertEqual(self.sent, [])

    def test_folder_uses_vietnam_time_at_upload(self):
        utc_time = datetime(2026, 10, 17, 20, 30, tzinfo=timezone.utc)
        with patch.object(upload, "datetime") as clock:
            clock.now.side_effect = lambda tz: utc_time.astimezone(tz)
            uploader = self.new_uploader()
            uploader.add_records(records(2_000))
        self.assertTrue(self.sent[0][0].startswith("18-10-2026/foody_batch_"))
        self.assertEqual(clock.now.call_args.args, (upload.VIETNAM_TIME,))


class CrawlerMinioTests(UploadTestCase):
    """Chạy luồng crawler bằng browser và API giả, không truy cập Foody."""

    def prepare_crawler(self):
        data_dir = self.state_file.parent / "extended"
        checkpoints = data_dir / "checkpoints"
        checkpoints.mkdir(parents=True)
        shop_url = "https://www.foody.vn/hue/quan-test"
        (checkpoints / "foody_hue_links.txt").write_text(shop_url, encoding="utf-8")
        self.driver = Mock()
        for setting in (
            patch.object(crawler, "DATA_DIR", data_dir),
            patch.object(crawler.uc, "Chrome", return_value=self.driver),
            patch.object(crawler.time, "sleep"),
            patch.object(crawler, "process_minio_batch", return_value={"processed": 0}),
        ):
            setting.start()
            self.addCleanup(setting.stop)
        return data_dir, checkpoints

    def test_main_uploads_and_flushes_then_skips_completed_shop_on_restart(self):
        data_dir, checkpoints = self.prepare_crawler()

        def scrape(driver, url, on_records):
            data = records(4_501)
            on_records(data)
            self.assertEqual([len(batch) for _, batch in self.sent], [2_000, 2_000])
            return data, len(data)

        with patch.object(crawler, "scrape_shop", side_effect=scrape) as scrape_mock:
            crawler.main(cities=("hue",), fresh=False)
            crawler.main(cities=("hue",), fresh=False)
        self.assertEqual(scrape_mock.call_count, 1)
        self.assertEqual([len(batch) for _, batch in self.sent], [2_000, 2_000, 501])
        self.assertEqual(self.driver.quit.call_count, 2)
        dataset = json.loads((data_dir / "foody_hue_dataset.json").read_text(encoding="utf-8"))
        self.assertEqual(len(dataset), 4_501)
        self.assertIn("quan-test", (checkpoints / "foody_hue_progress_api_v2.txt").read_text())

    def test_main_stops_and_closes_browser_when_upload_fails(self):
        _, checkpoints = self.prepare_crawler()
        self.client.fput_object.side_effect = OSError("Mất kết nối")

        def scrape(driver, url, on_records):
            on_records(records(2_000))
            self.fail("Crawler phải dừng khi upload thất bại")

        with patch.object(crawler, "scrape_shop", side_effect=scrape):
            with self.assertRaises(upload.BatchUploadError):
                crawler.main(cities=("hue",), fresh=False)
        self.driver.quit.assert_called_once()
        self.assertFalse((checkpoints / "foody_hue_progress_api_v2.txt").exists())
        state = json.loads((checkpoints / "minio_upload_state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["pending"]), 2_000)

    def test_collect_uploads_while_still_inside_one_large_shop(self):
        uploader = self.new_uploader()
        groups = [
            {item["ID Bình Luận"]: item for item in records(200, start=start)}
            for start in range(0, 2_200, 200)
        ]
        callbacks = []

        def on_records(data):
            uploader.add_records(data)
            callbacks.append(len(self.sent))

        with patch.object(crawler, "load_review_group", side_effect=groups), patch.object(
            crawler, "api_review_record", side_effect=lambda item, *args: item
        ):
            result, expected = crawler.collect_all_reviews(
                Mock(), {"res_id": 1, "expected": 2_200}, "hue", "quan-test",
                "https://www.foody.vn/hue/quan-test", {}, on_records=on_records,
            )
        self.assertEqual(len(result), expected)
        self.assertEqual(callbacks, [0] * 9 + [1, 1])
        self.assertEqual([len(batch) for _, batch in self.sent], [2_000])


if __name__ == "__main__":
    unittest.main()
