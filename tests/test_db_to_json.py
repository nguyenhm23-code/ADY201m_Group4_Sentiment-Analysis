"""Check export completeness, empty output, and preservation after interruptions."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion import db_to_json as export


class DbToJsonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "reviews.json"
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.__iter__.return_value = iter(())
        for setting in (
            patch.object(export, "database_config", return_value={"dbname": "test"}),
            patch.object(export.psycopg2, "connect", return_value=self.connection),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            setting.__enter__()
            self.addCleanup(setting.__exit__, None, None, None)

    def test_complete_json_preserves_bigint_unicode_null_dates_and_numbers(self):
        self.cursor.__iter__.return_value = iter([
            ('{"review_id":9223372036854775807,"comment_text":"Ngon 😊",'
             '"rating":8.5,"device":null,"comment_datetime":"2026-10-18T21:00:00"}',),
            ('{"review_id":2,"comment_text":"Quán Huế"}',),
        ])
        self.assertEqual(export.export_db_to_json(self.output, batch_size=17), 2)
        rows = json.loads(self.output.read_text("utf-8"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["review_id"], 2**63 - 1)
        self.assertEqual(rows[0]["comment_text"], "Ngon 😊")
        self.assertEqual(rows[0]["rating"], 8.5)
        self.assertIsNone(rows[0]["device"])
        self.assertEqual(rows[0]["comment_datetime"], "2026-10-18T21:00:00")
        self.assertEqual(self.cursor.itersize, 17)
        self.connection.set_session.assert_called_once_with(readonly=True)
        self.connection.close.assert_called_once()

    def test_empty_database_writes_empty_array_and_replaces_previous_snapshot(self):
        self.output.write_text('[{"old":true}]', encoding="utf-8")
        self.assertEqual(export.export_db_to_json(self.output), 0)
        self.assertEqual(json.loads(self.output.read_text("utf-8")), [])

    def test_interrupted_stream_preserves_previous_output_and_removes_temp(self):
        self.output.write_text('[{"old":true}]', encoding="utf-8")

        def interrupted():
            yield ('{"review_id":1}',)
            raise OSError("connection lost")

        self.cursor.__iter__.side_effect = interrupted
        with self.assertRaisesRegex(OSError, "connection lost"):
            export.export_db_to_json(self.output)
        self.assertEqual(json.loads(self.output.read_text("utf-8")), [{"old": True}])
        self.assertEqual(list(self.root.iterdir()), [self.output])
        self.connection.close.assert_called_once()

    def test_invalid_batch_size_does_not_connect(self):
        with self.assertRaises(ValueError):
            export.export_db_to_json(self.output, batch_size=0)
        export.psycopg2.connect.assert_not_called()

    def test_direct_script_help_from_another_working_directory(self):
        result = subprocess.run([sys.executable, str(Path(export.__file__)), "--help"],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output", result.stdout)


if __name__ == "__main__":
    unittest.main()
