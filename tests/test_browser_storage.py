"""Actual Postgres transactions, encryption and Browser snapshot limits."""
from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.browser.accounts import Accounts
from host.runtime.browser.storage import Store
from host.runtime.browser.client import BrowserError
from host.runtime.core import db, secretbox
from test_browser_sessions import FakeBrowser

KEY = "acct_" + "a" * 32
DATA = {"provider_identifier": "example", "state": "connected", "checked_at": "2026-09-29T00:00:00Z", "usage": {}}
AUTH = {"cookies": [{"name": "session", "value": "private-cookie"}], "origins": [{"origin": "https://x.com", "localStorage": [{"name": "secret", "value": "private-local"}], "indexedDB": []}]}
DECODO = {"mode": "decodo", "username": "example", "password": "private-proxy", "location": "new_york", "session": "a" * 24}


class BrowserStorageTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        self.store = Store()

    def test_encrypted_settings_and_auth_round_trip_and_direct_erases_credentials(self):
        self.store.save_settings(DECODO)
        self.store.save_account(KEY, "x", DATA, AUTH)
        with db.transaction() as cur:
            cur.execute("SELECT password_ciphertext FROM browser_settings")
            password = cur.fetchone()[0]
            cur.execute("SELECT auth_ciphertext FROM browser_accounts")
            auth = cur.fetchone()[0]
        self.assertTrue(password.startswith("enc:v1:"))
        self.assertTrue(auth.startswith("enc:v1:"))
        for secret in ("private-proxy", "private-cookie", "private-local"):
            self.assertNotIn(secret, password + auth)
        self.assertEqual(Store().load_settings(), DECODO)
        self.assertEqual(Store().auth(KEY), AUTH)
        self.assertEqual(Store().accounts(), {KEY: ("x", DATA)})
        self.store.save_settings({"mode": "direct"})
        with db.transaction() as cur:
            cur.execute("SELECT username, password_ciphertext, location, session FROM browser_settings")
            self.assertEqual(cur.fetchone(), (None, None, None, None))
        self.store.delete_account(KEY)
        self.assertEqual(self.store.accounts(), {})
        with self.assertRaises(BrowserError):
            self.store.auth(KEY)

    def test_linkedin_identity_constraints_usage_and_shared_capacity(self):
        owner = "https://www.linkedin.com/in/owner/"
        data = {**deepcopy(DATA), "provider_identifier": owner,
                "usage": {"linkedin_send_dm": {"day": "2026-10-08", "count": 50}}}
        self.store.save_account(KEY, "linkedin", data, AUTH)
        self.assertEqual(Store().accounts()[KEY], ("linkedin", data))
        self.assertEqual(Store().auth(KEY), AUTH)
        for provider, identifier in (("x", owner), ("linkedin", "example"),
                                     ("linkedin", "https://evil.test/in/owner/"),
                                     ("linkedin", owner + "?secret=value")):
            with self.subTest(provider=provider, identifier=identifier), self.assertRaises(Exception):
                self.store.save_account(KEY, provider, {**data, "provider_identifier": identifier}, AUTH)
        self.assertEqual(Store().accounts()[KEY], ("linkedin", data))
        for i in range(1, 5):
            self.store.save_account("acct_" + f"{i:032x}", "x", DATA, AUTH)
        with self.assertRaisesRegex(BrowserError, "5 browser accounts"):
            self.store.save_account("acct_" + "f" * 32, "linkedin", data, AUTH)

    def test_shared_budget_rollback_and_replacement_reclaims_old_snapshot(self):
        second = "acct_" + "b" * 32
        self.store.save_account(KEY, "x", DATA, AUTH)
        self.store.save_account(second, "x", DATA, AUTH)
        with db.transaction() as cur:
            cur.execute("SELECT sum(octet_length(auth_ciphertext)) FROM browser_accounts")
            size = int(cur.fetchone()[0])
        changed = {**AUTH, "extra": "large" * 100}
        with patch("host.runtime.browser.storage.SAVED_STATE_LIMIT_BYTES", size + 128):
            with self.assertRaisesRegex(BrowserError, "1 GB total"):
                self.store.save_account(KEY, "x", {**DATA, "state": "needs_attention"}, changed)
            self.assertEqual(self.store.auth(KEY), AUTH)
            self.assertEqual(self.store.accounts()[KEY][1], DATA)
            self.store.save_account(KEY, "x", DATA, AUTH)
        self.store.delete_account(second)
        self.store.save_account(KEY, "x", DATA, changed)
        self.assertEqual(self.store.auth(KEY), changed)

    def test_size_and_ciphertext_budget_reject_before_encryption(self):
        self.store.save_account(KEY, "x", DATA, AUTH)
        with patch("host.runtime.browser.storage.MAX_SNAPSHOT_BYTES", 64), patch.object(secretbox, "encrypt") as encrypt:
            with self.assertRaisesRegex(BrowserError, "64 MiB"):
                self.store.save_account(KEY, "x", DATA, {"value": "x" * 65})
            encrypt.assert_not_called()
        # Base64/header expansion must be reserved even when raw JSON fits.
        with patch("host.runtime.browser.storage.SAVED_STATE_LIMIT_BYTES", len(json.dumps(AUTH)) + 1), patch.object(secretbox, "encrypt") as encrypt:
            with self.assertRaisesRegex(BrowserError, "1 GB total"):
                self.store.save_account(KEY, "x", DATA, AUTH)
            encrypt.assert_not_called()
        self.assertEqual(self.store.auth(KEY), AUTH)

    def test_oversized_stored_ciphertext_is_not_fetched_or_decrypted(self):
        self.store.save_account(KEY, "x", DATA, AUTH)
        with patch("host.runtime.browser.storage.MAX_SNAPSHOT_BYTES", 1), patch.object(secretbox, "decrypt") as decrypt:
            with self.assertRaisesRegex(BrowserError, "64 MiB"):
                self.store.auth(KEY)
            decrypt.assert_not_called()

    def test_failed_transaction_keeps_metadata_auth_and_settings(self):
        self.store.save_account(KEY, "x", DATA, AUTH)
        self.store.save_settings(DECODO)
        with self.assertRaises(Exception):
            self.store.save_account(KEY, "x", {**DATA, "state": "invalid"}, {"cookies": []})
        with self.assertRaises(Exception):
            self.store.save_settings({**DECODO, "location": "invalid"})
        self.assertEqual(self.store.auth(KEY), AUTH)
        self.assertEqual(self.store.accounts()[KEY][1], DATA)
        self.assertEqual(self.store.load_settings(), DECODO)
        with patch.object(secretbox, "encrypt", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                self.store.save_settings({**DECODO, "password": "new"})
        self.assertEqual(self.store.load_settings(), DECODO)

    def test_five_account_limit_and_usage_survive_restart(self):
        data = deepcopy(DATA)
        data["usage"] = {"x_post_tweet": {"day": "2026-09-29", "count": 50}}
        keys = ["acct_" + f"{i:032x}" for i in range(6)]
        for key in keys[:5]:
            self.store.save_account(key, "x", data, AUTH)
        with self.assertRaisesRegex(BrowserError, "5 browser accounts"):
            self.store.save_account(keys[5], "x", data, AUTH)
        self.assertEqual(Store().accounts()[keys[0]][1]["usage"], data["usage"])
        self.store.delete_account(keys[0])
        self.store.save_account(keys[5], "x", DATA, AUTH)
        self.assertEqual(self.store.accounts()[keys[5]][1]["usage"], {})

    def test_browser_role_is_scoped_and_agent_tools_cannot_read_browser_secrets(self):
        self.store.save_account(KEY, "x", DATA, AUTH)
        with db.transaction() as cur:
            cur.execute('SET LOCAL ROLE "kern-browser"')
            cur.execute("SELECT account_id FROM browser_accounts")
            self.assertEqual(cur.fetchone(), (KEY,))
            cur.execute("SELECT has_table_privilege('browser_accounts', 'UPDATE'), has_table_privilege('browser_settings', 'SELECT'), has_table_privilege('secret_keys', 'SELECT'), has_table_privilege('tool_credentials', 'SELECT'), has_table_privilege('provider_accounts', 'SELECT')")
            self.assertEqual(cur.fetchone(), (True, True, True, False, False))
        for role in ("kern-tools", "kern-workspace", "kern-proxy"):
            with db.transaction() as cur:
                cur.execute("SELECT has_table_privilege(%s, 'browser_accounts', 'SELECT'), has_table_privilege(%s, 'browser_settings', 'SELECT')", (role, role))
                self.assertEqual(cur.fetchone(), (False, False))

    def test_usage_is_committed_before_submit_and_database_failure_prevents_submit(self):
        from host.runtime.browser.actions.x_post_tweet import execute
        from host.runtime.browser.accounts import Profile
        self.store.save_account(KEY, "x", DATA, AUTH)
        profile = Profile(KEY, "x", FakeBrowser, self.store, deepcopy(DATA))
        def submit(*args):
            self.assertEqual(Store().accounts()[KEY][1]["usage"]["x_post_tweet"]["count"], 1)
            return "https://x.com/example/status/123"
        with patch("host.runtime.browser.actions.x_post_tweet.prepare_post"), patch("host.runtime.browser.actions.x_post_tweet.submit_prepared_post", side_effect=submit) as send:
            execute(profile, {"provider_identifier": "example", "text": "hello"})
            self.assertEqual(send.call_count, 1)
            with patch.object(self.store, "save_account", side_effect=OSError("unavailable")), patch("host.runtime.browser.actions.x_post_tweet._report_failure"):
                with self.assertRaises(OSError):
                    execute(profile, {"provider_identifier": "example", "text": "hello"})
            self.assertEqual(send.call_count, 1)


class BrowserSnapshotBoundaryTests(unittest.TestCase):
    def test_oversized_source_strings_are_rejected_before_json_encoding(self):
        with patch("host.runtime.browser.storage.MAX_SNAPSHOT_BYTES", 64), patch.object(json.JSONEncoder, "iterencode") as encode:
            with self.assertRaisesRegex(BrowserError, "64 MiB"):
                Store().save_account(KEY, "x", DATA, {"value": "x" * 65})
            encode.assert_not_called()

    def test_snapshot_accumulation_is_bounded_before_database_or_encryption(self):
        with patch("host.runtime.browser.storage.MAX_SNAPSHOT_BYTES", 64), patch.object(db, "transaction") as transaction, patch.object(secretbox, "encrypt") as encrypt:
            with self.assertRaisesRegex(BrowserError, "64 MiB"):
                Store().save_account(KEY, "x", DATA, {"values": ["\x00" * 5] * 3})
            transaction.assert_not_called()
            encrypt.assert_not_called()

    def test_unicode_and_control_expansion_is_rejected_before_encoding(self):
        for value in ('é' * 12, '😀' * 6, '\x00' * 12, '\\' * 33):
            with self.subTest(value=value), patch('host.runtime.browser.storage.MAX_SNAPSHOT_BYTES', 64), patch.object(json.JSONEncoder, 'iterencode') as encode:
                with self.assertRaisesRegex(BrowserError, '64 MiB'):
                    Store().save_account(KEY, 'x', DATA, {'value': value})
                encode.assert_not_called()

    def test_many_small_records_do_not_retain_all_encoder_fragments(self):
        import tracemalloc
        auth = {'values': list(range(200000))}
        serialized_size = len(json.dumps(auth))
        tracemalloc.start()
        try:
            with patch.object(db, 'transaction', side_effect=OSError('serialization complete')):
                with self.assertRaisesRegex(OSError, 'serialization complete'):
                    Store().save_account(KEY, 'x', DATA, auth)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Bound transient serialization storage relative to the JSON, rather
        # than one Python string and pointer for every encoder fragment.
        self.assertLess(peak, 4 * serialized_size + 1024 * 1024)
