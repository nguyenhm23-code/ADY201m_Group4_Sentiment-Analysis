"""Real PostgreSQL tests in isolated, disposable schemas only."""
import os
import unittest
import uuid
from unittest.mock import patch

import psycopg2
from psycopg2 import sql

from src.ingestion import postgres_load as loader
from src.processing.foody.basic_pipeline import build_dataframe
from test_postgres_load import record


@unittest.skipUnless(os.getenv("ETL_TEST_POSTGRES_DSN"), "ETL_TEST_POSTGRES_DSN not configured")
class PostgresIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["ETL_TEST_POSTGRES_DSN"]
        self.schema = "etl_test_" + uuid.uuid4().hex
        self.connection = psycopg2.connect(self.dsn)
        self.addCleanup(self.connection.close)
        self.connection.autocommit = True
        with self.connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        replacements = {"DB_SCHEMA": self.schema}
        for name in ("CREATE_TABLE_SQL", "INDEX_SQL", "VIEW_SQL", "SHOP_UPSERT_SQL",
                     "USER_UPSERT_SQL", "STAGE_SQL", "HASH_COLLISION_SQL", "PROMOTE_SQL", "NATIVE_UPSERT_SQL", "NATURAL_UPSERT_SQL",
                     "BATCH_RECEIPT_SQL", "FIND_BATCH_SQL", "SAVE_BATCH_SQL"):
            value = getattr(loader, name)
            replacements[name] = (tuple(v.replace("public.", self.schema + ".") for v in value)
                                  if isinstance(value, tuple) else value.replace("public.", self.schema + "."))
        patcher = patch.multiple(loader, **replacements)
        patcher.start()
        self.addCleanup(patcher.stop)
        config = patch.object(loader, "database_config", return_value={"dsn": self.dsn})
        config.start()
        self.addCleanup(config.stop)

    def drop_schema(self):
        with self.connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def query(self, statement):
        with self.connection.cursor() as cursor:
            cursor.execute(statement.replace("public.", self.schema + "."))
            return cursor.fetchall() if cursor.description else None

    def test_reimport_is_idempotent_preserves_metadata_and_first_seen(self):
        frame = build_dataframe([record(101, **{"Lượt Xem": 100}), record(102)])
        loader.upsert_reviews(frame, batch_size=1)
        original = self.query("SELECT review_id, source_review_id FROM public.reviews ORDER BY source_review_id")
        first_seen = self.query("SELECT first_seen_at FROM public.users")
        loader.upsert_reviews(build_dataframe([record(101), record(102)]))
        self.assertEqual(self.query("SELECT review_id, source_review_id FROM public.reviews ORDER BY source_review_id"), original)
        self.assertEqual(self.query("SELECT views FROM public.shops"), [(100,)])
        self.assertEqual(self.query("SELECT first_seen_at FROM public.users"), first_seen)
        self.assertEqual(self.query("SELECT user_pseudo_id, user_name FROM public.users"), [("lan", " Lan ")])

    def test_promote_no_id_keeps_surrogate_and_no_id_reimport_skips_duplicate(self):
        loader.upsert_reviews(build_dataframe([record(None)]))
        original = self.query("SELECT review_id FROM public.reviews")[0][0]
        loader.upsert_reviews(build_dataframe([record(2**63 - 1)]))
        self.assertEqual(self.query("SELECT review_id, source_review_id FROM public.reviews"), [(original, 2**63 - 1)])
        loader.upsert_reviews(build_dataframe([record(None)]))
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.reviews"), [(1,)])

    def test_distinct_source_ids_with_same_natural_key_retained(self):
        frame = build_dataframe([record(101), record(102), record(None)])
        loader.upsert_reviews(frame)
        loader.upsert_reviews(frame)
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.reviews"), [(3,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.foody_reviews_flat"), [(3,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.reviews WHERE source_review_id IS NULL"), [(1,)])

    def test_transaction_rolls_back_parents_on_invalid_review(self):
        loader.upsert_reviews(build_dataframe([record()]))
        invalid = build_dataframe([record(102, **{"Tên User": "New", "ID Quán": "new-shop"})])
        invalid.loc[0, "rating"] = 11
        with self.assertRaises(psycopg2.Error):
            loader.upsert_reviews(invalid)
        for table in ("shops", "users", "reviews"):
            self.assertEqual(self.query(f"SELECT COUNT(*) FROM public.{table}"), [(1,)])

    def test_schema_mismatch_refused_and_legacy_table_preserved(self):
        self.query("CREATE TABLE public.foody_reviews (marker TEXT)")
        self.query("INSERT INTO public.foody_reviews VALUES ('legacy')")
        loader.upsert_reviews(build_dataframe([record()]))
        self.assertEqual(self.query("SELECT marker FROM public.foody_reviews"), [("legacy",)])
        self.query("ALTER TABLE public.reviews ADD COLUMN comment_hour INT")
        with self.assertRaises(ValueError):
            loader.upsert_reviews(build_dataframe([record(102)]))
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.reviews"), [(1,)])

    def test_long_unidentified_comment_stored_in_full(self):
        comment = " ".join(uuid.uuid4().hex for _ in range(500))
        frame = build_dataframe([record(None, **{"Bình Luận": comment})])
        loader.upsert_reviews(frame)
        loader.upsert_reviews(frame)
        self.assertEqual(self.query("SELECT comment_text FROM public.reviews"), [(comment,)])

    def test_committed_counts_and_receipt_survive_retries(self):
        first = loader.upsert_reviews(build_dataframe([record(101), record(102)]),
                                      return_stats=True, batch_id="batch-1")
        self.assertEqual((first["inserted"], first["updated"], first["duplicates_in_db"]), (2, 0, 0))
        retry = loader.upsert_reviews(build_dataframe([record(101), record(102)]),
                                      return_stats=True, batch_id="batch-1")
        self.assertTrue(retry.pop("replayed"))
        self.assertEqual(retry, {k: v for k, v in first.items() if k != "replayed"})
        mixed = loader.upsert_reviews(build_dataframe([record(102), record(103)]),
                                      return_stats=True, batch_id="batch-2")
        self.assertEqual((mixed["inserted"], mixed["updated"], mixed["duplicates_in_db"]), (1, 1, 1))
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.foody_ingestion_batches"), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM public.reviews"), [(3,)])

    def test_counts_for_legacy_promotion_and_unidentified_skip(self):
        loader.upsert_reviews(build_dataframe([record(None)]))
        promoted = loader.upsert_reviews(build_dataframe([record(101)]), return_stats=True)
        self.assertEqual((promoted["inserted"], promoted["updated"], promoted["promoted_legacy_ids"]), (0, 1, 1))
        skipped = loader.upsert_reviews(build_dataframe([record(None)]), return_stats=True)
        self.assertEqual((skipped["inserted"], skipped["updated"], skipped["skipped_existing"], skipped["duplicates_in_db"]),
                         (0, 0, 1, 1))

    def test_failed_batch_leaves_no_receipt(self):
        loader.upsert_reviews(build_dataframe([record()]), return_stats=True, batch_id="good")
        invalid = build_dataframe([record(102)])
        invalid.loc[0, "rating"] = 11
        with self.assertRaises(psycopg2.Error):
            loader.upsert_reviews(invalid, return_stats=True, batch_id="bad")
        self.assertEqual(self.query("SELECT batch_id FROM public.foody_ingestion_batches"), [("good",)])


if __name__ == "__main__":
    unittest.main()
