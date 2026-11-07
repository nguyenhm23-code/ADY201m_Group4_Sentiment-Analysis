"""Batch ETL tests: real cleaning, durable logs, recovery; services are mocked."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.ingestion import batch_pipeline as pipeline
from src.ingestion import minio_upload as upload


def review(source_id=101):
    return {"Thành Phố": "hue", "ID Quán": "quan-test",
            "URL Quán": "https://www.foody.vn/hue/quan-test",
            "ID Bình Luận": source_id, "Tên User": "Lan", "Điểm Đánh Giá": 8,
            "Ngày Giờ": "18/10/2026 10:00", "Bình Luận": "Ngon"}


class BatchPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_file = self.root / "state.json"
        self.client = Mock()
        self.objects = {}
        self.responses = []

        def put(bucket, name, path, **kwargs):
            self.objects[(bucket, name)] = Path(path).read_bytes()

        def get(bucket, name):
            response = Mock()
            response.read.return_value = self.objects[(bucket, name)]
            self.responses.append(response)
            return response

        self.client.fput_object.side_effect = put
        self.client.get_object.side_effect = get
        self.db = Mock(return_value={"processed": 1, "inserted": 1, "updated": 0,
                                    "skipped_existing": 0, "duplicates_in_db": 0,
                                    "promoted_legacy_ids": 0, "replayed": False,
                                    "db_written_at": "2026-10-18T21:00:00+07:00"})
        for setting in (
            patch.object(upload, "create_minio_client", return_value=self.client),
            patch.object(pipeline, "upsert_reviews", self.db),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            setting.__enter__()
            self.addCleanup(setting.__exit__, None, None, None)

    def uploader(self, size=2000):
        return upload.MinioBatchUploader(self.state_file, batch_size=size,
                                         process_batch=pipeline.process_minio_batch)

    def events(self):
        return [json.loads(line) for line in
                (self.root / "ingestion_logs/events.jsonl").read_text("utf-8").splitlines()]

    def test_upload_read_clean_db_and_log_for_every_2000_and_remainder(self):
        def commit(frame, **kwargs):
            return {**self.db.return_value, "processed": len(frame), "inserted": len(frame)}

        self.db.side_effect = commit
        uploader = self.uploader()
        uploader.add_records([review(i) for i in range(1, 4502)])
        self.assertEqual([len(call.args[0]) for call in self.db.call_args_list], [2000, 2000])
        uploader.flush()
        self.assertEqual([len(call.args[0]) for call in self.db.call_args_list], [2000, 2000, 501])
        self.assertEqual(len(self.objects), 3)
        self.client.list_objects.assert_not_called()
        for response in self.responses:
            response.close.assert_called_once()
            response.release_conn.assert_called_once()
        summaries = [event for event in self.events() if event["event"] == "batch_completed"]
        self.assertEqual([event["inserted"] for event in summaries], [2000, 2000, 501])
        self.assertTrue(all(event["uploaded_at"].endswith("+07:00") for event in summaries))
        self.assertEqual(len(list((self.root / "ingestion_logs/batches").glob("*/summary.json"))), 3)

    def test_three_kinds_of_duplicates_and_quarantine_are_separate(self):
        # Different raw identities below normalize to the same source ID.
        duplicate = {**review(), "URL Quán": "https://www.foody.vn/hue/quan-other"}
        self.db.return_value.update(inserted=0, updated=1, duplicates_in_db=1)
        uploader = self.uploader(3)
        uploader.add_records([review(), review(), duplicate, {}])
        summary = next(event for event in self.events() if event["event"] == "batch_completed")
        self.assertEqual((summary["raw_rows"], summary["clean_rows"], summary["rejected_rows"]), (3, 1, 1))
        self.assertEqual((summary["duplicates_in_batch"], summary["duplicates_in_db"],
                          summary["crawler_duplicates_skipped_total"]), (1, 1, 1))
        self.assertEqual(len(json.loads(Path(summary["rejected_path"]).read_text("utf-8"))), 1)
        self.assertEqual(self.db.call_args.kwargs["return_stats"], True)

    def test_db_failure_keeps_pending_and_restart_uses_same_batch(self):
        self.db.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(upload.BatchUploadError):
            self.uploader(1).add_records([review()])
        state = json.loads(self.state_file.read_text("utf-8"))
        self.assertEqual(state["pending"], [review()])
        self.assertEqual(state["uploaded_keys"], [])
        failed = next(event for event in self.events() if event["event"] == "batch_failed")
        self.assertEqual(failed["stage"], "pipeline_database")
        self.db.side_effect = None
        self.uploader(1).add_records([])
        self.assertEqual(len(self.objects), 1)
        self.assertEqual(self.client.fput_object.call_args_list[0].args[1], state["object_name"])
        self.assertEqual(self.db.call_args_list[0].kwargs["batch_id"], self.db.call_args_list[1].kwargs["batch_id"])
        self.assertEqual(json.loads(self.state_file.read_text("utf-8"))["batch_count"], 1)

    def test_commit_then_checkpoint_failure_recovers_original_statistics(self):
        real_write = upload.write_json

        def fail_checkpoint(path, data):
            if path == self.state_file and data["batch_count"] == 1:
                raise OSError("checkpoint unavailable")
            real_write(path, data)

        with patch.object(upload, "write_json", side_effect=fail_checkpoint):
            with self.assertRaises(upload.BatchUploadError):
                self.uploader(1).add_records([review()])
        self.db.return_value["replayed"] = True
        self.uploader(1).add_records([])
        completed = [event for event in self.events() if event["event"] == "batch_completed"]
        self.assertTrue(completed[-1]["replayed"])
        self.assertEqual(completed[-1]["inserted"], 1)
        self.assertEqual(len(self.objects), 1)

    def test_all_invalid_rows_quarantined_and_batch_not_acknowledged(self):
        with self.assertRaises(upload.BatchUploadError):
            self.uploader(1).add_records([{}])
        rejected_path = next((self.root / "ingestion_logs/batches").glob("*/rejected.json"))
        self.assertEqual(len(json.loads(rejected_path.read_text("utf-8"))), 1)
        self.db.assert_not_called()
        self.assertEqual(json.loads(self.state_file.read_text("utf-8"))["pending"], [{}])

    def test_completion_log_failure_keeps_batch_pending_after_db_success(self):
        real_write = upload.write_json

        def fail_summary(path, data):
            if Path(path).name == "summary.json":
                raise OSError("log directory unavailable")
            real_write(path, data)

        with patch.object(upload, "write_json", side_effect=fail_summary):
            with self.assertRaises(upload.BatchUploadError):
                self.uploader(1).add_records([review()])
        self.db.assert_called_once()
        state = json.loads(self.state_file.read_text("utf-8"))
        self.assertEqual((state["batch_count"], len(state["pending"])), (0, 1))
        failed = next(event for event in self.events() if event["event"] == "batch_failed")
        self.assertEqual(failed["stage"], "completion_log")
        self.db.return_value["replayed"] = True
        self.uploader(1).add_records([])
        self.assertEqual(json.loads(self.state_file.read_text("utf-8"))["batch_count"], 1)

    def test_minio_integrity_failure_stops_before_db(self):
        response = Mock()
        response.read.return_value = b"[]"
        self.client.get_object.side_effect = None
        self.client.get_object.return_value = response
        with self.assertRaises(upload.BatchUploadError):
            self.uploader(1).add_records([review()])
        self.db.assert_not_called()
        response.close.assert_called_once()
        response.release_conn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
