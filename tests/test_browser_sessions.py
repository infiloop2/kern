"""Browser service boundary and durable submission tests; no provider credentials."""
from __future__ import annotations
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from host.runtime.browser.client import BrowserError
from host.runtime.browser.accounts import Profile, Accounts
from host.runtime.browser.providers import PROVIDERS
from host.runtime.browser.browser import Browser, permitted_url
from host.runtime.browser.actions.x_post_tweet import validate_post, execute as post_tweet, PostRejected
from host.runtime.browser.service import authorized, Handler, Server
from host.runtime.core.unix_socket_service import UnixSocketServer
from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection
from host.runtime.admin_api import browser as admin_browser
from host.runtime.admin_api.errors import ApiError
from host.tools.browser import BUNDLED_TOOL, MANIFEST
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from browser_fakes import MemoryStore
from test_tools import FakeHostAPI, assert_matches_output_schema


class FakeBrowser:
    account_value = "example"
    fail_prepare = False
    fail_submit = False
    submissions = 0
    def __init__(self, profile, site, *, block_media=False):
        self.page = self
        self.profile = profile
        self.site = site
        self.block_media = block_media
    def account(self):
        return self.account_value
    def origin(self):
        return self.site
    def frame(self):
        return "operator-only-image"
    def close(self):
        pass
    def save_state(self):
        return {"cookies": [], "origins": []}
    def input(self, payload):
        pass
    def prepare(self, account, text, reply_id=""):
        if self.fail_prepare or account != self.account_value:
            raise RuntimeError("secret provider DOM")
    def submit(self, account, reply_id=""):
        type(self).submissions += 1
        if self.fail_submit:
            raise RuntimeError("secret provider response")
        return f"https://x.com/{account}/status/123"


class BrowserSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        FakeBrowser.account_value = "example"
        FakeBrowser.fail_submit = FakeBrowser.fail_prepare = False
        FakeBrowser.submissions = 0
        self.enterContext(patch("host.runtime.browser.providers.x.verify_account", side_effect=lambda page: page.account()))
        self.enterContext(patch("host.runtime.browser.actions.x_post_tweet.prepare_post", side_effect=lambda page, *args: page.prepare(*args)))
        self.enterContext(patch("host.runtime.browser.actions.x_post_tweet.submit_prepared_post", side_effect=lambda page, *args: page.submit(*args)))
        self.diagnostics = self.enterContext(patch("host.runtime.browser.actions.x_post_tweet.host_errors.emit_record"))
        self.store = MemoryStore()
        self.engine_store = MemoryStore()
        key = "acct_" + "1" * 32
        data = {"provider_identifier": "example", "state": "needs_attention", "checked_at": "", "usage": {}}
        self.engine_store.save_account(key, "x", data, {})
        self.engine = Profile(key, "x", FakeBrowser, self.engine_store, data)
        self.body = {"provider_identifier": "example", "text": "hello"}

    def connect(self):
        lease = self.engine.dispatch("open", {})["lease"]
        self.engine.dispatch("save", {"lease": lease})

    def test_readiness_uses_open_browser_without_closing_operator_window(self):
        accounts = Accounts(self.store, factory=FakeBrowser)
        login = accounts.dispatch("create", {"provider": "x"})
        accounts.dispatch("open", login)
        profile = accounts.pending[login["login_id"]]
        with patch.object(profile.browser, "frame", return_value="image") as frame, patch.object(profile.browser, "close") as close:
            self.assertEqual(accounts.dispatch("ready", {}), {"ready": True})
        frame.assert_called_once()
        close.assert_not_called()
        self.assertTrue(profile.lease)

    def test_takeover_pauses_until_saved_or_checked(self):
        self.connect()
        self.assertEqual(self.engine.status()["state"], "connected")
        lease = self.engine.dispatch("open", {})["lease"]
        with self.assertRaises(BrowserError):
            post_tweet(self.engine, self.body)
        self.engine.dispatch("cancel", {"lease": lease})
        self.assertEqual(self.engine.status()["state"], "needs_attention")
        self.assertEqual(self.engine.dispatch("check", {})["state"], "connected")

    def test_post_launch_skips_home_and_filters_media_but_operator_launch_does_not(self):
        self.connect()
        with patch.object(self.engine, "factory", wraps=FakeBrowser) as factory:
            for extra in ({}, {"in_reply_to_tweet_id": "12345"}):
                post_tweet(self.engine, {**self.body, **extra})
                self.assertEqual(factory.call_args.args[1], "about:blank")
                self.assertEqual(factory.call_args.kwargs, {"block_media": True})
                self.assertIsNone(self.engine.browser)
            lease = self.engine.dispatch("open", {})["lease"]
            self.assertEqual(factory.call_args.args[1], self.engine.provider.login_url)
            self.assertEqual(factory.call_args.kwargs, {"block_media": False})
            self.engine.dispatch("cancel", {"lease": lease})

    def test_post_resource_filter_preserves_composer_dependencies(self):
        browser = Browser.__new__(Browser)
        browser.reported_failures = set()
        requests = [
            ("https://pbs.twimg.com/media/photo.jpg", "image", True),
            ("https://abs.twimg.com/emoji.svg", "image", True),
            ("https://example.test/clip.mp4", "media", True),
            ("https://video.twimg.com/playlist.m3u8", "fetch", True),
            ("https://video.twimg.com/segment", "xhr", True),
            ("https://abs.twimg.com/responsive-web/main.js", "script", False),
            ("https://abs.twimg.com/style.css", "stylesheet", False),
            ("https://x.com/i/api/graphql/id/CreateTweet", "fetch", False),
            ("https://x.com/i/status/12345", "document", False),
            ("https://video.twimg.com.example.test/api", "fetch", False),
        ]
        for enabled in (False, True):
            browser.block_media = enabled
            for url, resource_type, omitted in requests:
                with self.subTest(enabled=enabled, url=url):
                    route = Mock(request=SimpleNamespace(url=url, resource_type=resource_type, failure="net::ERR_FAILED"))
                    with patch.object(browser, "report_failure") as failure:
                        browser.route_request(route)
                        if enabled and omitted:
                            route.abort.assert_called_once()
                            route.fallback.assert_not_called()
                            browser.record_failed_request(route.request)
                            failure.assert_not_called()
                        else:
                            route.abort.assert_not_called()
                            route.fallback.assert_called_once()

    def test_login_check_detects_account_change_and_pauses(self):
        self.connect()
        FakeBrowser.account_value = "other"
        state = self.engine.dispatch("check", {})
        self.assertEqual(state["provider_identifier"], "example")
        self.assertEqual(state["state"], "needs_attention")
        restored = Profile(self.engine.account_id, "x", FakeBrowser, self.engine_store, self.engine_store.accounts()[self.engine.account_id][1])
        self.assertEqual(restored.status()["state"], "needs_attention")
        with self.assertRaises(BrowserError):
            post_tweet(restored, self.body)

    def test_provider_contract_preserves_identifier_and_x_action_rejects_it(self):
        provider = SimpleNamespace(name="fixture", login_url="https://fixture.example/",
                                   verify_account=lambda page: "Owner@example.test",
                                   validate_identifier=lambda value: value)
        with patch.dict(PROVIDERS, {"fixture": provider}):
            accounts = Accounts(self.store, FakeBrowser)
            login = accounts.dispatch("create", {"provider": "fixture"})
            lease = accounts.dispatch("open", login)["lease"]
            saved = accounts.dispatch("save", {**login, "lease": lease})
            self.assertEqual(saved["provider"], "fixture")
            self.assertEqual(saved["provider_identifier"], "Owner@example.test")
            restored = Accounts(accounts.store, FakeBrowser)
            self.assertEqual(restored.dispatch("list", {})["accounts"], [saved])
            profile = restored.profiles[saved["account_id"]]
            with patch.object(profile, "launch") as launch:
                with self.assertRaisesRegex(BrowserError, "requires an X connection"):
                    restored.dispatch("post_tweet", {"account_id": saved["account_id"], **self.body}, agent_action=True)
                launch.assert_not_called()

    def test_each_service_call_attempts_once_and_allows_identical_content(self):
        self.connect()
        self.assertEqual(post_tweet(self.engine, self.body)["status"], "posted")
        restored = Profile(self.engine.account_id, "x", FakeBrowser, self.engine_store, self.engine_store.accounts()[self.engine.account_id][1])
        self.assertEqual(post_tweet(restored, self.body)["status"], "posted")
        self.assertEqual(FakeBrowser.submissions, 2)
        self.assertEqual(restored.data["usage"]["x_post_tweet"]["count"], 2)

    def test_submit_failure_is_terminal_counts_attempt_and_does_not_pause_login(self):
        self.connect()
        FakeBrowser.fail_submit = True
        with self.assertRaisesRegex(BrowserError, "Check X before approving another attempt"):
            post_tweet(self.engine, self.body)
        self.assertEqual(FakeBrowser.submissions, 1)
        restored = Profile(self.engine.account_id, "x", FakeBrowser, self.engine_store, self.engine_store.accounts()[self.engine.account_id][1])
        self.assertEqual(restored.status()["state"], "connected")
        self.assertEqual(restored.data["usage"]["x_post_tweet"]["count"], 1)
        FakeBrowser.fail_submit = False
        self.assertEqual(post_tweet(restored, self.body)["status"], "posted")
        self.assertEqual(FakeBrowser.submissions, 2)

    def test_post_result_survives_snapshot_failure(self):
        self.connect()
        with patch.object(FakeBrowser, "save_state", side_effect=OSError("private disk detail")), patch("host.runtime.browser.actions.x_post_tweet.host_errors.report_warning") as warning:
            self.assertEqual(post_tweet(self.engine, self.body), {"status": "posted", "url": "https://x.com/example/status/123"})
            self.assertEqual(FakeBrowser.submissions, 1)
            self.assertIsNone(self.engine.browser)
            warning.assert_called_once()
            self.assertEqual(str(warning.call_args.args[1]), "OSError")
            self.assertEqual(warning.call_args.kwargs["context"], {"stage": "cleanup", "failure_type": "OSError"})
            FakeBrowser.fail_prepare = True
            with self.assertRaisesRegex(BrowserError, "not submitted"):
                post_tweet(self.engine, self.body)

    def test_explicit_rejection_preserves_message_and_counts_attempt(self):
        self.connect()
        with patch("host.runtime.browser.actions.x_post_tweet.submit_prepared_post", side_effect=PostRejected("X rejected the submission (duplicate post, code 187).")):
            with self.assertRaisesRegex(PostRejected, "duplicate post"):
                post_tweet(self.engine, self.body)
        self.assertEqual(self.engine.data["usage"]["x_post_tweet"]["count"], 1)

    def test_post_diagnostics_keep_stages_stack_and_safe_reasons(self):
        self.connect()
        for function, error, stage in [
            ("prepare_post", RuntimeError("private-live-token"), "prepare"),
            ("submit_prepared_post", RuntimeError("private-live-token"), "confirm"),
            ("submit_prepared_post", PostRejected("X rejected the submission (code 226)."), "rejected"),
        ]:
            with self.subTest(stage=stage), patch("host.runtime.browser.actions.x_post_tweet." + function, side_effect=error):
                with self.assertRaises(BrowserError):
                    post_tweet(self.engine, self.body)
                record = self.diagnostics.call_args.args[0]
                self.assertEqual(record["component"], "browser.x_post_tweet")
                self.assertEqual(record["context"]["stage"], stage)
                self.assertEqual(record["context"]["failure_type"], type(error).__name__)
                if stage == "prepare":
                    self.assertEqual(record["context"]["step"], "prepare_composer")
                self.assertIn("x_post_tweet.py", record["traceback"])
                self.assertNotIn("private-live-token", json.dumps(record))
                self.assertEqual(record["summary"], str(error) if stage == "rejected" else "RuntimeError")

    def test_check_cannot_launch_while_another_login_is_open(self):
        accounts = Accounts(self.store, FakeBrowser)
        first = self.connect_account(accounts)
        login = accounts.dispatch("create", {"provider": "x"})
        accounts.dispatch("open", login)
        with patch.object(accounts.profiles[first], "launch") as launch:
            with self.assertRaisesRegex(BrowserError, "Another website is open"):
                accounts.dispatch("check", {"account_id": first})
            launch.assert_not_called()

    def test_prepare_failure_has_no_submission_or_usage(self):
        self.connect()
        FakeBrowser.fail_prepare = True
        with self.assertRaisesRegex(BrowserError, "not submitted"):
            post_tweet(self.engine, self.body)
        self.assertEqual(FakeBrowser.submissions, 0)
        self.assertEqual(self.engine.data["usage"], {})

    def test_daily_limit_counts_post_and_reply_attempts_after_restart(self):
        self.connect()
        with self.assertRaises(BrowserError):
            post_tweet(self.engine, {**self.body, "provider_identifier": "other"})
        for number in range(50):
            payload = {**self.body, "text": str(number)}
            if number % 2:
                payload["in_reply_to_tweet_id"] = "12345"
            post_tweet(self.engine, payload)
        restored = Profile(self.engine.account_id, "x", FakeBrowser, self.engine_store, self.engine_store.accounts()[self.engine.account_id][1])
        with self.assertRaisesRegex(BrowserError, "daily"):
            post_tweet(restored, self.body)
        self.assertEqual(FakeBrowser.submissions, 50)
        restored.data["usage"]["x_post_tweet"]["day"] = "2000-01-01"
        restored.data["usage"]["another_action"] = {"day": datetime.now(timezone.utc).date().isoformat(), "count": 500}
        self.assertEqual(post_tweet(restored, self.body)["status"], "posted")
        self.assertEqual(restored.data["usage"]["x_post_tweet"]["count"], 1)

    def test_lease_authentication_and_idle_expiry(self):
        lease = self.engine.dispatch("open", {})["lease"]
        with self.assertRaises(BrowserError):
            self.engine.dispatch("frame", {"lease": "wrong"})
        with self.assertRaises(BrowserError):
            self.engine.dispatch("open", {})
        with self.assertRaises(BrowserError):
            self.engine.dispatch("check", {})
        expires = self.engine.expires
        self.engine.dispatch("frame", {"lease": lease})
        self.assertEqual(expires, self.engine.expires)
        self.engine.expires = 0
        with self.assertRaises(BrowserError):
            self.engine.dispatch("input", {"lease": lease, "kind": "home"})
        self.assertEqual(self.engine.status()["state"], "needs_attention")
        self.assertIsNone(self.engine.browser)

    def connect_account(self, accounts, handle="example"):
        FakeBrowser.account_value = handle
        login = accounts.dispatch("create", {"provider": "x"})
        lease = accounts.dispatch("open", login)["lease"]
        return accounts.dispatch("save", {**login, "lease": lease})["account_id"]

    def test_temporary_login_is_not_an_account_and_cancel_discards_it(self):
        accounts = Accounts(self.store, FakeBrowser)
        login = accounts.dispatch("create", {"provider": "x"})
        self.assertEqual(accounts.dispatch("list", {}), {"accounts": []})
        with self.assertRaises(BrowserError):
            accounts.dispatch("post_tweet", {**login, **self.body}, agent_action=True)
        with self.assertRaisesRegex(BrowserError, "Unsupported browser provider"):
            accounts.dispatch("create", {"provider": "linkedin"})
        lease = accounts.dispatch("open", login)["lease"]
        accounts.dispatch("cancel", {**login, "lease": lease})
        self.assertEqual(accounts.store.accounts(), {})
        login = accounts.dispatch("create", {"provider": "x"})
        accounts.pending[login["login_id"]].expires = 0
        accounts.expire()
        self.assertEqual(accounts.store.accounts(), {})
        accounts.dispatch("create", {"provider": "x"})
        restarted = Accounts(accounts.store, FakeBrowser)
        self.assertEqual(restarted.dispatch("list", {}), {"accounts": []})
        self.assertEqual(accounts.store.accounts(), {})

    def test_connect_replaces_abandoned_login_and_rejects_late_old_requests(self):
        accounts = Accounts(self.store, FakeBrowser)
        old = accounts.dispatch("create", {"provider": "x"})
        lease = accounts.dispatch("open", old)["lease"]
        browser = accounts.pending[old["login_id"]].browser
        with patch.object(browser, "close") as close, patch.object(browser, "save_state") as snapshot:
            new = accounts.dispatch("create", {"provider": "x"})
            close.assert_called_once()
            snapshot.assert_not_called()
        self.assertNotEqual(old, new)
        new_lease = accounts.dispatch("open", new)["lease"]
        for operation in ("open", "frame", "input", "save", "cancel"):
            with self.subTest(operation=operation), self.assertRaises(BrowserError):
                accounts.dispatch(operation, {**old, "lease": lease})
        self.assertEqual(accounts.dispatch("frame", {**new, "lease": new_lease})["image"], "operator-only-image")
        self.assertEqual(self.store.accounts(), {})

    def test_connect_replaces_unopened_login_and_saved_account_control(self):
        accounts = Accounts(self.store, FakeBrowser)
        account_id = self.connect_account(accounts)
        lease = accounts.dispatch("open", {"account_id": account_id})["lease"]
        old = accounts.profiles[account_id]
        with patch.object(old.browser, "close") as close:
            first = accounts.dispatch("create", {"provider": "x"})
            close.assert_called_once()
        second = accounts.dispatch("create", {"provider": "x"})
        self.assertNotEqual(first, second)
        self.assertEqual(list(accounts.pending), [second["login_id"]])
        self.assertEqual(old.status()["state"], "needs_attention")
        self.assertIn(account_id, self.store.accounts())
        with self.assertRaises(BrowserError):
            accounts.dispatch("cancel", {"account_id": account_id, "lease": lease})
        accounts.dispatch("open", second)

    def test_invalid_or_agent_create_does_not_replace_operator_login(self):
        accounts = Accounts(self.store, FakeBrowser)
        login = accounts.dispatch("create", {"provider": "x"})
        lease = accounts.dispatch("open", login)["lease"]
        for body, agent_action in [({"provider": "invalid"}, False), ({"provider": "x"}, True)]:
            with self.assertRaises(BrowserError):
                accounts.dispatch("create", body, agent_action=agent_action)
        self.assertEqual(accounts.dispatch("frame", {**login, "lease": lease})["image"], "operator-only-image")

    def test_failed_new_account_save_remains_cancellable_without_saved_state(self):
        accounts = Accounts(self.store, FakeBrowser)
        login = accounts.dispatch("create", {"provider": "x"})
        lease = accounts.dispatch("open", login)["lease"]
        with patch.object(self.store, "save_account", side_effect=OSError("database unavailable")):
            with self.assertRaises(OSError):
                accounts.dispatch("save", {**login, "lease": lease})
        self.assertEqual(accounts.dispatch("list", {}), {"accounts": []})
        self.assertEqual(self.store.accounts(), {})
        accounts.dispatch("cancel", {**login, "lease": lease})
        self.assertEqual(accounts.pending, {})
        self.assertIn("login_id", accounts.dispatch("create", {"provider": "x"}))

    def test_agent_actions_pause_only_for_account_needing_attention(self):
        accounts = Accounts(self.store, FakeBrowser)
        first = self.connect_account(accounts)
        second = self.connect_account(accounts, "another")
        lease = accounts.dispatch("open", {"account_id": second})["lease"]
        with self.assertRaisesRegex(BrowserError, "operator control"):
            accounts.dispatch("post_tweet", {"account_id": second, **self.body}, agent_action=True)
        accounts.dispatch("save", {"account_id": second, "lease": lease})
        self.assertEqual(accounts.dispatch("check", {"account_id": first})["state"], "needs_attention")
        with self.assertRaisesRegex(BrowserError, "login needs attention"):
            accounts.dispatch("post_tweet", {"account_id": first, **self.body}, agent_action=True)
        self.assertEqual(accounts.dispatch("post_tweet", {"account_id": second, **self.body, "provider_identifier": "another"}, agent_action=True)["status"], "posted")
        self.assertNotEqual(accounts.profiles[first].account_id, accounts.profiles[second].account_id)
        with self.assertRaises(BrowserError):
            accounts.dispatch("open", {"account_id": "../../admin-state"})

    def test_account_binding_cannot_change_on_reconnect(self):
        accounts = Accounts(self.store, FakeBrowser)
        account_id = self.connect_account(accounts)
        FakeBrowser.account_value = "another"
        lease = accounts.dispatch("open", {"account_id": account_id})["lease"]
        with self.assertRaisesRegex(BrowserError, "another account"):
            accounts.dispatch("save", {"account_id": account_id, "lease": lease})
        accounts.dispatch("cancel", {"account_id": account_id, "lease": lease})
        restored = Accounts(accounts.store, FakeBrowser)
        saved = restored.dispatch("list", {})["accounts"][0]
        self.assertEqual(saved, {"account_id": account_id, "provider": "x", "provider_identifier": "example", "state": "needs_attention", "checked_at": saved["checked_at"]})
        FakeBrowser.account_value = "example"
        lease = restored.dispatch("open", {"account_id": account_id})["lease"]
        restored.dispatch("save", {"account_id": account_id, "lease": lease})
        self.assertEqual(restored.dispatch("list", {})["accounts"][0]["state"], "connected")

    def test_disconnect_deletes_usage_and_releases_account_slot(self):
        accounts = Accounts(self.store, FakeBrowser)
        ids = [self.connect_account(accounts, f"user{index}") for index in range(5)]
        with self.assertRaisesRegex(BrowserError, "Up to 5 browser accounts"):
            accounts.dispatch("create", {"provider": "x"})
        FakeBrowser.account_value = "user0"
        accounts.dispatch("post_tweet", {"account_id": ids[0], **self.body, "provider_identifier": "user0"}, agent_action=True)
        accounts.dispatch("disconnect", {"account_id": ids[0]})
        self.assertNotIn(ids[0], accounts.store.accounts())
        restored = Accounts(accounts.store, FakeBrowser)
        self.assertEqual(len(restored.dispatch("list", {})["accounts"]), 4)
        with self.assertRaises(BrowserError):
            restored.dispatch("post_tweet", {"account_id": ids[0], **self.body}, agent_action=True)
        self.connect_account(restored, "replacement")
        FakeBrowser.account_value = "user1"
        FakeBrowser.fail_submit = True
        with self.assertRaisesRegex(BrowserError, "Could not confirm"):
            restored.dispatch("post_tweet", {"account_id": ids[1], **self.body, "provider_identifier": "user1"}, agent_action=True)
        restored.dispatch("disconnect", {"account_id": ids[1]})
        self.assertNotIn(ids[1], restored.store.accounts())
        self.assertNotIn(ids[1], restored.profiles)

    def test_snapshot_returns_state_without_creating_auth_files(self):
        browser = Browser.__new__(Browser)
        browser.block_media = False
        browser.context = SimpleNamespace(storage_state=lambda **kwargs: {"cookies": [], "origins": []})
        self.assertEqual(browser.save_state(), {"cookies": [], "origins": []})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_save_failure_closes_browser_and_disconnect_does_not_save(self):
        self.connect()
        self.engine.launch()
        with patch.object(self.engine.browser, "save_state", side_effect=BrowserError("storage full")), patch.object(self.engine.browser, "close") as close:
            with self.assertRaisesRegex(BrowserError, "storage full"):
                self.engine.close()
            close.assert_called_once()
        self.assertIsNone(self.engine.browser)
        self.engine.launch()
        with patch.object(self.engine.browser, "save_state", side_effect=AssertionError("disconnect must not save")), patch.object(self.engine.browser, "close") as close:
            self.engine.dispatch("disconnect", {})
            close.assert_called_once()

    def test_browser_firewall_isolates_relay_and_blocks_private_destinations(self):
        bootstrap = (Path(__file__).resolve().parents[1] / "host/bootstrap/bootstrap.sh").read_text()
        relay = bootstrap.index('tcp dport @BROWSER_NETWORK_PORT@ meta skuid "kern-browser" accept')
        relay_drop = bootstrap.index('oif lo tcp dport @BROWSER_NETWORK_PORT@ drop')
        private_drop = bootstrap.index('meta skuid "kern-browser" ip daddr')
        public = bootstrap.index('meta skuid "kern-browser" tcp dport { 443, 7000 } accept')
        browser_drop = bootstrap.index('meta skuid "kern-browser" drop')
        self.assertLess(relay, relay_drop)
        self.assertLess(relay_drop, private_drop)
        self.assertLess(private_drop, public)
        self.assertLess(public, browser_drop)
        self.assertLess(browser_drop, bootstrap.index('    oif lo accept'))
        for protocol in ("udp", "tcp"):
            self.assertLess(bootstrap.index(f'meta skuid "kern-browser" {protocol} dport 53 accept'), private_drop)
        self.assertNotIn("kern-browser-network", bootstrap)
        debug_allow = bootstrap.index('tcp dport @BROWSER_DEBUG_PORT@ meta skuid "kern-browser" accept')
        debug_drop = bootstrap.index('oif lo tcp dport @BROWSER_DEBUG_PORT@ drop')
        self.assertLess(debug_allow, debug_drop)
        self.assertLess(debug_drop, private_drop)
        self.assertLess(bootstrap.index('oif lo tcp sport @BROWSER_DEBUG_PORT@ drop'), private_drop)

    def test_input_and_origin_validation(self):
        for url in ["file:///etc/passwd", "http://x.com", "https://127.0.0.1", "https://[::1]", "https://169.254.169.254", "https://user:password@x.com", "https://x.com:7443"]:
            self.assertFalse(permitted_url(url), url)
        self.assertTrue(permitted_url("https://x.com/"))
        for body in [{**self.body, "reply_id": "123"}, {**self.body, "in_reply_to_tweet_id": ""},
                     {**self.body, "in_reply_to_tweet_id": "https://x.com/i/status/123"},
                     {**self.body, "request_id": "short"}, {**self.body, "text": ""}]:
            with self.assertRaises(BrowserError):
                validate_post(body)

    def test_browser_failure_logs_are_sanitized_and_bounded(self):
        browser = Browser.__new__(Browser)
        browser.block_media = False
        browser.reported_failures = set()
        request = SimpleNamespace(
            url="https://abs.twimg.com/private/path?token=secret", resource_type="script",
            failure="net::ERR_CONNECTION_RESET private details",
        )
        with patch("host.runtime.browser.browser.host_errors.report_warning") as warning, patch(
            "host.runtime.browser.browser.host_metrics.service_resource_snapshot",
            return_value={"browser_memory_bytes": 12345},
        ):
            browser.record_failed_request(request)
            browser.record_failed_request(request)
            browser.record_response(SimpleNamespace(request=request, status=200))
            warning.assert_called_once_with(
                "browser.session", "net::ERR_CONNECTION_RESET",
                context={"host": "abs.twimg.com", "resource_type": "script", "browser_memory_bytes": 12345},
            )
            browser.record_response(SimpleNamespace(request=request, status=403))
            self.assertEqual(warning.call_args.args[1], "HTTP 403")
            request.failure = "private details without an error code"
            browser.record_failed_request(request)
            self.assertEqual(warning.call_args.args[1], "Request failed")
            for index in range(30):
                request.url = f"https://host{index}.example/private?token=secret"
                browser.record_failed_request(request)
            self.assertEqual(warning.call_count, 20)
            self.assertNotIn("private", str(warning.call_args_list))
            self.assertNotIn("secret", str(warning.call_args_list))

    def test_browser_policy_blocks_are_logged_and_still_aborted(self):
        browser = Browser.__new__(Browser)
        browser.block_media = False
        browser.reported_failures = set()
        route = Mock(request=SimpleNamespace(url="https://127.0.0.1/private", resource_type="document"))
        with patch("host.runtime.browser.browser.host_errors.report_warning") as warning:
            browser.route_request(route)
            route.request.failure = "net::ERR_FAILED"
            browser.record_failed_request(route.request)
        warning.assert_called_once()
        route.abort.assert_called_once()
        route.fallback.assert_not_called()
        self.assertEqual(warning.call_args.args[1], "Blocked by Browser URL policy")

    def test_page_errors_record_host_and_standard_type_without_private_content(self):
        browser = Browser.__new__(Browser)
        browser.reported_failures = set()
        page = SimpleNamespace(url="https://x.com/private-path?token=secret")
        with patch("host.runtime.browser.browser.host_errors.report_warning") as warning:
            for name in ("TypeError", "private-error-name"):
                browser.record_page_error(page, SimpleNamespace(name=name, message="private-message", stack="private-stack"))
            self.assertEqual(warning.call_args_list[0].args[1], "Uncaught page script error (TypeError)")
            self.assertEqual(warning.call_args.args[1], "Uncaught page script error (Error)")
            self.assertEqual(warning.call_args.kwargs["context"]["host"], "x.com")
            self.assertEqual(warning.call_args.kwargs["context"]["resource_type"], "script")
            self.assertNotIn("private", str(warning.call_args_list))
            self.assertNotIn("secret", str(warning.call_args_list))

    def test_browser_warning_is_accepted_by_host_diagnostics_collector(self):
        from host.runtime.host_diagnostics_collector.collector import parse_journal_record
        browser = Browser.__new__(Browser)
        browser.block_media = False
        browser.reported_failures = set()
        with patch("host.runtime.core.host_errors.emit_record") as emit:
            browser.report_failure("Screenshot capture failed")
        _, event = parse_journal_record(json.dumps({
            "__REALTIME_TIMESTAMP": "1000000", "_SYSTEMD_UNIT": "kern-browser.service",
            "MESSAGE": json.dumps(emit.call_args.args[0]),
        }))
        self.assertEqual(event["kind"], "unexpected_behavior")
        self.assertEqual(event["service"], "kern-browser")
        self.assertEqual(event["summary"], "Screenshot capture failed")

    def test_screenshot_failures_are_logged_once_without_changing_error(self):
        browser = Browser.__new__(Browser)
        browser.block_media = False
        browser.reported_failures = set()
        browser.page = Mock()
        browser.page.screenshot.side_effect = RuntimeError("private screenshot failure")
        with patch("host.runtime.browser.browser.host_errors.report_warning") as warning:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "private screenshot failure"):
                    browser.frame()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "Screenshot capture failed")
        self.assertNotIn("private", str(warning.call_args))

    def test_browser_home_and_reload_use_fixed_page_targets(self):
        browser = Browser.__new__(Browser)
        browser.block_media = False
        browser.site = "https://x.com/"
        browser.page = Mock()
        browser.input({"kind": "home"})
        browser.input({"kind": "reload"})
        browser.page.goto.assert_called_once_with("https://x.com/", wait_until="domcontentloaded")
        browser.page.reload.assert_called_once_with(wait_until="domcontentloaded")
        with self.assertRaises(BrowserError):
            browser.input({"kind": "reload", "url": "https://other.example"})

    def test_peers_have_disjoint_capabilities(self):
        self.assertEqual(MANIFEST.host_service_dependency, "kern-browser.service")
        self.assertFalse(MANIFEST.service)
        with self.assertRaisesRegex(ValueError, "host_service_dependency must name"):
            replace(MANIFEST, host_service_dependency="browser")
        with self.assertRaisesRegex(ValueError, "both service and host_service_dependency"):
            replace(MANIFEST, service="host.tools.whatsapp.gateway:GATEWAY")
        def user(name):
            return SimpleNamespace(pw_uid={"kern-admin": 1, "kern-tools": 2}[name])
        with patch("host.runtime.browser.service.pwd.getpwnam", side_effect=user):
            self.assertEqual(authorized(1, "/operator/frame"), "frame")
            self.assertEqual(authorized(1, "/operator/check"), "check")
            self.assertEqual(authorized(1, "/operator/cancel"), "cancel")
            self.assertEqual(authorized(2, "/actions/post_tweet"), "post_tweet")
            for uid, path in [(2, "/operator/frame"), (2, "/operator/check"), (2, "/operator/cancel"), (2, "/operator/settings"), (1, "/actions/post_tweet"), (3, "/actions/list"), (3, "/operator/input"), (2, "/actions/evaluate")]:
                self.assertIsNone(authorized(uid, path))

    def test_real_socket_rejects_forbidden_peer_before_dispatch(self):
        socket_path = str(self.root / "browser.sock")
        server = UnixSocketServer(socket_path, Handler)
        server.busy = threading.Lock()
        server.worker = ThreadPoolExecutor(max_workers=1)
        server.profiles = SimpleNamespace(dispatch=lambda operation, body, *, agent_action=False: {"operation": operation, "agent_action": agent_action, "body": body})
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            for role, path, expected in [("agent", "/actions/list", 403), ("tools", "/operator/frame", 403),
                                          ("admin", "/actions/post_tweet", 403), ("tools", "/actions/list", 200),
                                          ("admin", "/operator/list", 200), ("admin", "/operator/input", 200)]:
                def user(name):
                    admitted = (role == "tools" and name == "kern-tools") or (role == "admin" and name == "kern-admin")
                    return SimpleNamespace(pw_uid=os.getuid() if admitted else -1)
                with patch("host.runtime.browser.service.pwd.getpwnam", side_effect=user):
                    connection = UnixHTTPConnection(socket_path)
                    try:
                        if path == "/operator/input":
                            server.busy.acquire()
                            threading.Timer(0.05, server.busy.release).start()
                        body = {"kind": "text", "text": "😀" * 4096} if path == "/operator/input" else {}
                        connection.request("POST", path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, expected)
                        result = json.loads(response.read())
                        if expected == 403:
                            self.assertNotIn("operation", result)
                        else:
                            self.assertEqual(result["agent_action"], role == "tools")
                            self.assertEqual(result["body"], body)
                    finally:
                        connection.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            server.worker.shutdown()

    def test_untrusted_peer_is_closed_before_allocating_a_handler_thread(self):
        server = Server.__new__(Server)
        server.allowed_uids = frozenset({os.getuid() + 1})
        request, client = socket.socketpair()
        self.addCleanup(client.close)
        with patch.object(UnixSocketServer, "process_request") as start_handler:
            server.process_request(request, None)
        start_handler.assert_not_called()
        self.assertEqual(request.fileno(), -1)

        server.allowed_uids = frozenset({os.getuid()})
        admitted, client2 = socket.socketpair()
        self.addCleanup(admitted.close)
        self.addCleanup(client2.close)
        with patch.object(UnixSocketServer, "process_request") as start_handler:
            server.process_request(admitted, None)
        start_handler.assert_called_once_with(admitted, None)

    def test_real_socket_cancel_queues_behind_frame_and_releases_control(self):
        accounts = Accounts(self.store, FakeBrowser)
        login = accounts.dispatch("create", {"provider": "x"})
        lease = accounts.dispatch("open", login)["lease"]
        profile = accounts.pending[login["login_id"]]
        control = {**login, "lease": lease}
        frame_started, release_frame, cancel_queued = (threading.Event() for _ in range(3))

        def frame():
            frame_started.set()
            if not release_frame.wait(10):
                raise AssertionError("fixture frame was never released")
            return "fixture-image"

        socket_path = str(self.root / "cancel.sock")
        server = UnixSocketServer(socket_path, Handler)
        server.busy = threading.Lock()
        server.worker = ThreadPoolExecutor(max_workers=1)
        server.profiles = accounts
        submit = server.worker.submit

        def observe_submit(function, operation, body, **kwargs):
            future = submit(function, operation, body, **kwargs)
            if operation == "cancel":
                cancel_queued.set()
            return future

        def request(operation):
            connection = UnixHTTPConnection(socket_path)
            try:
                connection.request("POST", "/operator/" + operation, json.dumps(control), {"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        clients = ThreadPoolExecutor(max_workers=2)
        try:
            with patch.object(profile.browser, "frame", side_effect=frame), patch.object(server.worker, "submit", side_effect=observe_submit), patch(
                "host.runtime.browser.service.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid()),
            ):
                captured = clients.submit(request, "frame")
                self.assertTrue(frame_started.wait(3), "frame never acquired the worker")
                cancelled = clients.submit(request, "cancel")
                self.assertTrue(cancel_queued.wait(3), "Cancel was rejected instead of queued behind the busy frame")
                self.assertTrue(server.busy.locked())
                self.assertFalse(cancelled.done())
                self.assertEqual(profile.lease, lease)
                release_frame.set()
                self.assertEqual(captured.result(timeout=3)[0], 200)
                self.assertEqual(cancelled.result(timeout=3), (200, {"ok": True}))
                self.assertNotIn(login["login_id"], accounts.pending)
                self.assertFalse(profile.lease)
                self.assertIsNone(profile.browser)
        finally:
            release_frame.set()
            clients.shutdown()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            server.worker.shutdown()

    def test_admin_routes_complete_new_login_through_real_account_service(self):
        from host.runtime.admin_api import service as admin_api
        accounts = Accounts(self.store, FakeBrowser)
        def route(operation, body):
            return admin_api.route("POST", "/v1/browser/" + operation, {}, body,
                                   principal=admin_api.OperatorPrincipal("test-session"))
        def request(path, body):
            return accounts.dispatch(path.removeprefix("/operator/"), body)
        with patch.object(admin_browser.state, "enabled_tool_ids", return_value={"browser"}), patch.object(admin_browser.client, "request", side_effect=request):
            login = route("create", {"provider": "x"})
            lease = route("open", login)["lease"]
            saved = route("save", {**login, "lease": lease})
            self.assertEqual(route("list", {})["accounts"], [saved])
            restored = Accounts(accounts.store, FakeBrowser)
            self.assertEqual(restored.dispatch("list", {})["accounts"], [saved])
            for operation in ("finish", "settings", "history", "resolve"):
                with self.assertRaises(ApiError) as failure:
                    route(operation, {})
                self.assertEqual(failure.exception.status, 404)
            with self.assertRaises(ApiError):
                admin_api.route("POST", "/v1/browser/save", {}, {**login, "lease": lease},
                                principal=admin_api.WorkspacePrincipal())

    def test_unexpected_login_check_error_persists_attention(self):
        self.connect()
        with patch.object(FakeBrowser, "account", side_effect=RuntimeError("provider detail")):
            with self.assertRaises(RuntimeError):
                self.engine.dispatch("check", {})
        restored = Profile(self.engine.account_id, "x", FakeBrowser, self.engine_store, self.engine_store.accounts()[self.engine.account_id][1])
        self.assertEqual(restored.status()["state"], "needs_attention")

    def test_disabled_integration_cannot_open_but_can_disconnect(self):
        with patch.object(admin_browser.state, "enabled_tool_ids", return_value=set()), patch.object(admin_browser.client, "request", return_value={}) as request:
            with self.assertRaises(ApiError):
                admin_browser.control("open", {})
            request.assert_not_called()
            admin_browser.control("disconnect", {"account_id": "saved"})
            request.assert_called_once()

    def test_approved_text_is_preserved_without_outbound_guard(self):
        api = FakeHostAPI()
        text = "Contact example@example.com about this post."
        selected = {"accounts": [{"account_id": "acct_" + "a" * 32, "provider": "x", "provider_identifier": "example", "state": "connected"}]}
        with patch.object(api.outbound, "guard_request_parameter_string", side_effect=AssertionError("approval is the content control")), patch("host.tools.browser.client.request", return_value=selected):
            pending = BUNDLED_TOOL.execute("x_post_tweet", {"account_id": "acct_" + "a" * 32, "text": text}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertEqual(api.approvals.get(pending.approval_id).payload["text"], text)

    def test_tool_requires_approval_for_exact_post_or_reply(self):
        api = FakeHostAPI()
        post_schema = next(action.input_schema for action in MANIFEST.actions if action.id == "x_post_tweet")
        self.assertEqual(set(post_schema["required"]), {"account_id", "text"})
        self.assertNotIn("provider_identifier", post_schema["properties"])
        selected = {"accounts": [{"account_id": "a" * 32, "provider": "x", "provider_identifier": "example", "state": "connected"}]}
        reply = {"account_id": "a" * 32, "text": self.body["text"], "in_reply_to_tweet_id": "12345"}
        target = {"id": "12345", "text": "Original target text", "status": "loaded"}
        with patch("host.tools.browser.client.request", return_value=selected) as request, \
             patch("host.tools.browser.target_tweet", return_value=target) as context:
            pending = BUNDLED_TOOL.execute("x_post_tweet", reply, api)
            self.assertIsInstance(pending, ActionPendingApproval)
            request.assert_called_once_with("/actions/list")
            context.assert_called_once_with("12345")
        record = api.approvals.get(pending.approval_id)
        self.assertEqual(record.payload["in_reply_to_tweet_id"], "12345")
        self.assertEqual(record.payload["target_tweet"], target)
        self.assertIn("https://x.com/i/status/12345", record.summary)
        self.assertIn("hello", record.summary)
        with patch("host.tools.browser.client.request", side_effect=[selected, {"status": "posted", "url": "https://x.com/example/status/67890"}]) as request:
            result = BUNDLED_TOOL.execute_approved(api.approvals.approve(pending.approval_id), api)
            self.assertIsInstance(result, ApprovalExecuted)
            self.assertEqual(request.call_args.args, ("/actions/post_tweet", {**reply, "provider_identifier": "example"}))

    def test_approved_submission_failure_returns_failure_without_retry(self):
        api = FakeHostAPI()
        selected = {"accounts": [{"account_id": "acct_" + "a" * 32, "provider": "x", "provider_identifier": "example", "state": "connected"}]}
        payload = {"account_id": "acct_" + "a" * 32, "text": "hello"}
        with patch("host.tools.browser.client.request", return_value=selected):
            pending = BUNDLED_TOOL.execute("x_post_tweet", payload, api)
        approved = api.approvals.approve(pending.approval_id)
        with patch("host.tools.browser.client.request", side_effect=[selected, BrowserError("Could not confirm publication. Check X before approving another attempt.")]) as request:
            result = BUNDLED_TOOL.execute_approved(approved, api)
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.args[0], "/actions/post_tweet")
        with patch("host.tools.browser.client.request", return_value=selected):
            next_request = BUNDLED_TOOL.execute("x_post_tweet", payload, api)
        self.assertIsInstance(next_request, ActionPendingApproval)
        self.assertNotEqual(next_request.approval_id, pending.approval_id)

    def test_approval_rechecks_account_and_permission(self):
        api = FakeHostAPI()
        selected = {"accounts": [{"account_id": "a" * 32, "provider": "x", "provider_identifier": "example", "state": "connected"}]}
        with patch("host.tools.browser.client.request", return_value=selected):
            pending = BUNDLED_TOOL.execute("x_post_tweet", {"account_id": "a" * 32, **{key: value for key, value in self.body.items() if key != "provider_identifier"}}, api)
        approved = api.approvals.approve(pending.approval_id)
        for changes in ({"provider_identifier": "other"}, {"provider": "linkedin"}, {"state": "needs_attention"}):
            blocked = {"accounts": [{**selected["accounts"][0], **changes}]}
            with patch("host.tools.browser.client.request", return_value=blocked) as request:
                self.assertIsInstance(BUNDLED_TOOL.execute_approved(approved, api), ActionFailed)
                request.assert_called_once_with("/actions/list")

    def test_tool_returns_only_schema_metadata(self):
        state = {"accounts": [{"account_id": "abc", **self.engine.status()}]}
        with patch("host.tools.browser.client.request", return_value=state):
            result = BUNDLED_TOOL.execute("x_connection_status", {}, FakeHostAPI())
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, MANIFEST, "x_connection_status", result)
        self.assertNotIn("lease", json.dumps(result.result))


if __name__ == "__main__":
    unittest.main()
