from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from host.runtime.admin_api import admin_auth


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        admin_auth._sessions.clear()
        admin_auth._client_failures.clear()
        self.addCleanup(admin_auth._sessions.clear)
        self.addCleanup(admin_auth._client_failures.clear)

    def test_create_and_validate_session_refreshes_idle_clock(self) -> None:
        with patch.object(admin_auth, "_now", return_value=1000.0):
            token = admin_auth.create_session()
            self.assertIsNotNone(admin_auth.validate_session(token))
        # A use just inside the idle window keeps the session alive.
        with patch.object(admin_auth, "_now", return_value=1000.0 + admin_auth.SESSION_IDLE_TIMEOUT_SECONDS - 1):
            self.assertIsNotNone(admin_auth.validate_session(token))

    def test_unknown_token_is_not_a_session(self) -> None:
        self.assertIsNone(admin_auth.validate_session("not-a-real-token"))

    def test_idle_timeout_expires_and_drops_session(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            token = admin_auth.create_session()
        with patch.object(admin_auth, "_now", return_value=admin_auth.SESSION_IDLE_TIMEOUT_SECONDS + 1):
            self.assertIsNone(admin_auth.validate_session(token))
        self.assertEqual(len(admin_auth._sessions), 0)

    def test_absolute_timeout_expires_even_when_recently_used(self) -> None:
        now = 10_000_000.0
        with patch.object(admin_auth, "_now", return_value=now):
            token = admin_auth.create_session()
        (session,) = admin_auth._sessions.values()
        session.created_at = now - admin_auth.SESSION_ABSOLUTE_TIMEOUT_SECONDS - 1
        session.last_used_at = now - 1  # active within the idle window
        with patch.object(admin_auth, "_now", return_value=now):
            self.assertIsNone(admin_auth.validate_session(token))

    def test_destroy_session_revokes_it(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            token = admin_auth.create_session()
            token_hash = admin_auth.validate_session(token)
            self.assertIsNotNone(token_hash)
            assert token_hash is not None
            admin_auth.destroy_session(token_hash)
            self.assertIsNone(admin_auth.validate_session(token))

    def test_session_cap_evicts_the_oldest(self) -> None:
        with patch.object(admin_auth, "MAX_SESSIONS", 2):
            with patch.object(admin_auth, "_now", return_value=1.0):
                oldest = admin_auth.create_session()
            with patch.object(admin_auth, "_now", return_value=2.0):
                middle = admin_auth.create_session()
            with patch.object(admin_auth, "_now", return_value=3.0):
                newest = admin_auth.create_session()
            with patch.object(admin_auth, "_now", return_value=3.0):
                self.assertIsNone(admin_auth.validate_session(oldest))
                self.assertIsNotNone(admin_auth.validate_session(middle))
                self.assertIsNotNone(admin_auth.validate_session(newest))

    def test_a_source_is_blocked_only_past_the_limit(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            # Up to the limit, attempts are allowed to proceed (True); only the
            # excess is blocked (False), at which point even a correct password
            # would be refused.
            for _ in range(admin_auth.MAX_FAILURES_PER_CLIENT):
                self.assertTrue(admin_auth.register_attempt("1.2.3.4"))
            self.assertFalse(admin_auth.register_attempt("1.2.3.4"))
            # A different source is unaffected by another's attempts.
            self.assertTrue(admin_auth.register_attempt("9.9.9.9"))

    def test_a_block_clears_after_the_window(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            for _ in range(admin_auth.MAX_FAILURES_PER_CLIENT):
                admin_auth.register_attempt("1.2.3.4")
            self.assertFalse(admin_auth.register_attempt("1.2.3.4"))
        with patch.object(admin_auth, "_now", return_value=admin_auth.FAILURE_WINDOW_SECONDS + 1):
            self.assertTrue(admin_auth.register_attempt("1.2.3.4"))

    def test_success_clears_a_sources_streak(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            for _ in range(admin_auth.MAX_FAILURES_PER_CLIENT):
                admin_auth.register_attempt("1.2.3.4")
            # At the limit the next attempt would be blocked...
            self.assertFalse(admin_auth.register_attempt("1.2.3.4"))
            admin_auth.record_success("1.2.3.4")
            # ...but a correct login reset the streak, so it may proceed again.
            self.assertTrue(admin_auth.register_attempt("1.2.3.4"))

    def test_concurrent_attempts_never_exceed_the_limit(self) -> None:
        # The count is consulted and incremented under the lock, so a burst of
        # simultaneous attempts from one source can never win more than the
        # per-window budget of guesses (no check-then-act bypass).
        allowed: list[bool] = []
        guard = threading.Lock()

        def worker() -> None:
            ok = admin_auth.register_attempt("5.5.5.5")
            with guard:
                allowed.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(allowed), admin_auth.MAX_FAILURES_PER_CLIENT)

    def test_throttle_key_buckets_ipv4_per_address_and_ipv6_per_64(self) -> None:
        self.assertEqual(admin_auth.throttle_key_from_ip("203.0.113.7"), "cf4:203.0.113.7")
        # Two addresses in the same /64 canonicalize to one bucket.
        low = admin_auth.throttle_key_from_ip("2001:db8:1:2:3:4:5:6")
        high = admin_auth.throttle_key_from_ip("2001:db8:1:2:ffff:ffff:ffff:ffff")
        self.assertEqual(low, high)
        self.assertEqual(low, "cf6:2001:db8:1:2::")
        # A different /64 is a different bucket.
        self.assertNotEqual(low, admin_auth.throttle_key_from_ip("2001:db8:1:3::1"))

    def test_throttle_key_rejects_an_invalid_address(self) -> None:
        with self.assertRaises(ValueError):
            admin_auth.throttle_key_from_ip("not-an-ip")

    def test_session_cookie_attributes(self) -> None:
        # Over HTTPS the cookie uses the un-tossable __Host- prefix and Secure.
        secure = admin_auth.session_cookie("abc", secure=True)
        self.assertIn("__Host-tc_admin_session=abc", secure)
        self.assertIn("HttpOnly", secure)
        self.assertIn("SameSite=Strict", secure)
        self.assertIn("Path=/", secure)
        self.assertIn("Secure", secure)
        self.assertNotIn("Domain", secure)
        # The loopback SSH forward is plain HTTP localhost, where __Host- cannot
        # apply and there are no siblings; a plain, non-Secure cookie is used.
        loopback = admin_auth.session_cookie("abc", secure=False)
        self.assertIn("tc_admin_session=abc", loopback)
        self.assertNotIn("__Host-", loopback)
        self.assertNotIn("Secure", loopback)

    def test_clear_cookie_expires_immediately(self) -> None:
        self.assertIn("Max-Age=0", admin_auth.clear_session_cookie(secure=False))
        self.assertIn("__Host-tc_admin_session=;", admin_auth.clear_session_cookie(secure=True))

    def test_parse_session_token(self) -> None:
        self.assertEqual(admin_auth.parse_session_token("a=1; __Host-tc_admin_session=xyz; b=2"), "xyz")
        self.assertEqual(admin_auth.parse_session_token("a=1; tc_admin_session=xyz; b=2"), "xyz")
        self.assertIsNone(admin_auth.parse_session_token("other=1"))
        self.assertIsNone(admin_auth.parse_session_token(""))

    def test_parse_prefers_host_cookie_over_a_tossed_plain_cookie(self) -> None:
        # A sibling can toss a plain tc_admin_session on the shared parent domain,
        # sent first by a more specific path, but never a __Host- cookie. Parsing
        # prefers the __Host- value, so the tossed cookie cannot break the session.
        self.assertEqual(
            admin_auth.parse_session_token("tc_admin_session=tossed; __Host-tc_admin_session=real"),
            "real",
        )


if __name__ == "__main__":
    unittest.main()
