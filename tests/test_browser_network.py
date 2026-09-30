"""Browser proxy boundary, private settings and fail-closed routing, offline."""
from __future__ import annotations

import base64
import socket
import ssl
import threading
import unittest
from unittest.mock import Mock, patch

from browser_fakes import MemoryStore
from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.config import LOCATIONS, Settings
from host.runtime.browser_network.relay import Network, TunnelHandler
from host.runtime.browser_network.transport import connect_proxy, target, is_public
from host.runtime.browser.accounts import Accounts
from host.runtime.browser.service import authorized

DECODO = {"mode": "decodo", "username": "example", "password": "private-password", "location": "new_york"}


class BrowserNetworkTests(unittest.TestCase):
    def setUp(self):
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
            with patch("host.runtime.browser_network.relay.target", return_value=["1.1.1.1"]), patch("socket.create_connection") as dial:
                with self.assertRaises(BrowserError):
                    recovered.dial("example.com:443")
                dial.assert_not_called()
        self.assertEqual(recovered.dispatch("get", {})["mode"], "direct")

    def test_direct_tries_remaining_validated_addresses_without_resolving_again(self):
        ipv6, ipv4 = "2606:4700::1111", "1.1.1.1"
        answers = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ipv6, 443, 0, 0)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ipv4, 443))]
        upstream = Mock()
        with patch("socket.getaddrinfo", return_value=answers) as resolve, patch("socket.create_connection", side_effect=[OSError("no IPv6 route"), upstream]) as connect:
            self.assertIs(self.network.dial("example.com:443"), upstream)
            resolve.assert_called_once_with("example.com", 443, type=socket.SOCK_STREAM)
            self.assertEqual([entry.args[0] for entry in connect.call_args_list], [(ipv6, 443), (ipv4, 443)])

    def test_atomic_write_failure_preserves_old_settings(self):
        self.network.dispatch("save", DECODO)
        with patch.object(self.store, "save_settings", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.network.dispatch("save", {"mode": "direct"})
        self.assertEqual({k: v for k, v in Settings(self.store).value.items() if k != "session"}, DECODO)

    def test_public_resolution_pins_ip_and_rejects_mixed_private_answers(self):
        answer = lambda ip: (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        with patch("socket.getaddrinfo", return_value=[answer("93.184.215.14")]):
            self.assertEqual(target("example.com:443"), ["93.184.215.14"])
        with patch("socket.getaddrinfo", return_value=[answer("93.184.215.14"), answer("10.0.0.1")]):
            with self.assertRaises(BrowserError):
                target("example.com:443")
        for address in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "100.101.102.103", "224.0.0.1", "::1", "fc00::1", "::ffff:127.0.0.1", "64:ff9b::a00:1", "2002:0a00:0001::"):
            self.assertFalse(is_public(address), address)
        for authority in ("example.com:80", "user:pass@example.com:443", "example.com:443/path", "example.com:443?x=1", "example.com:443\r\nX:yes"):
            with self.subTest(authority=authority), self.assertRaises(BrowserError):
                target(authority)

    def test_decodo_preserves_location_and_session_without_fallback(self):
        self.network.dispatch("save", DECODO)
        upstream = Mock()
        with patch("host.runtime.browser_network.relay.target", return_value=["93.184.215.14"]), patch("host.runtime.browser_network.relay.connect_proxy", return_value=upstream) as proxy:
            self.assertIs(self.network.dial("example.com:443"), upstream)
            proxy.assert_called_once_with(("gate.decodo.com", 7000), "93.184.215.14", (self.network.settings.proxy_username(), DECODO["password"]))
        self.network.release(upstream)
        with patch("host.runtime.browser_network.relay.target", return_value=["93.184.215.14"]), patch("host.runtime.browser_network.relay.connect_proxy", side_effect=BrowserError("rejected")), patch("socket.create_connection") as direct:
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

    def test_decodo_tries_pinned_addresses_with_the_same_route_and_credentials(self):
        self.network.dispatch("save", DECODO)
        addresses = ["2606:4700::1111", "1.1.1.1", "1.0.0.1"]
        credentials = (self.network.settings.proxy_username(), DECODO["password"])
        upstream = Mock()
        with patch("host.runtime.browser_network.relay.target", return_value=addresses) as resolve, patch("host.runtime.browser_network.relay.connect_proxy", side_effect=[BrowserError("tunnel rejected"), OSError("unreachable"), upstream]) as proxy, patch("host.runtime.browser_network.relay.connect_addresses") as direct:
            self.assertIs(self.network.dial("example.com:443"), upstream)
            resolve.assert_called_once_with("example.com:443")
            self.assertEqual([entry.args for entry in proxy.call_args_list], [(("gate.decodo.com", 7000), address, credentials) for address in addresses])
            direct.assert_not_called()

    def test_decodo_exhaustion_and_tls_errors_never_change_route(self):
        self.network.dispatch("save", DECODO)
        for failures, count in (([BrowserError("first"), BrowserError("last")], 2),
                                ([ssl.SSLCertVerificationError("untrusted gateway")], 1)):
            with self.subTest(failures=failures), patch("host.runtime.browser_network.relay.target", return_value=["1.1.1.1", "1.0.0.1"]), patch("host.runtime.browser_network.relay.connect_proxy", side_effect=failures) as proxy, patch("host.runtime.browser_network.relay.connect_addresses") as direct:
                with self.assertRaises(type(failures[-1])) as caught:
                    self.network.dial("example.com:443")
                self.assertIs(caught.exception, failures[-1])
                self.assertEqual(proxy.call_count, count)
                direct.assert_not_called()
                self.assertFalse(self.network.connections)

    def test_slow_dial_does_not_block_other_resources_or_settings_changes(self):
        from concurrent.futures import ThreadPoolExecutor
        for mode in ("direct", "decodo"):
            with self.subTest(mode=mode):
                self.network.dispatch("save", DECODO if mode == "decodo" else {"mode": "direct"})
                entered, resume = threading.Event(), threading.Event()
                slow, fast = Mock(), Mock()
                def connect(*args):
                    address = args[1] if mode == "decodo" else args[0][0]
                    if address == "1.1.1.1":
                        entered.set()
                        if not resume.wait(5):
                            raise TimeoutError("fixture was not released")
                        return slow
                    return fast
                connector = "connect_proxy" if mode == "decodo" else "connect_addresses"
                with patch("host.runtime.browser_network.relay.target", side_effect=lambda host: [host.split(":")[0]]), patch("host.runtime.browser_network.relay." + connector, side_effect=connect), ThreadPoolExecutor(max_workers=3) as pool:
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

    def test_connect_handshake_keeps_proxy_auth_out_of_tunnel_and_pins_target(self):
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
        with patch("host.runtime.browser_network.transport.connect_public", return_value=client), patch("host.runtime.browser_network.transport.ssl.create_default_context") as tls:
            tls.return_value.wrap_socket.return_value = client
            stream = connect_proxy(("gate.decodo.com", 7000), "93.184.215.14", ("username", "password"))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(received[0].startswith(b"CONNECT 93.184.215.14:443 HTTP/1.1\r\n"))
        self.assertIn(base64.b64encode(b"username:password"), received[0])
        self.assertEqual(stream.recv(11), b"TLS-FIXTURE")

    def test_provider_error_redacts_body_and_headers(self):
        client, peer = socket.socketpair()
        self.addCleanup(peer.close)
        peer.sendall(b"HTTP/1.1 407 secret-provider-message\r\nSecret: private\r\n\r\nprivate-body")
        with patch("host.runtime.browser_network.transport.connect_public", return_value=client), patch("host.runtime.browser_network.transport.ssl.create_default_context") as tls:
            tls.return_value.wrap_socket.return_value = client
            with self.assertRaises(BrowserError) as error:
                connect_proxy(("gate.decodo.com", 7000), "93.184.215.14", ("user", "secret"))
        self.assertIn("407", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(client.fileno(), -1)

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
