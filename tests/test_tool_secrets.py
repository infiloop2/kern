"""Generic tool secret storage: limits locally, encryption/isolation in CI Postgres."""

import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.core import db, state
from host.runtime.tools import tools_host


def store(tool="first"):
    return tools_host.HostSecrets(tool)


class SecretValidationTests(unittest.TestCase):
    def test_rejects_invalid_or_oversized_values_before_storage(self):
        for value in (
            [], {"x": float("nan")}, {"x": object()}, {"x": "x" * 16384},
            {"nested": [{"tuple": (1, 2)}]}, {"nested": {1: "integer key"}},
            {"nested": [{True: "boolean key"}]}, {None: "null key"}, {"set": {1, 2}},
        ):
            with self.subTest(value_type=type(value)), patch.object(state, "put_tool_secret") as write, self.assertRaises(ValueError):
                store().save(value)
            write.assert_not_called()

    def test_exact_limit_is_scoped_by_tool(self):
        with patch.object(state, "put_tool_secret") as write:
            store().save({"x": "x" * (16384 - 8)})
        tool, serialized = write.call_args.args
        self.assertEqual((tool, len(serialized.encode())), ("first", 16384))


class SecretStorageTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def test_encrypted_round_trip_isolation_replace_and_clear(self):
        private = {"verifier": "PRIVATE_TEST_SECRET", "nested": [1, True, None]}
        one = store()
        self.assertIsNone(one.load())
        one.save(private)
        self.assertEqual(one.load(), private)
        with db.transaction() as cur:
            cur.execute("SELECT value FROM tool_secrets")
            ciphertext = cur.fetchone()[0]
        self.assertTrue(ciphertext.startswith("enc:v1:"))
        self.assertNotIn("PRIVATE_TEST_SECRET", ciphertext)
        self.assertIsNone(store(tool="other").load())
        one.save({"replacement": True})
        self.assertEqual(store().load(), {"replacement": True})
        store(tool="other").save({"keep": True})
        one.clear()
        one.clear()
        self.assertIsNone(one.load())
        self.assertEqual(store(tool="other").load(), {"keep": True})

    def test_table_privileges_limit_storage_to_tools_service(self):
        with db.transaction() as cur:
            for role, expected in (("kern-tools", True), ("kern-proxy", False), ("kern-workspace", False), ("kern-agent-network", False)):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege(%s, 'tool_secrets', %s)", (role, privilege))
                    self.assertEqual(cur.fetchone()[0], expected)
