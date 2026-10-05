"""Preparation failures identify the step without leaking page/provider content."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError

from host.runtime.browser.actions import x_post_tweet as posts
from host.runtime.browser.accounts import Profile
from host.runtime.browser.client import BrowserError
from host.runtime.host_diagnostics_collector.collector import parse_journal_record
from browser_fakes import MemoryStore


class BrowserPostDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.page.url = "https://x.com/compose/post?private-live-token"
        self.page.locator.return_value.count.return_value = 0
        self.page.locator.return_value.filter.return_value.count.return_value = 0
        self.page.get_by_role.return_value.count.return_value = 0
        self.page.get_by_role.return_value.get_by_test_id.return_value.count.return_value = 0
        self.page.goto.return_value = SimpleNamespace(status=200)
        self.browser = Mock(page=self.page)
        self.browser.save_state.return_value = {"cookies": [], "origins": []}
        self.store = MemoryStore()
        self.profile = Profile("acct_" + "a" * 32, "x", Mock(return_value=self.browser), self.store,
                               {"provider_identifier": "example", "state": "connected", "checked_at": "", "usage": {}})
        self.store.save_account(self.profile.account_id, "x", self.profile.data, {})
        self.verify = self.enterContext(patch.object(posts.x, "verify_account", return_value="example"))
        self.expect = self.enterContext(patch("playwright.sync_api.expect"))
        self.submit = self.enterContext(patch.object(posts, "submit_prepared_post"))
        self.emit = self.enterContext(patch.object(posts.host_errors, "emit_record"))
        self.body = {"provider_identifier": "example", "text": "Approved exact text"}

    def assert_failed_step(self, step, body=None, failure_type="TimeoutError"):
        with self.assertRaises(BrowserError) as caught:
            posts.execute(self.profile, body or self.body)
        self.assertIn("Post was not submitted", str(caught.exception))
        self.assertIn(step, str(caught.exception))
        if failure_type != "BrowserError":
            self.assertIn(failure_type, str(caught.exception))
        self.assertEqual(self.profile.data["usage"], {})
        self.submit.assert_not_called()
        record = self.emit.call_args.args[0]
        self.assertEqual({key: record["context"][key] for key in ("stage", "step", "failure_type")},
                         {"stage": "prepare", "step": step, "failure_type": failure_type})
        self.assertGreaterEqual(record["context"]["preparation_elapsed_ms"], 0)
        self.assertEqual(record["context"]["host"], "x.com")
        self.assertIn(step, record["summary"])
        self.assertNotIn("private-live-token", json.dumps(record))
        self.assertNotIn("private-live-token", str(caught.exception))
        _, event = parse_journal_record(json.dumps({"__REALTIME_TIMESTAMP": "1000000",
            "_SYSTEMD_UNIT": "kern-browser.service", "MESSAGE": json.dumps(record)}))
        self.assertEqual(event["severity"], "warning")
        self.assertEqual(event["context"]["step"], step)

    def test_navigation_failure_identifies_post_or_reply_page(self):
        for reply_id in ("", "12345"):
            with self.subTest(reply_id=reply_id):
                self.page.goto.side_effect = TimeoutError("private-live-token")
                self.assert_failed_step("navigate_to_target" if reply_id else "navigate_to_composer",
                                        {**self.body, "in_reply_to_tweet_id": reply_id} if reply_id else self.body)

    def test_live_account_timeout_and_changed_account_are_specific(self):
        self.verify.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("verify_account")
        self.verify.side_effect = None
        self.verify.return_value = "different"
        self.assert_failed_step("verify_account", failure_type="BrowserError")
        self.assertIn("signed-in account changed", self.emit.call_args.args[0]["summary"])

    def test_reply_target_and_reply_button_failures_are_distinct(self):
        target = self.page.locator.return_value.filter.return_value
        with patch.object(self.expect.return_value, "to_have_count", side_effect=AssertionError("private-live-token")):
            self.assert_failed_step("find_reply_target", {**self.body, "in_reply_to_tweet_id": "12345"}, "AssertionError")
        target.get_by_test_id.return_value.click.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("open_reply_composer", {**self.body, "in_reply_to_tweet_id": "12345"})

    def test_clear_type_and_disabled_submit_failures_are_distinct(self):
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        for method, step in (("fill", "clear_composer"), ("press_sequentially", "type_post_text")):
            with self.subTest(step=step), patch.object(editor, method, side_effect=TimeoutError("private-live-token")):
                self.assert_failed_step(step)
        self.expect.return_value.to_be_enabled.side_effect = AssertionError("private-live-token")
        self.assert_failed_step("wait_for_submit_enabled", failure_type="AssertionError")

    def test_reply_failure_records_exact_target_and_structural_state(self):
        self.page.url = "https://x.com/someone/status/12345?private-live-token"
        target = self.page.locator.return_value.filter.return_value
        target.count.return_value = 1
        target.is_visible.return_value = True
        reply = target.get_by_test_id.return_value
        reply.count.return_value = 1
        reply.is_visible.return_value = True
        reply.is_enabled.return_value = False
        reply.evaluate.return_value = False
        reply.click.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("open_reply_composer", {**self.body, "in_reply_to_tweet_id": "12345"})
        context = self.emit.call_args.args[0]["context"]
        self.assertEqual(context["reply_target_id"], "12345")
        self.assertEqual(context["target_count"], 1)
        self.assertEqual(context["reply_control_count"], 1)
        self.assertEqual(context["page_route"], "target")
        self.assertFalse(context["reply_enabled"])
        self.assertFalse(context["reply_center_unobstructed"])
        reply.click.assert_called_once()

    def test_snapshot_failure_preserves_original_step_and_partial_facts(self):
        self.page.locator.return_value.count.side_effect = RuntimeError("private-live-token")
        self.page.goto.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("navigate_to_composer")
        self.assertTrue(self.emit.call_args.args[0]["context"]["snapshot_incomplete"])

    def test_incomplete_or_changed_text_is_never_submitted_or_counted(self):
        self.page.wait_for_function.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("verify_post_text", failure_type="BrowserError")
        self.assertIn("did not exactly match the approved post", self.emit.call_args.args[0]["summary"])

    def test_navigation_http_status_is_visible_and_stops_preparation(self):
        self.page.goto.return_value = SimpleNamespace(status=403)
        self.assert_failed_step("navigate_to_composer", failure_type="BrowserError")
        self.assertIn("HTTP 403", self.emit.call_args.args[0]["summary"])
        self.verify.assert_not_called()

    def test_browser_launch_failure_is_specific_and_sanitized(self):
        self.profile.factory.side_effect = OSError("private-live-token")
        with self.assertRaisesRegex(BrowserError, "launch_browser.*OSError") as caught:
            posts.execute(self.profile, self.body)
        self.assertNotIn("private-live-token", str(caught.exception))
        self.assertEqual(self.emit.call_args.args[0]["context"], {
            "stage": "prepare", "step": "launch_browser", "failure_type": "OSError"})
        self.assertEqual(self.profile.data["usage"], {})
        self.submit.assert_not_called()

    def test_successful_preparation_preserves_keyboard_input_and_does_not_submit(self):
        posts.prepare_post(self.page, "example", "Exact text")
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        editor.fill.assert_called_once_with("")
        editor.press_sequentially.assert_called_once_with("Exact text", delay=0, timeout=60000)
        self.expect.return_value.to_be_enabled.assert_called_once_with(timeout=10000)
        self.page.wait_for_function.assert_called_once_with(
            posts.MATCHES_TEXT, arg=[editor.element_handle.return_value, "Exact text"], timeout=10000)
        self.submit.assert_not_called()
        self.emit.assert_not_called()
