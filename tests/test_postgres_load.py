import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.utils import postgres_load as loader


def review(review_id="r1", rating=8.0):
    row = dict.fromkeys(loader.COLUMNS)
    row.update(review_id=review_id, shop_id=123, rating=rating)
    return row


class PostgresLoadTests(unittest.TestCase):
    def test_empty_dataframe_does_not_connect(self):
        with patch.object(loader.psycopg2, "connect") as connect:
            self.assertEqual(loader.upsert_reviews(pd.DataFrame()), 0)
            connect.assert_not_called()

    def test_invalid_input_does_not_connect(self):
        with patch.object(loader.psycopg2, "connect") as connect:
            with self.assertRaises(ValueError):
                loader.upsert_reviews(pd.DataFrame([{"review_id": "r1"}]))
            with self.assertRaises(ValueError):
                loader.upsert_reviews(pd.DataFrame([review(None)]))
            connect.assert_not_called()

    def test_nulls_and_timestamps(self):
        for value in [None, float("nan"), pd.NaT, pd.NA]:
            self.assertIsNone(loader.to_database_value(value))
        value = pd.Timestamp("2026-01-01", tz="UTC")
        self.assertEqual(loader.to_database_value(value), value.to_pydatetime())

    def test_duplicate_keys_use_last_row_and_batches_share_transaction(self):
        connection = MagicMock()
        rows = pd.DataFrame([review("r1", 1), review("r1", 9), review("r2", 7)])
        with patch.object(loader, "database_config", return_value={}), \
                patch.object(loader.psycopg2, "connect", return_value=connection), \
                patch.object(loader, "execute_values") as execute:
            self.assertEqual(loader.upsert_reviews(rows, batch_size=1), 2)
        self.assertEqual(execute.call_count, 2)
        first_row = execute.call_args_list[0].args[2][0]
        self.assertEqual(first_row[loader.COLUMNS.index("rating")], 9)
        self.assertIsNone(first_row[loader.COLUMNS.index("comment_datetime")])
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once_with(None, None, None)
        connection.close.assert_called_once()

    def test_write_error_reaches_transaction_and_closes_connection(self):
        connection = MagicMock()
        with patch.object(loader, "database_config", return_value={}), \
                patch.object(loader.psycopg2, "connect", return_value=connection), \
                patch.object(loader, "execute_values", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                loader.upsert_reviews(pd.DataFrame([review()]))
        self.assertIs(connection.__exit__.call_args.args[0], RuntimeError)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
