from __future__ import annotations

import hashlib
import threading
import unittest
from unittest.mock import patch

from host.runtime.admin_api import admin_auth


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        admin_auth._sessions.clear()
        admin_auth._client_failures.clear()
        admin_auth._ADMIN_PASSWORD_HASH = None
        self.addCleanup(admin_auth._sessions.clear)
        self.addCleanup(admin_auth._client_failures.clear)
        self.addCleanup(setattr, admin_auth, "_ADMIN_PASSWORD_HASH", None)

    def test_local_password_login_mints_the_path_bound_session(self) -> None:
        password = "correct horse"
        with (
            patch.object(
                admin_auth,
                "_admin_password_hash",
                return_value=hashlib.sha256(password.encode()).hexdigest(),
            ),
            patch.object(admin_auth.admin_passkeys, "configured") as configured,
        ):
            result = admin_auth.begin_password_login(
                admin_auth.LOCAL_SSH_FORWARD,
                client_key="local:test",
                password_loader=lambda: password,
            )

        configured.assert_not_called()
        self.assertIsNone(result.passkey_options)
        self.assertEqual(len(result.set_cookies), 1)
        cookie = result.set_cookies[0]
        self.assertIn("tc_admin_session=", cookie)
        self.assertNotIn("__Host-", cookie)
        token = admin_auth.parse_session_token(
            cookie,
            context=admin_auth.LOCAL_SSH_FORWARD,
        )
        self.assertIsNotNone(token)
        assert token is not None
        self.assertIsNotNone(admin_auth.validate_session(token))

    def test_public_password_login_defers_session_until_passkey(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        password = "correct horse"
        with (
            patch.object(
                admin_auth,
                "_admin_password_hash",
                return_value=hashlib.sha256(password.encode()).hexdigest(),
            ),
            patch.object(admin_auth.admin_passkeys, "configured", return_value=True),
            patch.object(
                admin_auth.admin_passkeys,
                "begin_login",
                return_value=("preauth", {"challenge": "challenge"}),
            ) as begin_login,
            patch.object(admin_auth, "_create_session") as create_session,
        ):
            result = admin_auth.begin_password_login(
                public,
                client_key="cf4:203.0.113.8",
                password_loader=lambda: password,
            )

        self.assertEqual(result.passkey_options, {"challenge": "challenge"})
        self.assertIn(
            "__Host-tc_admin_passkey_login=preauth",
            result.set_cookies[0],
        )
        create_session.assert_not_called()
        begin_login.assert_called_once_with(
            rp_id="admin.example.com",
            origin="https://admin.example.com",
            client_key="cf4:203.0.113.8",
        )

    def test_passkey_completion_is_the_only_public_session_mint(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        response = {"credential": "response"}
        with patch.object(
            admin_auth.admin_passkeys,
            "finish_login",
        ) as finish_login:
            result = admin_auth.complete_passkey_login(
                public,
                cookie_header="__Host-tc_admin_passkey_login=preauth",
                csrf_header="1",
                client_key_loader=lambda: "cf4:203.0.113.8",
                response_loader=lambda: response,
            )

        finish_login.assert_called_once_with(
            "preauth",
            response,
            client_key="cf4:203.0.113.8",
        )
        self.assertEqual(len(result.set_cookies), 2)
        self.assertIn("__Host-tc_admin_session=", result.set_cookies[0])
        self.assertIn("Secure", result.set_cookies[0])
        self.assertIn(
            "__Host-tc_admin_passkey_login=;",
            result.set_cookies[1],
        )

    def test_failed_passkey_clears_preauth_without_minting_session(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        with (
            patch.object(
                admin_auth.admin_passkeys,
                "finish_login",
                side_effect=admin_auth.admin_passkeys.PasskeyError("bad assertion"),
            ),
            patch.object(admin_auth, "_create_session") as create_session,
            self.assertRaisesRegex(
                admin_auth.PasskeyVerificationError,
                "bad assertion",
            ) as error,
        ):
            admin_auth.complete_passkey_login(
                public,
                cookie_header="__Host-tc_admin_passkey_login=preauth",
                csrf_header="1",
                client_key_loader=lambda: "cf4:203.0.113.8",
                response_loader=dict,
            )

        create_session.assert_not_called()
        self.assertIn(
            "__Host-tc_admin_passkey_login=;",
            error.exception.set_cookies[0],
        )

    def test_blocked_password_source_is_rejected_before_password_comparison(self) -> None:
        with (
            patch.object(admin_auth, "_now", return_value=0.0),
            patch.object(admin_auth, "_admin_password_hash") as password_hash,
        ):
            for _ in range(admin_auth.MAX_FAILURES_PER_CLIENT):
                admin_auth.register_attempt("source")
            with self.assertRaises(admin_auth.LoginRateLimited):
                admin_auth.begin_password_login(
                    admin_auth.LOCAL_SSH_FORWARD,
                    client_key="source",
                    password_loader=lambda: "password",
                )
        password_hash.assert_not_called()

    def test_malformed_login_body_never_charges_the_throttle(self) -> None:
        # The throttle bucket is keyed on the browser's egress IP, so a
        # cross-site page's bodiless POSTs must not consume the operator's
        # attempt budget; only a valid-shaped body may charge it.
        with patch.object(admin_auth, "_now", return_value=0.0):
            for _ in range(admin_auth.MAX_FAILURES_PER_CLIENT + 1):
                with self.assertRaises(admin_auth.InvalidPassword):
                    admin_auth.begin_password_login(
                        admin_auth.LOCAL_SSH_FORWARD,
                        client_key="source",
                        password_loader=lambda: None,
                    )
            self.assertNotIn("source", admin_auth._client_failures)
            # The untouched budget is still available for real attempts.
            self.assertTrue(admin_auth.register_attempt("source"))

    def test_request_context_encodes_the_https_hostname_invariant(self) -> None:
        with self.assertRaises(ValueError):
            admin_auth.RequestAuthContext(admin_auth.AccessPath.PUBLIC_HTTPS)
        with self.assertRaises(ValueError):
            admin_auth.RequestAuthContext(
                admin_auth.AccessPath.SSH_FORWARD,
                "admin.example.com",
            )

    def test_background_validation_does_not_refresh_idle_clock(self) -> None:
        with patch.object(admin_auth, "_now", return_value=1000.0):
            token = admin_auth._create_session()
            self.assertIsNotNone(admin_auth.validate_session(token))
        # A background request just inside the idle window validates...
        with patch.object(
            admin_auth,
            "_now",
            return_value=1000.0 + admin_auth.SESSION_IDLE_TIMEOUT_SECONDS - 1,
        ):
            self.assertIsNotNone(admin_auth.validate_session(token))
        # ...but does not keep an abandoned tab alive past the original clock.
        with patch.object(
            admin_auth,
            "_now",
            return_value=1000.0 + admin_auth.SESSION_IDLE_TIMEOUT_SECONDS + 1,
        ):
            self.assertIsNone(admin_auth.validate_session(token))

    def test_operator_activity_refreshes_idle_clock(self) -> None:
        with patch.object(admin_auth, "_now", return_value=1000.0):
            token = admin_auth._create_session()
        with patch.object(
            admin_auth,
            "_now",
            return_value=1000.0 + admin_auth.SESSION_IDLE_TIMEOUT_SECONDS - 1,
        ):
            self.assertIsNotNone(admin_auth.validate_session(token, refresh_idle=True))
        with patch.object(
            admin_auth,
            "_now",
            return_value=1000.0 + (2 * admin_auth.SESSION_IDLE_TIMEOUT_SECONDS) - 2,
        ):
            self.assertIsNotNone(admin_auth.validate_session(token))

    def test_public_admin_session_lifetimes_are_bounded(self) -> None:
        self.assertEqual(admin_auth.SESSION_IDLE_TIMEOUT_SECONDS, 12 * 60 * 60)
        self.assertEqual(admin_auth.SESSION_ABSOLUTE_TIMEOUT_SECONDS, 3 * 24 * 60 * 60)

    def test_unknown_token_is_not_a_session(self) -> None:
        self.assertIsNone(admin_auth.validate_session("not-a-real-token"))

    def test_idle_timeout_expires_and_drops_session(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            token = admin_auth._create_session()
        with patch.object(admin_auth, "_now", return_value=admin_auth.SESSION_IDLE_TIMEOUT_SECONDS + 1):
            self.assertIsNone(admin_auth.validate_session(token))
        self.assertEqual(len(admin_auth._sessions), 0)

    def test_absolute_timeout_expires_even_when_recently_used(self) -> None:
        now = 10_000_000.0
        with patch.object(admin_auth, "_now", return_value=now):
            token = admin_auth._create_session()
        (session,) = admin_auth._sessions.values()
        session.created_at = now - admin_auth.SESSION_ABSOLUTE_TIMEOUT_SECONDS - 1
        session.last_used_at = now - 1  # active within the idle window
        with patch.object(admin_auth, "_now", return_value=now):
            self.assertIsNone(admin_auth.validate_session(token))

    def test_destroy_session_revokes_it(self) -> None:
        with patch.object(admin_auth, "_now", return_value=0.0):
            token = admin_auth._create_session()
            token_hash = admin_auth.validate_session(token)
            self.assertIsNotNone(token_hash)
            assert token_hash is not None
            admin_auth._destroy_session(token_hash)
            self.assertIsNone(admin_auth.validate_session(token))

    def test_session_cap_evicts_the_oldest(self) -> None:
        with patch.object(admin_auth, "MAX_SESSIONS", 2):
            with patch.object(admin_auth, "_now", return_value=1.0):
                oldest = admin_auth._create_session()
            with patch.object(admin_auth, "_now", return_value=2.0):
                middle = admin_auth._create_session()
            with patch.object(admin_auth, "_now", return_value=3.0):
                newest = admin_auth._create_session()
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

    def test_request_classification_keeps_ssh_recovery_database_independent(self) -> None:
        loader_called = False

        def load_hostname() -> str:
            nonlocal loader_called
            loader_called = True
            raise RuntimeError("database unavailable")

        context = admin_auth.classify_request(
            forwarded_proto_values=[],
            host_values=["127.0.0.1:7443"],
            public_hostname_loader=load_hostname,
        )
        self.assertIs(context, admin_auth.LOCAL_SSH_FORWARD)
        self.assertFalse(loader_called)
        self.assertIsNone(context.passkey_context)

    def test_public_request_classification_is_exact_and_https_only(self) -> None:
        loader = lambda: "admin.example.com"
        context = admin_auth.classify_request(
            forwarded_proto_values=["https"],
            host_values=["admin.example.com:443"],
            public_hostname_loader=loader,
        )
        self.assertTrue(context.is_public_https)
        self.assertEqual(
            context.passkey_context,
            ("admin.example.com", "https://admin.example.com"),
        )

        for proto, hosts in (
            (["https", "https"], ["admin.example.com"]),
            (["https"], ["other.example.com"]),
            (["ftp"], ["admin.example.com"]),
        ):
            with self.subTest(proto=proto, hosts=hosts):
                with self.assertRaises(admin_auth.RequestBoundaryError):
                    admin_auth.classify_request(
                        forwarded_proto_values=proto,
                        host_values=hosts,
                        public_hostname_loader=loader,
                    )

        with self.assertRaises(admin_auth.PublicHttpsRequired) as error:
            admin_auth.classify_request(
                forwarded_proto_values=["http"],
                host_values=["admin.example.com"],
                public_hostname_loader=loader,
            )
        self.assertEqual(error.exception.hostname, "admin.example.com")

    def test_https_only_auth_routes_are_declared_in_one_policy(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        for method, path in admin_auth.HTTPS_ONLY_AUTH_ROUTES:
            with self.subTest(method=method, path=path):
                self.assertFalse(
                    admin_auth.route_is_available(
                        admin_auth.LOCAL_SSH_FORWARD, method, path
                    )
                )
                self.assertTrue(
                    admin_auth.route_is_available(public, method, path)
                )
        self.assertTrue(
            admin_auth.route_is_available(
                admin_auth.LOCAL_SSH_FORWARD, "POST", "/v1/login"
            )
        )
        self.assertFalse(
            admin_auth.route_is_available(
                admin_auth.LOCAL_SSH_FORWARD, "GET", "/v1/admin-passkeys"
            )
        )

    def test_session_authentication_is_bound_to_the_classified_path(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        token = admin_auth._create_session()
        local_cookie = admin_auth._session_cookie(
            token, context=admin_auth.LOCAL_SSH_FORWARD
        )
        public_cookie = admin_auth._session_cookie(token, context=public)

        self.assertEqual(
            admin_auth.authenticate_session_request(
                public,
                cookie_header=public_cookie,
                csrf_header="1",
                activity_header="",
            ),
            admin_auth._hash(token),
        )
        with self.assertRaises(admin_auth.SessionAuthError):
            admin_auth.authenticate_session_request(
                public,
                cookie_header=local_cookie,
                csrf_header="1",
                activity_header="",
            )
        with self.assertRaises(admin_auth.MissingSessionRequestHeader):
            admin_auth.authenticate_session_request(
                admin_auth.LOCAL_SSH_FORWARD,
                cookie_header=local_cookie,
                csrf_header="",
                activity_header="",
            )

    def test_login_source_identity_uses_only_its_classified_path(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        self.assertEqual(
            admin_auth.login_client_key(
                admin_auth.LOCAL_SSH_FORWARD,
                local_address="127.0.0.1",
                cf_connecting_ip_values=["not-used"],
                cf_connecting_ipv6_values=[],
            ),
            "local:127.0.0.1",
        )
        self.assertEqual(
            admin_auth.login_client_key(
                public,
                local_address="127.0.0.1",
                cf_connecting_ip_values=["203.0.113.8"],
                cf_connecting_ipv6_values=[],
            ),
            "cf4:203.0.113.8",
        )
        with self.assertRaises(admin_auth.RequestBoundaryError):
            admin_auth.login_client_key(
                public,
                local_address="127.0.0.1",
                cf_connecting_ip_values=[],
                cf_connecting_ipv6_values=[],
            )

    def test_session_cookie_attributes(self) -> None:
        # Over HTTPS the cookie uses the un-tossable __Host- prefix and Secure.
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        secure = admin_auth._session_cookie("abc", context=public)
        self.assertIn("__Host-tc_admin_session=abc", secure)
        self.assertIn("HttpOnly", secure)
        self.assertIn("SameSite=Strict", secure)
        self.assertIn("Path=/", secure)
        self.assertIn("Secure", secure)
        self.assertNotIn("Domain", secure)
        # The loopback SSH forward is plain HTTP localhost, where __Host- cannot
        # apply and there are no siblings; a plain, non-Secure cookie is used.
        loopback = admin_auth._session_cookie(
            "abc", context=admin_auth.LOCAL_SSH_FORWARD
        )
        self.assertIn("tc_admin_session=abc", loopback)
        self.assertNotIn("__Host-", loopback)
        self.assertNotIn("Secure", loopback)

    def test_clear_cookie_expires_immediately(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        self.assertIn(
            "Max-Age=0",
            admin_auth._clear_session_cookie(context=admin_auth.LOCAL_SSH_FORWARD),
        )
        self.assertIn(
            "__Host-tc_admin_session=;",
            admin_auth._clear_session_cookie(context=public),
        )

    def test_passkey_login_cookie_is_secure_and_not_a_session(self) -> None:
        cookie = admin_auth._passkey_login_cookie("preauth")
        self.assertIn("__Host-tc_admin_passkey_login=preauth", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(
            admin_auth._parse_passkey_login_token(
                "__Host-tc_admin_passkey_login=preauth"
            ),
            "preauth",
        )
        self.assertIsNone(
            admin_auth.parse_session_token(
                "__Host-tc_admin_passkey_login=preauth",
                context=admin_auth.RequestAuthContext(
                    admin_auth.AccessPath.PUBLIC_HTTPS,
                    "admin.example.com",
                ),
            )
        )

    def test_parse_session_token(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        self.assertEqual(
            admin_auth.parse_session_token(
                "a=1; __Host-tc_admin_session=xyz; b=2",
                context=public,
            ),
            "xyz",
        )
        self.assertEqual(
            admin_auth.parse_session_token(
                "a=1; tc_admin_session=xyz; b=2",
                context=admin_auth.LOCAL_SSH_FORWARD,
            ),
            "xyz",
        )
        self.assertIsNone(
            admin_auth.parse_session_token(
                "other=1", context=admin_auth.LOCAL_SSH_FORWARD
            )
        )
        self.assertIsNone(admin_auth.parse_session_token("", context=public))

    def test_cookie_names_are_exclusive_to_their_transport(self) -> None:
        public = admin_auth.RequestAuthContext(
            admin_auth.AccessPath.PUBLIC_HTTPS,
            "admin.example.com",
        )
        # A sibling can toss a plain tc_admin_session on the shared parent
        # domain, but the public HTTPS path ignores it completely.
        self.assertEqual(
            admin_auth.parse_session_token(
                "tc_admin_session=tossed; __Host-tc_admin_session=real",
                context=public,
            ),
            "real",
        )
        self.assertIsNone(
            admin_auth.parse_session_token(
                "tc_admin_session=tossed", context=public
            )
        )
        # Conversely the loopback HTTP path never accepts the public cookie.
        self.assertIsNone(
            admin_auth.parse_session_token(
                "__Host-tc_admin_session=public",
                context=admin_auth.LOCAL_SSH_FORWARD,
            )
        )

    def test_duplicate_expected_cookie_is_rejected(self) -> None:
        self.assertIsNone(
            admin_auth.parse_session_token(
                "tc_admin_session=first; tc_admin_session=second",
                context=admin_auth.LOCAL_SSH_FORWARD,
            )
        )
        self.assertIsNone(
            admin_auth.parse_session_token(
                "__Host-tc_admin_session=first; __Host-tc_admin_session=second",
                context=admin_auth.RequestAuthContext(
                    admin_auth.AccessPath.PUBLIC_HTTPS,
                    "admin.example.com",
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
