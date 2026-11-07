"""Tests for source preservation, identity, and the explicit two-step pipeline."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import Mock, patch

from src.processing.foody import basic_pipeline as pipeline
from src.processing.foody.schema import FLAT_COLUMNS, RAW_FIELD_MAP
from src.ingestion import postgres_load as loader


def record(source_id=101, **extra):
    return {"Thành Phố": "ho-chi-minh", "ID Quán": "quan-test",
            "URL Quán": "https://www.foody.vn/ho-chi-minh/quan-test",
            "ID Bình Luận": source_id, "Tên User": " Lan ", "Điểm Đánh Giá": 8,
            "Ngày Giờ": "21/11/2023 21:46:44,", "Bình Luận": "Ngon 😊", **extra}


class PipelineTests(unittest.TestCase):
    def test_no_id_kept_and_original_name_preserved(self):
        row = pipeline.normalize_record(record(None))
        self.assertIsNone(row["source_review_id"])
        self.assertEqual(row["user_pseudo_id"], "lan")
        self.assertEqual(row["user_name"], " Lan ")
        self.assertEqual(row["comment_datetime"], datetime(2023, 11, 21, 21, 46, 44))
        self.assertEqual(set(row), set(FLAT_COLUMNS))
        self.assertNotIn("comment_hour", row)
        self.assertNotIn("label_source", row)

    def test_all_crawled_fields_mapped(self):
        extra = {"Thiết Bị": "Android", "Số Ảnh Bình Luận": 2,
                 "Giờ Mở Cửa": "08:00", "Giờ Đóng Cửa": "24:00",
                 "Giá Thấp Nhất": 10000, "Giá Cao Nhất": 50000, "Lượt Xem": 1000,
                 "Tổng Số Bình Luận": 100, "Điểm Trung Bình Quán": 7.5,
                 "Số Bình Luận Tuyệt Vời": 10, "Số Bình Luận Khá Tốt": 20,
                 "Số Bình Luận Trung Bình": 30, "Số Bình Luận Kém": 40,
                 "Điểm Vị Trí": 6.1, "Điểm Giá Cả": 6.2, "Điểm Chất Lượng": 6.3,
                 "Điểm Phục Vụ": 6.4, "Điểm Không Gian": 6.5}
        raw = record(**extra)
        row = pipeline.normalize_record(raw)
        self.assertEqual(len(raw), 26)
        self.assertTrue(set(raw) <= set(RAW_FIELD_MAP))
        for field, value in extra.items():
            actual = row[RAW_FIELD_MAP[field]]
            expected = time(8) if field == "Giờ Mở Cửa" else time(0) if field == "Giờ Đóng Cửa" else value
            self.assertEqual(actual, expected, field)
        self.assertEqual(row["shop_url"], raw["URL Quán"])

    def test_identity_dedup_without_collapsing_distinct_native_ids(self):
        frame = pipeline.build_dataframe([record(101), record(101), record(102), record(None), record(None)])
        self.assertEqual(len(frame), 3)  # Ambiguous no-ID match is retained.
        self.assertEqual(set(frame.source_review_id.dropna()), {101, 102})
        unique = pipeline.build_dataframe([record(101), record(None)])
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique.iloc[0].source_review_id, 101)
        legacy = pipeline.build_dataframe([record(None), record(None)])
        self.assertEqual(len(legacy), 1)

    def test_newest_non_null_shop_metadata_preserved(self):
        frame = pipeline.build_dataframe([
            record(101, **{"Lượt Xem": 100, "Điểm Trung Bình Quán": 7.1}),
            record(102, **{"Lượt Xem": None, "Điểm Trung Bình Quán": 7.2}),
        ])
        self.assertEqual(list(frame.views), [100, 100])
        self.assertEqual(list(frame.shop_avg_rating), [7.2, 7.2])

    def test_quarantine_retains_entire_invalid_record(self):
        invalid = record(102, **{"Điểm Đánh Giá": "Không có điểm"})
        rejected = []
        frame = pipeline.build_dataframe([record(), invalid], rejected)
        self.assertEqual(len(frame), 1)
        self.assertEqual(rejected[0]["raw_record"], invalid)
        with self.assertRaises(ValueError):
            pipeline.normalize_record(record(**{"new_field": 123}))
        with self.assertRaises(ValueError):
            pipeline.normalize_record(record(**{"Giá Thấp Nhất": 10, "Giá Cao Nhất": 5}))

    def test_bigint_exact_and_json_roundtrip(self):
        frame = pipeline.build_dataframe([record(2**63 - 1), record(None, **{"Bình Luận": "Khác"})])
        rows = [{k: loader.to_database_value(v) for k, v in row.items()} for row in frame.to_dict("records")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean.json"
            pipeline.save_json(path, rows)
            loaded = pipeline.load_clean_dataframe(path)
            self.assertEqual(loaded.iloc[0].source_review_id, 2**63 - 1)
            self.assertIsNone(loaded.iloc[1].source_review_id)
            self.assertIsInstance(loaded.iloc[0].comment_datetime, datetime)
            self.assertNotIn("NaN", path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            pipeline.normalize_record(record(2**63))

    def test_minio_response_closed_on_bad_json(self):
        response = Mock()
        response.read.return_value = b'bad json'
        client = Mock()
        client.list_objects.return_value = [Mock(object_name="data.json", last_modified=datetime.now())]
        client.get_object.return_value = response
        with patch.object(pipeline, "minio_client", return_value=client):
            with self.assertRaises(json.JSONDecodeError):
                pipeline.load_raw_records_from_minio("raw")
        response.close.assert_called_once()
        response.release_conn.assert_called_once()

    def test_direct_scripts_from_their_own_directories(self):
        root = pipeline.PROJECT_ROOT
        for relative in ("src/processing/foody/basic_pipeline.py", "src/ingestion/postgres_load.py"):
            script = root / relative
            result = subprocess.run([sys.executable, str(script), "--help"], cwd=script.parent,
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_loader_requires_clean_contract(self):
        with self.assertRaises(ValueError):
            loader.upsert_reviews(pipeline.build_dataframe([]))
        frame = pipeline.build_dataframe([record()])
        frame["comment_hour"] = 21
        with self.assertRaises(ValueError):
            loader.upsert_reviews(frame)


if __name__ == "__main__":
    unittest.main()
