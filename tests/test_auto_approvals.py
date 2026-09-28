"""Policy authorization, scheduled decisions, execution races and provider egress."""
from http import HTTPStatus
import json
import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.admin_api import auto_approvals as worker, service
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import state
from host.runtime.admin_api import auto_approval_prompt as auto_approval
from host.runtime.host_inference import api, providers, usage

NOW = 14 * 3600
RECORD = {"approval_id": "approval_1.token", "tool_id": "gmail", "action_id": "send_email",
          "status": "pending", "summary": "Follow up", "payload": {"to": "customer@example.test", "body": "Hello"},
          "connection_id": "work", "account_id": "account", "account_label": "Support"}
POLICY = {"tool_id": "gmail", "action_id": "send_email", "instructions": "Routine follow-ups only"}


class AutoApprovalTests(unittest.TestCase):
    def setUp(self):
        # Use a real catalog action, rather than requiring a particular provider's naming.
        choice = worker.catalog()[0]
        self.record = {**RECORD, "tool_id": choice["tool_id"], "action_id": choice["actions"][0]["id"]}
        self.policy = {**POLICY, "tool_id": self.record["tool_id"], "action_id": self.record["action_id"]}

    def worker_context(self):
        self.enterContext(patch.object(worker.time, "time", return_value=NOW))
        self.enterContext(patch.object(state, "pending_auto_approvals", return_value=[self.record]))
        self.enterContext(patch.object(state, "tool_approval", return_value=self.record))
        self.enterContext(patch.object(state, "auto_approval_policies", return_value=[self.policy]))
        self.enterContext(patch.object(state, "host_inference_provider_metadata", return_value={"enabled": True, "configured": True}))
        self.save = self.enterContext(patch.object(state, "save_auto_approval_review"))
        self.error = self.enterContext(patch.object(state, "save_auto_approval_error"))
        self.decide = self.enterContext(patch.object(worker.tools_client, "decide_tool_approval"))
        self.judge = self.enterContext(patch.object(worker.client, "openai_text_completion", return_value={"approve": True, "reason": "Matches the policy."}))

    def test_operator_only_routes_and_invalid_scope(self):
        for method, path in (("GET", "/v1/auto-approvals"), ("PUT", "/v1/auto-approvals/policy"),
                             ("DELETE", "/v1/auto-approvals/policy")):
            with self.subTest(path=path), self.assertRaises(ApiError) as error:
                service.route(method, path, {}, {}, principal=service.WorkspacePrincipal())
            self.assertEqual(error.exception.status, 403)
        for body in ({}, {**self.policy, "instructions": " "}, {**self.policy, "action_id": "missing"},
                     {**self.policy, "instructions": "x" * 8001}):
            with self.assertRaises(ApiError):
                worker.save_policy(body)

    def test_schedule_jitter_and_quiet_hours(self):
        with patch.object(worker.random, "randint", side_effect=[1500, 0]):
            self.assertEqual(worker.next_check(23 * 3600 + 55 * 60), 86400 + 8 * 3600)
        with patch.object(worker.random, "randint", return_value=2100):
            self.assertEqual(worker.next_check(NOW), NOW + 2100)

    def test_positive_review_uses_existing_execution_path(self):
        self.worker_context()
        self.decide.side_effect = lambda *args: self.save.assert_called_once()
        worker.run_once()
        self.assertEqual(json.loads(self.judge.call_args.args[0])["request"]["payload"], self.record["payload"])
        self.assertEqual(json.loads(self.judge.call_args.args[0])["request"]["account_label"], "Support")
        self.assertEqual(self.save.call_args.args[3], "approved")
        self.decide.assert_called_once_with(self.record["approval_id"], "approve", self.record["tool_id"])

    def test_browser_batch_finishes_each_execution_before_reviewing_next(self):
        self.record = {**self.record, "tool_id": "browser", "action_id": "x_post_tweet"}
        self.policy = {**self.policy, "tool_id": "browser", "action_id": "x_post_tweet"}
        self.worker_context()
        second = {**self.record, "approval_id": "approval_2.token"}
        records = {record["approval_id"]: record for record in (self.record, second)}
        events = []
        def review(record, policy):
            events.append(("review", record["approval_id"]))
            return {"approve": True, "reason": "Matches."}
        def execute(approval_id, decision, tool):
            self.assertEqual((decision, tool), ("approve", "browser"))
            events.append(("executed", approval_id))
        self.decide.side_effect = execute
        with patch.object(state, "pending_auto_approvals", return_value=list(records.values())), \
             patch.object(state, "tool_approval", side_effect=records.get), \
             patch.object(worker, "review", side_effect=review):
            worker.run_once()
        self.assertEqual(events, [(kind, record["approval_id"])
                                 for record in records.values() for kind in ("review", "executed")])
        self.error.assert_not_called()

    def test_audit_failure_prevents_execution(self):
        self.worker_context()
        self.save.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            worker.run_once()
        self.decide.assert_not_called()

    def test_crash_before_tools_response_keeps_saved_decision(self):
        self.worker_context()
        self.decide.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            worker.run_once()
        self.save.assert_called_once()
        self.assertEqual(self.save.call_args.args[3:5], ("approved", "Matches the policy."))
        self.error.assert_not_called()

    def test_decision_uses_policy_at_review_time_without_rechecking(self):
        self.worker_context()
        for changed in ("policy", "provider", "quiet"):
            with self.subTest(changed=changed), \
                    patch.object(state, "auto_approval_policies", return_value=[self.policy]) as policies, \
                    patch.object(state, "host_inference_provider_metadata", return_value={"enabled": True, "configured": True}) as provider, \
                    patch.object(worker.time, "time", return_value=NOW) as clock:
                def judge(*args, **kwargs):
                    if changed == "policy":
                        policies.return_value = []
                    elif changed == "provider":
                        provider.return_value = {"enabled": False, "configured": True}
                    else:
                        clock.return_value = 0
                    return {"approve": True, "reason": "Matches."}
                self.judge.side_effect = judge
                worker.run_once()
                self.assertEqual(self.save.call_args.args[2:5], (self.policy["instructions"], "approved", "Matches."))
                policies.assert_called_once()
                provider.assert_called_once()
        self.assertEqual(self.decide.call_count, 3)

    def test_decision_errors_are_audited_without_recovery_or_retry(self):
        self.worker_context()
        for status in (HTTPStatus.BAD_GATEWAY, HTTPStatus.CONFLICT):
            with self.subTest(status=status), patch.object(state, "tool_approval", return_value=self.record) as read:
                self.decide.reset_mock()
                self.decide.side_effect = ApiError(status, "decision failed")
                worker.run_once()
                self.decide.assert_called_once()
                read.assert_called_once_with(self.record["approval_id"])
                self.assertEqual(self.save.call_args.args[3:5], ("approved", "Matches the policy."))
                self.error.assert_called_with(self.record["approval_id"], "decision failed")

    def test_negative_review_retains_reason(self):
        self.worker_context()
        self.judge.return_value = {"approve": False, "reason": "Customer status cannot be verified."}
        worker.run_once()
        self.assertEqual(self.save.call_args.args[3:5], ("left_pending", "Customer status cannot be verified."))
        self.decide.assert_not_called()

    def test_no_policy_no_provider_call(self):
        self.worker_context()
        with patch.object(state, "auto_approval_policies", return_value=[]):
            worker.run_once()
        self.assertEqual(self.save.call_args.args[3], "no_policy")
        self.judge.assert_not_called()
        self.decide.assert_not_called()

    def test_quiet_and_already_decided_do_not_review(self):
        self.worker_context()
        with patch.object(worker.time, "time", return_value=0):
            worker.run_once()
        with patch.object(state, "tool_approval", return_value={**self.record, "status": "denied"}):
            worker.run_once()
        self.judge.assert_not_called()
        self.save.assert_not_called()
        self.decide.assert_not_called()

    def test_unavailable_provider_records_each_request_without_inference(self):
        self.worker_context()
        second = {**self.record, "approval_id": "approval_2.token"}
        for enabled, configured, reason in (
            (False, True, "OpenAI Host AI inference is disabled."),
            (True, False, "OpenAI Host AI inference is not configured."),
        ):
            with self.subTest(enabled=enabled, configured=configured), \
                    patch.object(state, "host_inference_provider_metadata", return_value={"enabled": enabled, "configured": configured}), \
                    patch.object(state, "pending_auto_approvals", return_value=[self.record, second]):
                self.save.reset_mock()
                worker.run_once()
                self.assertEqual([call.args[0] for call in self.save.call_args_list], [self.record["approval_id"], second["approval_id"]])
                for call in self.save.call_args_list:
                    self.assertEqual(call.args[3:5], ("failed", reason))
        self.judge.assert_not_called()
        self.decide.assert_not_called()

    def test_configuration_race_records_unavailable_reason(self):
        self.worker_context()
        self.judge.side_effect = worker.client.HostInferenceError("disabled", reason="provider_disabled")
        worker.run_once()
        self.assertEqual(self.save.call_args.args[3:5], ("failed", "OpenAI Host AI inference is disabled or not configured."))
        self.decide.assert_not_called()

    def test_provider_failure_stops_the_batch_without_spending_other_requests(self):
        self.worker_context()
        self.judge.side_effect = worker.client.HostInferenceError("timed out")
        with patch.object(state, "pending_auto_approvals", return_value=[self.record, self.record]), \
             patch.object(worker.host_errors, "report_warning"):
            worker.run_once()
        self.judge.assert_called_once()
        self.save.assert_called_once()
        self.decide.assert_not_called()

    def test_malformed_or_failed_review_leaves_pending_without_retry(self):
        self.worker_context()
        with patch.object(worker.host_errors, "report_warning"):
            for result in ({"approve": "yes", "reason": "ok"}, {"approve": True, "reason": " "}, {}):
                self.judge.return_value = result
                worker.run_once()
                self.assertEqual(self.save.call_args.args[3], "failed")
        self.decide.assert_not_called()
        self.assertEqual(self.judge.call_count, 3)

    def test_lost_execution_response_is_not_replayed(self):
        self.worker_context()
        self.decide.side_effect = TimeoutError("lost response")
        with patch.object(worker.host_errors, "report_warning"):
            worker.run_once()
        self.decide.assert_called_once()
        self.save.assert_called_once()

    def test_oversized_payload_records_specific_error_without_inference(self):
        completion = worker.client.openai_text_completion
        self.worker_context()
        self.judge.side_effect = completion
        oversized = {**self.record, "payload": {"body": "x" * (48 * 1024)}}
        with patch.object(state, "tool_approval", return_value=oversized), \
                patch.object(worker.client, "_request") as request:
            worker.run_once()
        request.assert_not_called()
        self.decide.assert_not_called()
        self.assertEqual(self.save.call_args.args[3:5], (
            "failed", "Tool payload is too large for auto-review with this policy.",
        ))

    def test_sol_provider_transport_and_usage(self):
        captured = {}
        result = {"approve": False, "reason": "The recipient cannot be verified."}
        response = {"model": "gpt-6-sol", "choices": [{"message": {"content": json.dumps(result)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20}}}
        def transport(url, **kwargs):
            captured.update(json.loads(kwargs["data"]))
            captured["timeout"] = kwargs["timeout"]
            return json.dumps(response).encode()
        with patch.object(state, "enabled_host_inference_provider", return_value={"api_key": "sk-test"}), \
             patch.object(providers.provider_http, "post", side_effect=transport), \
             patch.object(usage, "_schedule") as meter, \
             patch.object(worker.client, "_request", side_effect=lambda path, body, *args: api.dispatch(path, body)["result"]):
            actual = worker.review({**self.record, "payload": {"api_key": "sk-hidden"}}, "Only follow-ups")
        self.assertEqual(actual, result)
        self.assertEqual(captured["model"], "gpt-6-sol")
        self.assertEqual(captured["reasoning_effort"], "medium")
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(captured["response_format"]["json_schema"]["schema"], auto_approval.SCHEMA)
        self.assertIn("untrusted", captured["messages"][0]["content"])
        self.assertNotIn("sk-hidden", captured["messages"][1]["content"])
        self.assertEqual(meter.call_args.args[:2], ("openai", "gpt-6-sol"))
        self.assertAlmostEqual(meter.call_args.args[3], 0.000264)


class AutoApprovalStorageTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        self.record = state.insert_tool_approval("fake_notes", "write_note", "Note", {"text": "hello"}, NOW,
                                                 pending_limit=1000, origin_thread_id=None)
        self.approval_id = self.record["approval_id"]
        state.set_auto_approval_policy("fake_notes", "write_note", "Allow notes")

    def test_each_request_is_checked_once_despite_policy_changes(self):
        for index, outcome in enumerate(("left_pending", "failed", "no_policy", "approved")):
            with self.subTest(outcome=outcome):
                record = self.record if index == 0 else state.insert_tool_approval(
                    "fake_notes", "write_note", "Note", {}, NOW, pending_limit=1000, origin_thread_id=None,
                )
                state.set_auto_approval_policy("fake_notes", "write_note", None if outcome == "no_policy" else "Allow notes")
                self.assertEqual([r["approval_id"] for r in state.pending_auto_approvals()], [record["approval_id"]])
                state.save_auto_approval_review(record["approval_id"], NOW, "" if outcome == "no_policy" else "Allow notes",
                                                outcome, "Checked", "gpt-6-sol")
                self.assertEqual(state.pending_auto_approvals(), [])
                for policy in ("Changed instructions", None, "Allow notes"):
                    state.set_auto_approval_policy("fake_notes", "write_note", policy)
                    self.assertEqual(state.pending_auto_approvals(), [])

    def test_decision_and_call_error_are_separate_from_tool_status(self):
        state.save_auto_approval_review(self.approval_id, NOW, "Allow notes", "approved", "Matches", "gpt-6-sol")
        state.save_auto_approval_error(self.approval_id, "tools service unavailable")
        self.assertEqual(state.pending_auto_approvals(), [])
        row = state.auto_approval_history(1, "approved")["items"][0]
        self.assertEqual((row["outcome"], row["reason"], row["status"]), ("approved", "Matches", "pending"))
        self.assertEqual(row["approval_error"], "tools service unavailable")
        item = state.page_approvals("pending", 1)["items"][0]
        self.assertEqual(item["auto_review"]["approval_error"], "tools service unavailable")

    def test_batch_is_bounded_and_advances_after_completed_checks(self):
        for i in range(25):
            state.insert_tool_approval("fake_notes", "write_note", f"Note {i}", {}, NOW,
                                       pending_limit=1000, origin_thread_id=None)
        first = state.pending_auto_approvals()
        self.assertEqual(len(first), 20)
        self.assertEqual(first[0]["approval_id"], self.approval_id)
        for record in first:
            state.save_auto_approval_review(record["approval_id"], NOW, "Allow notes", "left_pending", "Missing evidence")
        second = state.pending_auto_approvals()
        self.assertEqual(len(second), 6)
        self.assertFalse({r["approval_id"] for r in first} & {r["approval_id"] for r in second})

    def test_upsert_delete_last_check_and_snapshot(self):
        state.set_auto_approval_policy("fake_notes", "write_note", "Only short notes")
        self.assertEqual(len(state.auto_approval_policies()), 1)
        state.save_auto_approval_review(self.approval_id, NOW, "Only short notes", "left_pending", "Too long", "gpt-6-sol")
        state.set_auto_approval_policy("fake_notes", "write_note", None)
        item = state.page_approvals("pending", 1)["items"][0]
        self.assertFalse(item["has_auto_policy"])
        self.assertEqual(item["auto_review"]["reason"], "Too long")
        self.assertEqual(state.auto_approval_history(1, "left_pending")["items"][0]["policy"], "Only short notes")
        self.assertEqual(state.auto_approval_history(1, "approved")["total"], 0)
