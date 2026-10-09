"""Preparation failures identify the step without leaking page/provider content."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError

from host.runtime.browser.actions import x_post_tweet as posts
from host.runtime.browser.actions import x_diagnostics as diagnostics
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
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        editor.filter.return_value = editor
        self.enterContext(patch.object(posts, "active_composer", return_value=self.page.get_by_role.return_value))
        self.session = self.page.context.new_cdp_session.return_value
        self.session.send.return_value = {"result": {"value": {}}}
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
        self.session.send.side_effect = [
            {"result": {"value": {"reply_center_unobstructed": False, "reply_in_viewport": True,
                "reply_center_hit_tag": "div", "reply_center_hit_role": "dialog", "reply_center_hit_test_id": "mask"}}},
            {"result": {"value": {}}},
        ]
        reply.click.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("open_reply_composer", {**self.body, "in_reply_to_tweet_id": "12345"})
        context = self.emit.call_args.args[0]["context"]
        self.assertEqual(context["reply_target_id"], "12345")
        self.assertEqual(context["target_count"], 1)
        self.assertEqual(context["reply_control_count"], 1)
        self.assertEqual(context["page_route"], "target")
        self.assertFalse(context["reply_enabled"])
        self.assertFalse(context["reply_center_unobstructed"])
        self.assertEqual(context["snapshot_phase"], "after_failure")
        self.assertEqual(context["reply_center_hit_test_id"], "mask")
        reply.click.assert_called_once()

    def test_enriched_snapshot_survives_reporter_and_collector_without_changing_target(self):
        self.page.url = "https://x.com/someone/status/12345?private-live-token"
        expected = {"target_id_link_count": 1, "target_id_article_count": 1,
            "target_id_timestamp_article_count": 1, "target_id_query_link_count": 1,
            "target_id_extra_path_link_count": 0, "target_id_exact_suffix_link_count": 0,
            "target_link_scan_truncated": False, "visible_dialog_count": 0,
            "inline_editor_count": 1, "popup_editor_count": 0, "visible_inline_editor_count": 1,
            "visible_popup_editor_count": 0, "login_input_visible": False, "page_state_scan_truncated": False}
        self.session.send.return_value = {"result": {"value": expected}}
        self.expect.return_value.to_have_count.side_effect = AssertionError("private-live-token")
        self.assert_failed_step("find_reply_target", {**self.body, "in_reply_to_tweet_id": "12345"}, "AssertionError")
        context = self.emit.call_args.args[0]["context"]
        for key, value in expected.items():
            self.assertEqual(context[key], value)
        self.assertEqual(context["target_count"], 0)
        self.assertEqual(context["reply_target_id"], "12345")
        self.page.locator.return_value.filter.return_value.get_by_test_id.return_value.click.assert_not_called()

    def test_page_snapshot_failure_preserves_original_failure_and_does_not_click(self):
        self.session.send.side_effect = RuntimeError("private-live-token")
        self.expect.return_value.to_have_count.side_effect = AssertionError("private-live-token")
        self.assert_failed_step("find_reply_target", {**self.body, "in_reply_to_tweet_id": "12345"}, "AssertionError")
        self.assertTrue(self.emit.call_args.args[0]["context"]["snapshot_incomplete"])
        self.assertEqual(self.emit.call_args.args[0]["context"]["target_count"], 0)
        self.page.locator.return_value.filter.return_value.get_by_test_id.return_value.click.assert_not_called()

    def test_control_snapshot_failure_still_collects_page_state(self):
        target = self.page.locator.return_value.filter.return_value
        target.count.return_value = 1
        reply = target.get_by_test_id.return_value
        reply.count.return_value = 1
        reply.is_visible.return_value = True
        reply.is_enabled.return_value = True
        reply.click.side_effect = TimeoutError("private-live-token")
        self.session.send.side_effect = [RuntimeError("private-live-token"),
            {"result": {"value": {"inline_editor_count": 1}}}]
        self.assert_failed_step("open_reply_composer", {**self.body, "in_reply_to_tweet_id": "12345"})
        context = self.emit.call_args.args[0]["context"]
        self.assertTrue(context["snapshot_incomplete"])
        self.assertEqual(context["inline_editor_count"], 1)
        self.assertEqual(context["target_count"], 1)
        reply.click.assert_called_once()

    def test_page_result_is_validated_before_reporting_and_cannot_overwrite_bound_facts(self):
        self.session.send.return_value = {"result": {"value": {
            "target_id_link_count": 1, "reply_in_viewport": True, "reply_rect_x": -10,
            "reply_center_hit_tag": "button", "body_text": "private-live-token",
            "reply_target_id": "private-live-token", "snapshot_phase": "private-live-token",
            "visible_dialog_count": "private-live-token", "target_id_article_count": True,
            "target_id_query_link_count": -1, "target_id_extra_path_link_count": 2001,
            "reply_rect_y": 100001, "reply_rect_width": 1.5,
            "reply_fully_in_viewport": 1, "reply_center_hit_role": "private-live-token",
            "composer_empty": "private-live-token", "composer_editable": 1, "composer_focused": [],
        }}}
        self.expect.return_value.to_have_count.side_effect = AssertionError("private-live-token")
        self.assert_failed_step("find_reply_target", {**self.body, "in_reply_to_tweet_id": "12345"}, "AssertionError")
        context = self.emit.call_args.args[0]["context"]
        self.assertEqual(context["reply_target_id"], "12345")
        self.assertEqual(context["snapshot_phase"], "after_failure")
        self.assertEqual(context["target_id_link_count"], 1)
        self.assertEqual(context["reply_rect_x"], -10)
        self.assertTrue(context["snapshot_incomplete"])
        for key in ("body_text", "visible_dialog_count", "target_id_article_count",
                    "target_id_query_link_count", "target_id_extra_path_link_count",
                    "reply_rect_y", "reply_rect_width", "reply_fully_in_viewport", "reply_center_hit_role",
                    "composer_empty", "composer_editable", "composer_focused"):
            self.assertNotIn(key, context)

    def test_snapshot_execution_is_bounded_and_exception_details_are_discarded(self):
        self.session.send.return_value = {"exceptionDetails": {"text": "private-live-token"}}
        self.assertEqual(diagnostics._evaluate_facts(self.page, diagnostics.PAGE_FACTS, "12345"),
                         {"snapshot_incomplete": True})
        method, params = self.session.send.call_args.args
        self.assertEqual(method, "Runtime.evaluate")
        self.assertEqual(params["timeout"], 500)
        self.assertTrue(params["returnByValue"])
        self.session.detach.assert_called_once()

    def test_missing_or_nonobject_snapshot_values_are_incomplete(self):
        for value in (None, [], "private-live-token", 1):
            with self.subTest(value=value):
                self.session.send.return_value = {"result": {"value": value}}
                self.assertEqual(diagnostics._evaluate_facts(self.page, diagnostics.PAGE_FACTS, "12345"),
                                 {"snapshot_incomplete": True})

    def test_failed_cdp_cleanup_does_not_mask_snapshot_or_original_failure(self):
        self.session.send.side_effect = RuntimeError("private-live-token")
        self.session.detach.side_effect = RuntimeError("private-live-token")
        self.expect.return_value.to_have_count.side_effect = AssertionError("private-live-token")
        self.assert_failed_step("find_reply_target", {**self.body, "in_reply_to_tweet_id": "12345"}, "AssertionError")
        self.session.detach.assert_called_once()

    def test_snapshot_failure_preserves_original_step_and_partial_facts(self):
        self.page.locator.return_value.count.side_effect = RuntimeError("private-live-token")
        self.page.goto.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("navigate_to_composer")
        self.assertTrue(self.emit.call_args.args[0]["context"]["snapshot_incomplete"])

    def test_incomplete_or_changed_text_is_never_submitted_or_counted(self):
        def check_text(*args, **kwargs):
            if kwargs["arg"][1]:
                raise TimeoutError("private-live-token")
        self.page.wait_for_function.side_effect = check_text
        self.assert_failed_step("verify_post_text", failure_type="BrowserError")
        self.assertIn("did not exactly match the approved post", self.emit.call_args.args[0]["summary"])
        self.page.goto.assert_called_once()

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
        editor.fill.assert_called_once()
        self.assertEqual(editor.fill.call_args.args, ("",))
        self.assertEqual(editor.press_sequentially.call_args.args, ("Exact text",))
        self.assertEqual(editor.press_sequentially.call_args.kwargs["delay"], 0)
        self.assertLessEqual(editor.press_sequentially.call_args.kwargs["timeout"], 60000)
        self.expect.return_value.to_be_editable.assert_called_once()
        self.expect.return_value.to_be_enabled.assert_called_once()
        self.assertEqual([call.kwargs["arg"][1] for call in self.page.wait_for_function.call_args_list],
                         ["", "Exact text"])
        self.submit.assert_not_called()
        self.emit.assert_not_called()

    def test_ready_empty_editor_skips_fill_but_verifies_empty_and_approved_text(self):
        self.session.send.return_value = {"result": {"value": {"composer_empty": True}}}
        posts.prepare_post(self.page, "example", "Exact text")
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        editor.fill.assert_not_called()
        self.assertEqual([call.kwargs["arg"][1] for call in self.page.wait_for_function.call_args_list],
                         ["", "Exact text"])

    def test_nonboolean_empty_snapshot_cannot_skip_clear(self):
        self.session.send.return_value = {"result": {"value": {"composer_empty": "private-live-token"}}}
        posts.prepare_post(self.page, "example", "Exact text")
        self.page.get_by_role.return_value.get_by_test_id.return_value.fill.assert_called_once()

    def test_readiness_timeout_recovers_once_then_counts_and_submits_once(self):
        self.expect.return_value.to_be_editable.side_effect = [AssertionError("private-live-token"), None]
        self.submit.return_value = "https://x.com/example/status/123"
        self.assertEqual(posts.execute(self.profile, self.body)["status"], "posted")
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(self.verify.call_count, 2)
        self.assertEqual(self.profile.data["usage"]["x_post_tweet"]["count"], 1)
        self.submit.assert_called_once_with(self.page, "example", "")
        self.page.get_by_role.return_value.get_by_test_id.return_value.press_sequentially.assert_called_once()
        self.emit.assert_not_called()

    def test_partial_typing_is_cleared_on_recovery_without_an_extra_submission(self):
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        editor.press_sequentially.side_effect = [TimeoutError("private-live-token"), None]
        posts.execute(self.profile, self.body)
        self.assertEqual(editor.fill.call_count, 2)
        self.assertEqual([call.args[0] for call in editor.press_sequentially.call_args_list],
                         [self.body["text"], self.body["text"]])
        self.submit.assert_called_once()
        self.assertEqual(self.profile.data["usage"]["x_post_tweet"]["count"], 1)

    def test_failed_recovery_retains_initial_step_and_never_counts_or_submits(self):
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        editor.fill.side_effect = TimeoutError("private-live-token")
        self.assert_failed_step("clear_composer")
        self.assertEqual(self.page.goto.call_count, 2)
        context = self.emit.call_args.args[0]["context"]
        self.assertEqual(context["preparation_attempt"], 2)
        self.assertEqual(context["recovery_step"], "clear_composer")
        self.assertEqual(context["recovery_failure_type"], "TimeoutError")
        self.assertGreaterEqual(context["step_elapsed_ms"], 0)

    def test_recovery_rechecks_account_and_exact_reply_target(self):
        self.expect.return_value.to_be_editable.side_effect = [AssertionError("private-live-token"), None]
        posts.execute(self.profile, {**self.body, "in_reply_to_tweet_id": "12345"})
        self.assertEqual([call.args[0] for call in self.page.goto.call_args_list],
                         ["https://x.com/i/status/12345"] * 2)
        self.assertEqual(self.verify.call_count, 2)
        self.assertEqual(self.page.locator.return_value.filter.return_value.get_by_test_id.return_value.click.call_count, 2)
        self.submit.assert_called_once_with(self.page, "example", "12345")

    def test_account_change_during_recovery_stops_before_typing_or_submit(self):
        self.expect.return_value.to_be_editable.side_effect = AssertionError("private-live-token")
        self.verify.side_effect = ["example", "different"]
        self.assert_failed_step("verify_account", failure_type="BrowserError")
        self.page.get_by_role.return_value.get_by_test_id.return_value.press_sequentially.assert_not_called()

    def test_deadline_exhaustion_does_not_start_recovery_or_use_zero_timeout(self):
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        clock = [0.0]
        def expire(*args, **kwargs):
            clock[0] = 61.0
            raise TimeoutError(self.page.url.split("?")[1])
        editor.fill.side_effect = expire
        with patch.object(posts.time, "monotonic", side_effect=lambda: clock[0]):
            self.assert_failed_step("clear_composer")
        self.page.goto.assert_called_once()
        self.assertEqual(self.emit.call_args.args[0]["context"]["preparation_attempt"], 1)
        for mock in (self.page.goto, self.verify, editor.fill):
            for call in mock.call_args_list:
                self.assertGreater(call.kwargs["timeout"], 0)
                self.assertLessEqual(call.kwargs["timeout"], posts.PREPARATION_TIMEOUT_MS)

    def test_confirmation_timeout_never_reopens_composer_or_repeats_submit(self):
        self.submit.side_effect = TimeoutError("private-live-token")
        with self.assertRaisesRegex(BrowserError, "may have been published"):
            posts.execute(self.profile, self.body)
        self.page.goto.assert_called_once()
        self.submit.assert_called_once()
        self.assertEqual(self.profile.data["usage"]["x_post_tweet"]["count"], 1)

    def test_recovery_shares_the_original_deadline_instead_of_resetting_it(self):
        editor = self.page.get_by_role.return_value.get_by_test_id.return_value
        clock = [0.0]
        def clear(*args, **kwargs):
            if editor.fill.call_count == 1:
                clock[0] = 55.0
                raise TimeoutError(self.page.url.split("?")[1])
        editor.fill.side_effect = clear
        with patch.object(posts.time, "monotonic", side_effect=lambda: clock[0]):
            posts.execute(self.profile, self.body)
        self.assertEqual([call.kwargs["timeout"] for call in self.page.goto.call_args_list], [20000, 5000])
        self.assertEqual(editor.press_sequentially.call_args.kwargs["timeout"], 5000)
        self.submit.assert_called_once()
