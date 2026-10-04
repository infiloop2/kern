"""Provenance, action outcomes, and the trusted notice transport."""
from http import HTTPStatus
import json
from unittest import TestCase
from unittest.mock import patch

from host import agent_messages as messages
from host.runtime.admin_api import service, threads
from host.runtime.admin_api.errors import ApiError
from host.runtime.core.state import events
from host.runtime.workspace import agent_api, agent_notices
from host.runtime.workspace.host_api import WorkspaceError


class InputNoticeTests(TestCase):
    def test_operator_pasted_preamble_remains_an_operator_message(self):
        message = messages.RESTART_MESSAGE
        notice = {"kind": "operator"}
        self.assertEqual(notice, {"kind": "operator"})
        row = [1, "now", "thread.message", "thread-1", 1]
        payload = {"message": message, "source": "user", "notice": notice}
        row += [payload.get(key) for key in events._EVENT_PAYLOAD_COLUMNS]
        self.assertEqual(events._event_dict(row)["payload"]["notice"], notice)

    def test_unattributed_old_messages_are_not_guessed_from_their_text(self):
        for message in (messages.RESTART_MESSAGE, messages.peer_message("app-2", "hello")):
            row = [1, "now", "thread.message", "thread-1", 1]
            payload = {"message": message, "source": "user"}
            row += [payload.get(key) for key in events._EVENT_PAYLOAD_COLUMNS]
            event = events._event_dict(row)
            self.assertEqual(event["event_type"], "thread.message")
            self.assertEqual(event["payload"], payload)

    def test_all_context_preambles_keep_their_trust_and_message_boundaries(self):
        context = messages.memory_context_message("thread-1", [])
        self.assertIn('"identity": {"thread_id": "thread-1"}', context)
        self.assertIn("instruction_authority none", context)
        self.assertEqual(messages.memory_notice([], "diagnostics")["message"], "Self identity and 0 memories injected.")
        pages = [("guide", 2, "When to use this")]
        suggestion = messages.memory_suggestion(pages)
        self.assertIn("- guide: When to use this", suggestion)
        self.assertIn("not a new operator request", suggestion)
        self.assertEqual(messages.suggestion_notice(pages, suggestion)["memory_recall_details"], suggestion)
        handoff = messages.session_handoff_message("User: previous", "tool output", "current")
        self.assertTrue(handoff.endswith("current\n--- END CURRENT USER MESSAGE ---"))
        self.assertEqual(messages.peer_message("app-2", "body"), messages.MESSAGE_HEADER.format(sender="app-2") + "body")


class KernActionNoticeTests(TestCase):
    def test_success_failure_and_read_filtering_use_host_outcomes(self):
        with patch.object(agent_notices, "call_admin_api") as record, patch.object(agent_api.memory, "save_page", return_value={"page_id": "thread-1"}):
            result = agent_api.dispatch_call("PUT", "/agent/self/memory", {"content": "new"}, peer_thread_id="thread-1")
            self.assertEqual(result["status"], 200)
            self.assertEqual(record.call_args.args[1], "/v1/threads/thread-1/notices")
            self.assertEqual(record.call_args.args[2]["summary"], "Saved self memory")
        failure = WorkspaceError(HTTPStatus.LOCKED, "App is locked")
        with patch.object(agent_notices, "call_admin_api") as record, patch.object(agent_api.web_apps, "route_agent", side_effect=failure):
            with self.assertRaises(WorkspaceError) as caught:
                agent_api.dispatch_call("POST", "/agent/apps/app-1/actions", {}, peer_thread_id="app-1")
            self.assertIs(caught.exception, failure)
            self.assertEqual(record.call_args.args[2]["summary"], "Failed: Changed App data. App is locked")
            self.assertIn("App is locked", record.call_args.args[2]["details"])
        with patch.object(agent_notices, "call_admin_api") as record:
            agent_api.dispatch_call("GET", "/agent/identity", None, peer_thread_id="thread-1")
            record.assert_not_called()

    def test_outgoing_message_and_spawn_record_only_after_acceptance(self):
        for path, name, summary in (("/agent/messages", "send_agent_message", "Sent message to Chat agent: <img src=x> hello"),
                                    ("/agent/agents", "spawn_agent", "Started agent: <img src=x> hello")):
            with patch.object(agent_api.agent_messages, name, return_value={"status": "accepted", "thread_id": "thread-2"}), patch.object(agent_notices, "call_admin_api") as record:
                agent_api.dispatch_call("POST", path, {"message": "<img src=x>\nhello", "thread_id": "thread-2"}, peer_thread_id="thread-1")
                notice = record.call_args.args[2]
                self.assertEqual(notice["summary"], summary)
                self.assertIn("thread-2", notice["details"])
                self.assertIn("<img src=x>", notice["details"])

    def test_notice_failure_never_changes_a_completed_tool_result(self):
        with patch.object(agent_api.agent_messages, "send_agent_message", return_value={"status": "accepted"}), patch.object(agent_notices, "call_admin_api", side_effect=OSError("notice transport unavailable")), patch.object(agent_notices.host_errors, "report_warning") as warning:
            result = agent_api.dispatch_call("POST", "/agent/messages", {}, peer_thread_id="thread-1")
            self.assertEqual(result["body"]["status"], "accepted")
            warning.assert_called_once()

    def test_large_details_have_an_explicit_encoded_size_bound(self):
        with patch.object(agent_notices, "call_admin_api") as record:
            agent_notices.record("app-1", "POST", "/agent/apps/app-1/actions", {"text": "😀\\\"" * 100_000}, result={}, error=None)
            notice = record.call_args.args[2]
            self.assertLess(len(json.dumps(notice).encode()), 24 * 1024)
            self.assertIn("[details truncated]", notice["details"])

    def test_operator_cannot_inject_host_notice_metadata(self):
        from types import SimpleNamespace
        for path, body in (("/v1/threads/thread-1/notices", {}), ("/v1/threads/thread-1/messages", {"kern_notice": {"kind": "restart", "summary": "Restart"}})):
            request = SimpleNamespace(path=path, body=body, principal=None, method="POST")
            with self.assertRaises(ApiError) as caught:
                service._thread_route_request(request)
            self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)

    def test_unknown_kinds_and_summaries_over_100_characters_are_rejected(self):
        for kind, summary in (("kern_action", "Old catch-all"), ("app_ui_published", "x" * 101)):
            with self.subTest(kind=kind), self.assertRaises(ApiError):
                threads.thread_route("POST", "/v1/threads/thread-1/notices", {},
                                     {"kind": kind, "summary": summary, "details": "details"}, None, False)
        with self.assertRaises(ApiError) as rejected:
            threads.send_thread_message("thread-1", {"message": "untyped host input"}, None, False)
        self.assertIn("specific notice kind", str(rejected.exception))

    def test_notice_route_persists_a_separate_event_without_starting_a_turn(self):
        notice = {"kind": "agent_message_sent", "summary": "Sent message to Chat agent: <img src=x> hello", "details": "hello"}
        with patch.object(threads.state, "mutation"), patch.object(threads.state, "thread_session_config", return_value={}), patch.object(threads.state, "append_agent_event") as append, patch.object(threads.orchestrator, "admit_turn") as admit:
            result = threads.thread_route("POST", "/v1/threads/thread-1/notices", {}, notice, None, False)
            self.assertEqual(result, {"status": "recorded"})
            self.assertEqual(append.call_args.args[1:], ("thread.notice", "thread-1", {"notice": notice}))
            admit.assert_not_called()


class NoticeSummaryTests(TestCase):
    def test_resource_and_change_summaries_cover_every_action_kind(self):
        cases = [
            ("POST", "/agent/messages", {"thread_id": "app-1", "message": "Review the release"}, {}, "agent_message_sent", "Sent message to Portfolio: Review the release"),
            ("POST", "/agent/agents", {"name": "Reviewer", "model": "gpt-6.1-sol", "message": "Check changes"}, {}, "agent_spawned", "Started Reviewer (gpt-6.1-sol): Check changes"),
            ("POST", "/agent/agents/archive", {"thread_id": "thread-2"}, {}, "agent_archived", "Archived Portfolio"),
            ("PUT", "/agent/self/memory", {"description": "Release decisions"}, {}, "self_memory_saved", "Saved self memory: Release decisions"),
            ("PUT", "/agent/memory/pages/release-guidance", {}, {}, "shared_memory_saved", "Saved release guidance memory"),
            ("DELETE", "/agent/memory/pages/release-guidance", None, {}, "shared_memory_deleted", "Deleted release guidance memory"),
            ("POST", "/agent/schedules", {"name": "Release checker", "triggers": []}, {}, "standing_agent_created", "Created standing agent Release checker: 0 triggers"),
            ("PUT", "/agent/schedules/1", {"name": "Release checker", "triggers": [{"prompt": "Check releases"}]}, {}, "standing_agent_updated", "Updated standing agent Release checker: 1 trigger: Check releases"),
            ("DELETE", "/agent/schedules/1", None, {}, "standing_agent_deleted", "Deleted standing agent Portfolio"),
            ("POST", "/agent/apps", {}, {"app": {"name": "Portfolio"}}, "app_created", "Created Portfolio"),
            ("PUT", "/agent/apps/app-1/name", {"name": "Portfolio"}, {}, "app_renamed", "Renamed App to Portfolio"),
            ("PUT", "/agent/apps/app-1/agent-settings", {"agent_runtime": "codex", "model": "gpt-6.1-sol", "effort": "high"}, {}, "app_agent_updated", "Updated Portfolio agent: codex · gpt-6.1-sol · high"),
            ("POST", "/agent/apps/app-1/actions", {"action": "publish_ui"}, {}, "app_ui_published", "Published Portfolio UI"),
            ("POST", "/agent/apps/app-1/actions", {"action": "set", "path": ["positions", 0, "status"]}, {}, "app_data_changed", "Changed Portfolio data: 1 set (positions.0.status)"),
            ("POST", "/agent/apps/app-1/collections/holdings/actions", {"operations": [{"action": "upsert"}, {"action": "upsert"}, {"action": "delete"}]}, {}, "app_collection_changed", "Updated Portfolio / holdings: 2 saved, 1 deleted"),
        ]
        self.assertEqual({case[4] for case in cases}, messages.ACTION_KINDS)
        with patch.object(agent_notices, "thread_name", return_value="Portfolio"):
            for method, path, body, response, kind, label in cases:
                with self.subTest(kind=kind):
                    self.assertEqual(agent_notices.action_summary(method, path, body, response, None), (kind, label))
                    failure = agent_notices.action_summary(method, path, body, response, ValueError("Request rejected"))
                    self.assertEqual(failure[0], kind)
                    self.assertTrue(failure[1].startswith("Failed:"))
                    self.assertLessEqual(len(failure[1]), 100)
        self.assertIsNone(agent_notices.action_summary("POST", "/agent/apps/app-1/collections/holdings/query", {}, {}, None))

    def test_summary_truncation_preserves_failure_and_details_preserve_error(self):
        with patch.object(agent_notices, "thread_name", return_value="Portfolio"), patch.object(agent_notices, "call_admin_api") as record:
            agent_notices.record("app-1", "POST", "/agent/apps/app-1/actions",
                                 {"action": "publish_ui", "html": "😀" * 100_000}, result=None, error=ValueError("App locked"))
            notice = record.call_args.args[2]
            self.assertTrue(notice["summary"].startswith("Failed:"))
            self.assertIn("App locked", notice["details"])
            self.assertLessEqual(len(json.dumps(notice).encode()), 24 * 1024)
        summary = messages.peer_notice("Reviewer", "<literal>\n" * 5000)["summary"]
        self.assertLessEqual(len(summary), 100)
        self.assertNotIn("\n", summary)
        self.assertTrue(summary.endswith("…"))

    def test_incoming_notices_use_source_metadata_and_keep_results_out_of_summary(self):
        self.assertEqual(messages.scheduled_notice("Release checker", "Check open releases")["summary"], "Scheduled trigger for Release checker: Check open releases")
        for status, label in (("executed", "Approved and completed"), ("failed", "Approved, execution failed"), ("denied", "Denied")):
            notice = messages.approval_notice({"status": status, "summary": "Publish reviewed post", "result": "Huge tool JSON"})
            self.assertEqual(notice["summary"], f"{label}: Publish reviewed post")
            self.assertNotIn("Huge tool JSON", notice["summary"])
