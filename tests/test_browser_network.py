"""Browser proxy boundary, private settings and fail-closed routing, offline."""
from __future__ import annotations

import base64
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from browser_fakes import MemoryStore
from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.config import LOCATIONS, Settings
from host.runtime.browser_network.relay import Network, TunnelHandler, TunnelServer
from host.runtime.browser_network.transport import connect_proxy, proxy_details, target
from host.runtime.browser.accounts import Accounts
from host.runtime.browser.service import authorized

DECODO = {"mode": "decodo", "username": "example", "password": "private-password", "location": "new_york"}


class BrowserNetworkTests(unittest.TestCase):
    def setUp(self):
        self.warning = self.enterContext(patch("host.runtime.browser_network.transport.host_errors.report_warning"))
        self.store = MemoryStore()
        self.network = Network(self.store)
        self.addCleanup(self.network.disconnect)

    def test_secret_persistence_redaction_and_mode_replacement(self):
        result = self.network.dispatch("save", DECODO)
        self.assertNotIn("password", result)
        self.assertTrue(result["has_password"])
        settings = Settings(self.store)
        self.assertEqual({k: v for k, v in settings.value.items() if k != "session"}, DECODO)
        self.network.dispatch("save", {**DECODO, "password": ""})
        self.assertEqual({k: v for k, v in self.network.settings.value.items() if k != "session"}, DECODO)
        self.network.dispatch("save", {"mode": "direct"})
        self.assertEqual(Settings(self.store).value, {"mode": "direct"})
        self.assertNotIn("private-password", str(self.store.settings))

    def test_invalid_settings_leave_old_config_unchanged(self):
        self.network.dispatch("save", DECODO)
        for body in ({"mode": "unknown"}, {**DECODO, "server": "http://localhost"},
                     {**DECODO, "country": "usa"},
                     {**DECODO, "username": "user-example-session-kern1"},
                     {**DECODO, "password": "bad\r\nheader"},
                     {**DECODO, "city": "new-york-session-other"},
                     {**DECODO, "timezone": "Not/A_Zone"},
                     *({**DECODO, "location": location} for location in ("", "unknown", "new_york-session-other", None, [])),
                     {**DECODO, "locale": "en-US,secret"},
                     {**DECODO, "session": "injected"},
                     {"mode": "tailscale"}):
            with self.subTest(body=body), self.assertRaises(BrowserError):
                self.network.dispatch("save", body)
            self.assertEqual({k: v for k, v in self.network.settings.value.items() if k != "session"}, DECODO)

    def test_location_presets_match_proxy_target_and_browser_identity(self):
        expected = {
            "new_york": ("us", "new_york", "en-US", "America/New_York"),
            "london": ("gb", "london", "en-GB", "Europe/London"),
        }
        self.assertEqual(set(LOCATIONS), set(expected))
        for location, fields in expected.items():
            with self.subTest(location=location):
                public = self.network.dispatch("save", {**DECODO, "location": location})
                self.assertEqual(tuple(public[key] for key in ("country", "city", "locale", "timezone")), fields)
                self.assertIn(f"-country-{fields[0]}-city-{fields[1]}-", self.network.settings.proxy_username())
                self.assertEqual(self.network.settings.value["location"], location)
                self.assertNotIn("timezone", self.network.settings.value)

    def test_unavailable_settings_block_routing_and_recover_without_restart(self):
        with patch.object(self.store, "load_settings", side_effect=OSError("unavailable")):
            recovered = Network(self.store)
            self.assertIn("error", recovered.dispatch("get", {}))
            with patch("socket.create_connection") as dial:
                with self.assertRaises(BrowserError):
                    recovered.dial("example.com:443")
                dial.assert_not_called()
        self.assertEqual(recovered.dispatch("get", {})["mode"], "direct")

    def test_direct_delegates_dns_and_address_selection_to_socket(self):
        upstream = Mock()
        with patch("socket.create_connection", return_value=upstream) as connect:
            self.assertIs(self.network.dial("example.com:443"), upstream)
        connect.assert_called_once_with(("example.com", 443), timeout=10)

    def test_atomic_write_failure_preserves_old_settings(self):
        self.network.dispatch("save", DECODO)
        with patch.object(self.store, "save_settings", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.network.dispatch("save", {"mode": "direct"})
        self.assertEqual({k: v for k, v in Settings(self.store).value.items() if k != "session"}, DECODO)

    def test_target_validates_https_authority_without_dns(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("No DNS preflight")):
            self.assertEqual(target("example.com:443"), "example.com")
            self.assertEqual(target("[2606:4700::1111]:443"), "2606:4700::1111")
        for authority in ("example.com:80", "user:pass@example.com:443", "example.com:443/path", "example.com:443?x=1", "example.com:443\r\nX:yes"):
            with self.subTest(authority=authority), self.assertRaises(BrowserError):
                target(authority)

    def test_decodo_preserves_location_and_session_without_fallback(self):
        self.network.dispatch("save", DECODO)
        upstream = Mock()
        with patch("socket.getaddrinfo", side_effect=AssertionError("Destination DNS must be remote")), patch("host.runtime.browser_network.relay.connect_proxy", return_value=upstream) as proxy:
            self.assertIs(self.network.dial("example.com:443"), upstream)
            proxy.assert_called_once_with(("gate.decodo.com", 7000), "example.com", (self.network.settings.proxy_username(), DECODO["password"]))
        self.network.release(upstream)
        with patch("socket.getaddrinfo", side_effect=AssertionError("Destination DNS must be remote")), patch("host.runtime.browser_network.relay.connect_proxy", side_effect=BrowserError("rejected")), patch("socket.create_connection") as direct:
            with self.assertRaises(BrowserError):
                self.network.dial("example.com:443")
            direct.assert_not_called()

    def test_sticky_identity_survives_restart_and_changes_only_with_route(self):
        self.network.dispatch("save", DECODO)
        original = self.network.settings.proxy_username()
        self.assertRegex(original, r"^user-example-country-us-city-new_york-session-[a-f0-9]{24}-sessionduration-1440$")
        self.assertEqual(Settings(self.store).proxy_username(), original)
        self.network.dispatch("save", {**DECODO, "password": ""})
        self.assertEqual(self.network.settings.proxy_username(), original)
        self.network.dispatch("save", {**DECODO, "location": "london"})
        self.assertNotEqual(self.network.settings.proxy_username(), original)
        self.assertNotIn("session", self.network.settings.public())
        with self.assertRaises(BrowserError):
            self.network.dispatch("save", {**DECODO, "username": "another", "password": ""})

    def test_decodo_failure_does_not_retry_or_fall_back_to_direct(self):
        self.network.dispatch("save", DECODO)
        with patch("host.runtime.browser_network.relay.connect_proxy", side_effect=BrowserError("failed")) as proxy, patch("socket.create_connection") as direct:
            with self.assertRaises(BrowserError):
                self.network.dial("example.com:443")
            proxy.assert_called_once()
            direct.assert_not_called()
            self.assertFalse(self.network.connections)

    def test_test_uses_local_relay_without_credentials_and_has_total_deadline(self):
        self.network.dispatch("save", DECODO)
        with patch("subprocess.run", return_value=Mock(returncode=0, stdout=b"1.1.1.1\n")) as run:
            self.assertEqual(self.network.test(), {"mode": "decodo", "ip": "1.1.1.1"})
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/curl", "--disable"])
        self.assertEqual(command[command.index("--proxy") + 1], "http://127.0.0.1:7447")
        self.assertEqual(command[command.index("--noproxy") + 1], "")
        self.assertEqual(command[command.index("--max-time") + 1], "30")
        self.assertEqual(run.call_args.kwargs["timeout"], 35)
        self.assertNotIn(DECODO["password"], str(run.call_args))
        self.assertNotIn(self.network.settings.proxy_username(), str(run.call_args))

    def test_test_timeouts_provider_errors_and_bad_responses_are_redacted(self):
        for code in (28, 22, 60):
            with self.subTest(code=code), patch("subprocess.run", return_value=Mock(returncode=code, stdout=b"secret body", stderr=b"secret error")):
                with self.assertRaises(BrowserError) as caught:
                    self.network.test()
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(self.warning.call_args.kwargs["context"]["curl_exit"], code)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 35, output=b"secret")):
            with self.assertRaises(BrowserError):
                self.network.test()
        for response in (b"private-password", b"127.0.0.1", b"\xff"):
            with patch("subprocess.run", return_value=Mock(returncode=0, stdout=response)):
                with self.assertRaises(BrowserError):
                    self.network.test()
        self.assertNotIn("secret", str(self.warning.call_args_list))
        self.assertNotIn("private-password", str(self.warning.call_args_list))

    def test_real_curl_deadline_returns_while_relay_dns_is_stuck(self):
        entered, resume = threading.Event(), threading.Event()
        run = subprocess.run
        def short_test(command, **kwargs):
            # Exercise curl's actual deadline without taking 30 seconds in CI.
            command[command.index("--max-time") + 1] = "0.2"
            return run(command, **kwargs)
        def stuck_connection(*args, **kwargs):
            entered.set()
            resume.wait(3)
            raise socket.gaierror("private resolver error")
        with TunnelServer(self.network, port=8010) as relay:
            worker = threading.Thread(target=relay.serve_forever)
            worker.start()
            try:
                with patch("socket.create_connection", side_effect=stuck_connection), patch("subprocess.run", side_effect=short_test), patch("host.runtime.browser_network.relay.BROWSER_NETWORK_PORT", 8010):
                    started = time.monotonic()
                    with self.assertRaisesRegex(BrowserError, "timed out"):
                        self.network.test()
                    self.assertTrue(entered.is_set())
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(self.warning.call_args.kwargs["context"]["curl_exit"], 28)
            finally:
                resume.set()
                relay.shutdown()
                worker.join(3)

    def test_connection_diagnostics_identify_stage_without_raw_errors(self):
        for mode, stage in (("direct", "direct_connection"), ("decodo", "proxy_connection")):
            self.network.dispatch("save", DECODO if mode == "decodo" else {"mode": "direct"})
            with patch("socket.create_connection", side_effect=OSError(111, "private-password")):
                with self.assertRaises(BrowserError) as caught:
                    self.network.dial("x.com:443")
            context = self.warning.call_args.kwargs["context"]
            self.assertEqual(context["stage"], stage)
            self.assertEqual(context["errno"], 111)
            self.assertEqual(context["host"], "x.com")
            self.assertNotIn("private-password", str(caught.exception))
        self.assertNotIn("private-password", str(self.warning.call_args_list))

    def test_slow_dial_does_not_block_other_resources_or_settings_changes(self):
        from concurrent.futures import ThreadPoolExecutor
        for mode in ("direct", "decodo"):
            with self.subTest(mode=mode):
                self.network.dispatch("save", DECODO if mode == "decodo" else {"mode": "direct"})
                entered, resume = threading.Event(), threading.Event()
                slow, fast = Mock(), Mock()
                def connect(*args, **kwargs):
                    address = args[1] if mode == "decodo" else args[0][0]
                    if address == "1.1.1.1":
                        entered.set()
                        if not resume.wait(5):
                            raise TimeoutError("fixture was not released")
                        return slow
                    return fast
                connector = "host.runtime.browser_network.relay.connect_proxy" if mode == "decodo" else "socket.create_connection"
                with patch(connector, side_effect=connect), ThreadPoolExecutor(max_workers=3) as pool:
                    pending = pool.submit(self.network.dial, "1.1.1.1:443")
                    try:
                        self.assertTrue(entered.wait(2))
                        other = pool.submit(self.network.dial, "1.0.0.1:443")
                        self.assertIs(other.result(timeout=2), fast)
                        change = pool.submit(self.network.dispatch, "save", {"mode": "direct"})
                        change.result(timeout=2)
                    finally:
                        resume.set()
                    with self.assertRaisesRegex(BrowserError, "changed during connection"):
                        pending.result(timeout=2)
                fast.close.assert_called_once()
                slow.close.assert_called_once()
                self.assertFalse(self.network.connections)

    def test_changing_mode_closes_existing_tunnels(self):
        self.network.dispatch("save", DECODO)
        upstream = Mock()
        self.network.connections.add(upstream)
        self.network.dispatch("save", {"mode": "direct"})
        upstream.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        upstream.close.assert_called_once()
        self.assertEqual(self.network.connections, set())

    def test_connect_handshake_keeps_proxy_auth_out_of_tunnel_and_sends_hostname(self):
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        received = []
        def provider():
            request = bytearray()
            while not request.endswith(b"\r\n\r\n"):
                request.extend(peer.recv(1))
            received.append(bytes(request))
            peer.sendall(b"HTTP/1.1 200 OK\r\n\r\nTLS-FIXTURE")
        worker = threading.Thread(target=provider)
        worker.start()
        with patch("socket.create_connection", return_value=client), patch("host.runtime.browser_network.transport.ssl.create_default_context") as tls:
            tls.return_value.wrap_socket.return_value = client
            stream = connect_proxy(("gate.decodo.com", 7000), "example.com", ("username", "password"))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(received[0].startswith(b"CONNECT example.com:443 HTTP/1.1\r\n"))
        self.assertIn(base64.b64encode(b"username:password"), received[0])
        self.assertEqual(stream.recv(11), b"TLS-FIXTURE")

    def test_provider_error_records_headers_but_not_body_and_redacts_credentials(self):
        client, peer = socket.socketpair()
        self.addCleanup(peer.close)
        peer.sendall(b"HTTP/1.1 407 secret-provider-message\r\nSecret: private\r\n\r\nprivate-body")
        with patch("socket.create_connection", return_value=client), patch("host.runtime.browser_network.transport.ssl.create_default_context") as tls:
            tls.return_value.wrap_socket.return_value = client
            with self.assertRaises(BrowserError) as error:
                connect_proxy(("gate.decodo.com", 7000), "93.184.215.14", ("user", "secret"))
        self.assertIn("407", str(error.exception))
        context = self.warning.call_args.kwargs["context"]
        self.assertEqual(context["stage"], "proxy_connect")
        self.assertEqual(context["proxy_status"], 407)
        self.assertIn("[redacted]-provider-message", context["proxy_status_line"])
        self.assertEqual(context["proxy_header_1"], "Secret: private")
        self.assertEqual(context["gateway"], "gate.decodo.com:7000")
        self.assertGreaterEqual(context["elapsed_ms"], 0)
        self.assertNotIn("private-body", str(self.warning.call_args))
        self.assertNotIn("secret", str(self.warning.call_args))
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(client.fileno(), -1)

    def test_decodo_reason_reaches_diagnostics_and_real_curl_test(self):
        self.network.dispatch("save", DECODO)
        client, peer = socket.socketpair()
        self.addCleanup(peer.close)
        reason = "Access denied. You've reached your current traffic limit."
        peer.sendall(("HTTP/1.1 407 Proxy Authentication Required\r\n"
                      f"X-Error-Message: {reason}\r\nSecret: private\r\n\r\nprivate-body").encode())
        with TunnelServer(self.network, port=8010) as relay:
            worker = threading.Thread(target=relay.serve_forever)
            worker.start()
            try:
                with patch("socket.create_connection", return_value=client), patch("host.runtime.browser_network.transport.ssl.create_default_context") as tls, patch("host.runtime.browser_network.relay.BROWSER_NETWORK_PORT", 8010):
                    tls.return_value.wrap_socket.return_value = client
                    with self.assertRaises(BrowserError) as caught:
                        self.network.test()
                self.assertIn(reason, str(caught.exception))
                self.assertIn("407", str(caught.exception))
                contexts = [call.kwargs["context"] for call in self.warning.call_args_list]
                self.assertTrue(any(context.get("proxy_error") == reason and context.get("proxy_status") == 407 for context in contexts))
                self.assertIn("curl_exit", contexts[-1])
                self.assertNotIn("private-body", str(self.warning.call_args_list))
            finally:
                relay.shutdown()
                worker.join(3)
        self.assertEqual(client.fileno(), -1)

    def test_proxy_reason_redacts_credentials_before_clipping_and_removes_controls(self):
        username = "user-fixture-country-gb-city-london-session-abcdef123456-sessionduration-1440"
        password = "long-secret-" + "z" * 600
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        response = (f"HTTP/1.1 407 Rejected\r\nx-error-message: rejected {username} {password} {encoded} "
                    "fixture abcdef123456\t\x1b " + "more " * 200 + "\r\n\r\n").encode()
        details = proxy_details(response, (username, password))
        reason = details["proxy_error"]
        for secret in (username, password, encoded, "fixture", "abcdef123456", "long-secret", "zzz"):
            self.assertNotIn(secret, str(details))
        self.assertIn("[redacted]", reason)
        self.assertEqual(len(reason), 512)
        self.assertTrue(all(" " <= c <= "~" for c in reason))

    def test_test_ignores_website_error_header_after_successful_connect(self):
        headers = (b"HTTP/1.0 200 Connection established\r\n\r\n"
                   b"HTTP/1.1 502 Bad Gateway\r\nX-Kern-Browser-Error: private website text\r\n\r\n")
        with patch("subprocess.run", return_value=Mock(returncode=22, stdout=b"", stderr=headers)):
            with self.assertRaises(BrowserError) as caught:
                self.network.test()
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("private", str(self.warning.call_args_list))

    def test_partial_proxy_header_does_not_log_a_truncated_credential(self):
        details = proxy_details(b"HTTP/1.1 407 Rejected\r\nRequest-Id: abc123\r\nEcho: private-pass",
                                ("username", "private-password"))
        self.assertFalse(details["proxy_headers_complete"])
        self.assertEqual(details["proxy_header_1"], "Request-Id: abc123")
        self.assertNotIn("private-pass", str(details))

    def test_provider_issued_credentials_are_redacted_but_challenges_and_ids_remain(self):
        details = proxy_details(b"HTTP/1.1 407 Rejected\r\nSet-Cookie: newly-issued-secret\r\n"
                                b"Authorization: Bearer new-token\r\nProxy-Authorization: Basic new-auth\r\n"
                                b"Cookie: another-secret\r\nProxy-Authenticate: Basic realm=decodo\r\n"
                                b"X-Request-Id: abc123\r\n\r\n", ("username", "password"))
        for i in range(1, 5):
            self.assertTrue(details[f"proxy_header_{i}"].endswith(": [redacted]"))
        self.assertEqual(details["proxy_header_5"], "Proxy-Authenticate: Basic realm=decodo")
        self.assertEqual(details["proxy_header_6"], "X-Request-Id: abc123")

    def test_test_returns_relay_error_independent_of_http_version_or_status(self):
        reason = "Browser connection failed during proxy TLS. Check Host diagnostics."
        for status in ("HTTP/1.0 502 Bad Gateway", "HTTP/1.1 503 Unavailable", "HTTP/1.1 407 Authentication Required"):
            headers = f"{status}\r\nX-Kern-Browser-Error: {reason}\r\n\r\n".encode()
            with self.subTest(status=status), patch("subprocess.run", return_value=Mock(returncode=22, stdout=b"", stderr=headers)):
                with self.assertRaises(BrowserError) as caught:
                    self.network.test()
                self.assertEqual(str(caught.exception), reason)

    def test_accounts_share_saved_connection_settings_with_browser_launch(self):
        accounts = Accounts(self.store)
        result = accounts.dispatch("network_save", DECODO)
        self.assertNotIn("password", result)
        restored = Accounts(accounts.store)
        self.assertEqual(restored.dispatch("network_get", {}), result)
        with patch("host.runtime.browser.accounts.Browser") as browser:
            restored.dispatch("ready", {})
            self.assertEqual(browser.call_args.args[2], result)
            self.assertNotIn("password", browser.call_args.args[2])
        self.store.settings = {"mode": "unknown"}
        broken = Accounts(accounts.store)
        with self.assertRaisesRegex(BrowserError, "settings"):
            broken.dispatch("ready", {})
        self.assertEqual(broken.dispatch("network_save", {"mode": "direct"})["mode"], "direct")

    def test_network_settings_cannot_be_called_as_agent_actions_or_during_login(self):
        accounts = Accounts(self.store, Mock())
        with self.assertRaises(BrowserError):
            accounts.dispatch("network_get", {}, agent_action=True)
        accounts.pending["pending"] = Mock(expires=float("inf"))
        with self.assertRaises(BrowserError):
            accounts.dispatch("network_save", {"mode": "direct"})
        users = {"kern-admin": Mock(pw_uid=1), "kern-tools": Mock(pw_uid=2)}
        with patch("host.runtime.browser.service.pwd.getpwnam", side_effect=users.__getitem__):
            self.assertEqual(authorized(1, "/operator/network_save"), "network_save")
            self.assertIsNone(authorized(2, "/operator/network_save"))
            self.assertIsNone(authorized(2, "/actions/network_save"))


class TunnelBoundaryTests(unittest.TestCase):
    def test_connect_relays_bytes_and_closes_upstream(self):
        operator, accepted = socket.socketpair()
        upstream, website = socket.socketpair()
        for stream in (operator, accepted, upstream, website):
            stream.settimeout(2)
            self.addCleanup(stream.close)
        network = Mock()
        network.dial.return_value = upstream
        server = Mock(network=network)
        worker = threading.Thread(target=TunnelHandler, args=(accepted, ("127.0.0.1", 1), server))
        worker.start()
        operator.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            response.extend(operator.recv(1))
        self.assertIn(b"200 Connection established", response)
        operator.sendall(b"encrypted-request")
        self.assertEqual(website.recv(100), b"encrypted-request")
        website.sendall(b"encrypted-response")
        self.assertEqual(operator.recv(100), b"encrypted-response")
        operator.shutdown(socket.SHUT_RDWR)
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        network.dial.assert_called_once_with("example.com:443")
        network.release.assert_called_once_with(upstream)
