"""Verify single-object ingestion using the real cleaner and mocked services."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.ingestion import load_single_file as job


def review():
    return {"Thành Phố": "ha-noi", "ID Quán": "quan-test",
            "URL Quán": "https://www.foody.vn/ha-noi/quan-test",
            "ID Bình Luận": 101, "Tên User": "Lan", "Điểm Đánh Giá": 8,
            "Ngày Giờ": "18/10/2026 10:00", "Bình Luận": "Ngon"}


class SingleFileLoadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "raw.json"
        self.output = self.root / "result"
        self.client = Mock()
        self.response = Mock()
        self.client.get_object.return_value = self.response
        self.client_patch = patch.object(job, "create_minio_client", return_value=self.client)
        self.config_patch = patch.object(job, "database_config")
        self.upsert_patch = patch.object(job, "upsert_reviews", return_value=1)
        self.client_factory = self.client_patch.start()
        self.config_patch.start()
        self.upsert = self.upsert_patch.start()
        self.addCleanup(patch.stopall)

    def run_job(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8-sig")
        self.source.write_bytes(payload)
        self.response.read.return_value = payload
        with contextlib.redirect_stdout(io.StringIO()):
            return job.load_single_file(self.source, "raw-test", 23, self.output)

    def test_only_uploaded_object_is_processed_with_dedup_and_quarantine(self):
        self.assertEqual(self.run_job([review(), review(), {}]), 1)
        self.client.list_objects.assert_not_called()
        uploaded = self.client.put_object.call_args.args
        self.assertEqual(uploaded[0], "raw-test")
        self.assertEqual(uploaded[2].getvalue(), self.source.read_bytes())
        self.client.get_object.assert_called_once_with("raw-test", uploaded[1])
        self.response.close.assert_called_once()
        self.response.release_conn.assert_called_once()
        frame = self.upsert.call_args.args[0]
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0].source_review_id, 101)
        self.assertEqual(self.upsert.call_args.kwargs, {"batch_size": 23})
        self.assertEqual(len(json.loads((self.output / "clean.json").read_text("utf-8"))), 1)
        self.assertEqual(len(json.loads((self.output / "rejected.json").read_text("utf-8"))), 1)

    def test_retry_reuses_object_and_single_object_input_is_supported(self):
        self.run_job(review())
        self.run_job(review())
        names = [call.args[1] for call in self.client.put_object.call_args_list]
        self.assertEqual(names[0], names[1])

    def test_malformed_or_empty_input_does_not_upload(self):
        for payload in (b"not JSON", b"[]", b"[1]", b"null"):
            self.source.write_bytes(payload)
            with self.assertRaises(ValueError):
                job.load_single_file(self.source, output_dir=self.output)
        self.client_factory.assert_not_called()
        self.upsert.assert_not_called()

    def test_all_rejected_preserves_report_and_does_not_touch_database(self):
        with self.assertRaises(ValueError):
            self.run_job([{}])
        self.assertEqual(len(json.loads((self.output / "rejected.json").read_text("utf-8"))), 1)
        self.upsert.assert_not_called()

    def test_download_error_closes_response_and_does_not_touch_database(self):
        self.response.read.side_effect = OSError("download interrupted")
        with self.assertRaises(OSError):
            self.run_job(review())
        self.response.close.assert_called_once()
        self.response.release_conn.assert_called_once()
        self.upsert.assert_not_called()

    def test_database_error_keeps_clean_output_and_propagates(self):
        self.upsert.side_effect = RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.run_job(review())
        self.assertTrue((self.output / "clean.json").is_file())
        self.client.remove_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
