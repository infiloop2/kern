"""Database storage contract: catalog reads, operator boundary, live schema coverage."""
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime.admin_api import database_storage, service
from host.runtime.core import db


class DatabaseStorageTests(unittest.TestCase):
    def test_snapshot_preserves_unknown_estimates_and_unknown_policy(self):
        cur = MagicMock()
        cur.fetchone.return_value = (8192,)
        cur.fetchall.return_value = [("agent_events", 7500000, 4096), ("new_table", None, 0)]
        with patch.object(db, "transaction") as transaction:
            transaction.return_value.__enter__.return_value = cur
            report = database_storage.snapshot()
        self.assertEqual(report["database_bytes"], 8192)
        known, unknown = report["tables"]
        self.assertEqual(known["row_target"], 100000000)
        self.assertEqual(known["estimated_rows"], 7500000)
        self.assertIn("all conversations", known["retention"])
        self.assertIsNone(unknown["estimated_rows"])
        self.assertIsNone(unknown["row_target"])
        self.assertIn("Policy not documented", unknown["retention"])
        self.assertEqual(cur.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))

    def test_operator_read_only_route_rejects_queries_writes_and_workspace(self):
        operator = service.OperatorPrincipal("test-session")
        with patch.object(database_storage, "snapshot", return_value={"tables": []}) as read:
            self.assertEqual(service.route("GET", "/v1/database-storage", {}, None, principal=operator), {"tables": []})
            read.assert_called_once_with()
            read.reset_mock()
            for method, query, principal, status in (
                ("GET", {"table": ["secret_keys"]}, operator, 400),
                ("POST", {}, operator, 404),
                ("DELETE", {}, operator, 404),
                ("GET", {}, service.WorkspacePrincipal(), 403),
            ):
                with self.subTest(method=method, query=query, principal=principal):
                    with self.assertRaises(service.ApiError) as error:
                        service.route(method, "/v1/database-storage", query, None, principal=principal)
                    self.assertEqual(error.exception.status, status)
            read.assert_not_called()


class DatabaseStoragePostgresTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def test_every_migrated_table_has_a_policy_and_sizes_match_postgres(self):
        report = database_storage.snapshot()
        self.assertEqual({table["name"] for table in report["tables"]}, set(database_storage.POLICIES))
        self.assertGreater(report["database_bytes"], 0)
        sizes = [table["total_bytes"] for table in report["tables"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertTrue(all(size >= 0 for size in sizes))
        with db.transaction() as cur:
            cur.execute("SELECT pg_total_relation_size('public.agent_events'::regclass)")
            row = cur.fetchone()
            assert row is not None
            expected_size = row[0]
        actual = next(table for table in report["tables"] if table["name"] == "agent_events")
        self.assertEqual(actual["total_bytes"], expected_size)

    def test_estimates_after_analyze_and_no_stored_contents_returned(self):
        try:
            with db.transaction() as cur:
                cur.execute("CREATE TABLE public.storage_probe (id integer, private_value text)")
            initial = next(row for row in database_storage.snapshot()["tables"] if row["name"] == "storage_probe")
            self.assertIsNone(initial["estimated_rows"])
            with db.transaction() as cur:
                cur.execute("INSERT INTO public.storage_probe SELECT n, 'private-payload' FROM generate_series(1, 40) n")
                cur.execute("ANALYZE public.storage_probe")
            report = database_storage.snapshot()
            probe = next(row for row in report["tables"] if row["name"] == "storage_probe")
            self.assertEqual(probe["estimated_rows"], 40)
            self.assertGreater(probe["total_bytes"], 0)
            self.assertIn("Policy not documented", probe["retention"])
            self.assertNotIn("private-payload", str(report))
            self.assertNotIn("private_value", str(report))
        finally:
            with db.transaction() as cur:
                cur.execute("DROP TABLE IF EXISTS public.storage_probe")
