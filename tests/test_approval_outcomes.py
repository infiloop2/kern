"""Approval identity and one-shot ordinary thread messages."""

from http import HTTPStatus
from pathlib import Path
import json
import socket
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pg_harness
from host.runtime.admin_api import approval_outcomes as outcomes, threads, tools_client
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import peer_identity, state
from host.runtime.workspace import agent_messages, service as workspace_service
from host.runtime.tools import api as tools_api, tools_host


def to_workspace(method, path, query, body):
    """Exercise the internal HTTP handler without binding a listening port."""
    client, server = socket.socketpair()
    with client, server:
        payload = json.dumps(body).encode()
        client.sendall(f"{method} {path} HTTP/1.1\r\nHost: workspace\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)
        workspace_service.Handler(server, ("127.0.0.1", 0), None)
        server.close()
        raw = b""
        while chunk := client.recv(65536):
            raw += chunk
    head, _, data = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    result = json.loads(data)
    if status >= 400:
        raise ApiError(HTTPStatus(status), result["error"]["message"])
    return result


def to_admin(method, path, body):
    from host.runtime.admin_api.workspace_api import route_workspace_request
    return route_workspace_request(method, path, {}, body)


def record(status="executed"):
    return {"approval_id": "approval_1.token", "tool_id": "gmail", "action_id": "send",
            "summary": "Send the reviewed email", "status": status, "result": "Sent.",
            "origin_thread_id": "thread-34"}


class ApprovalOutcomeTests(unittest.TestCase):
    def test_messages_contain_only_the_decision_and_execution_outcome(self):
        expected = {
            "executed": "was approved and executed successfully.\n\nResult: Sent.",
            "denied": "was denied.",
            "failed": "was approved, but execution failed.\n\nError: Sent.",
        }
        for status, text in expected.items():
            self.assertEqual(outcomes.outcome_message(record(status)),
                             "This is an automated message from Kern.\n\n---\n\n"
                             f"Approval ID: approval_1.token {text}")

    def test_execution_details_match_the_result_exposed_by_mcp(self):
        for status, text, label in [
            ("executed", "Sent message msg-123.", "Result"),
            ("failed", "Provider rejected the request.\nQuota exceeded.", "Error"),
        ]:
            item = record(status) | {"result": text}
            with patch.object(state, "tool_approval", return_value=item):
                result = tools_api.call_action("check_tool_approval", {"approval_id": item["approval_id"]}, origin_thread_id=None)
            self.assertTrue(outcomes.outcome_message(item).endswith(
                label + ": " + result["result"]["execution_result"]))

    def test_failure_message_fits_transport(self):
        message = outcomes.outcome_message(record("failed") | {"result": "😀" * 10000})
        self.assertLess(len(message.encode()), threads.MESSAGE_LIMIT)

    def test_peer_identity_comes_only_from_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "42").mkdir()
            cgroup = root / "42/cgroup"
            for text, expected in [("0::/system.slice/kern-agent-thread-thread-34.scope", "thread-34"),
                                   ("0::/user.slice/thread-34", None),
                                   ("0::/kern-agent-thread-thread-34.scope/child", None)]:
                cgroup.write_text(text)
                self.assertEqual(peer_identity.peer_thread_id(42, root), expected)
            self.assertIsNone(peer_identity.peer_thread_id(43, root))

    def test_caller_cannot_supply_origin_in_action_envelope(self):
        with self.assertRaises(tools_host.ToolCallError):
            tools_api.call_action("call_tool", {"tool_id": "gmail", "action_id": "send",
                                               "origin_thread_id": "thread-9"}, origin_thread_id=None)

    def test_operator_decision_sends_one_normal_message_after_result(self):
        for status in outcomes.TERMINAL_STATUSES:
            result = {"approval": record(status)}
            with patch.object(state, "load_cloudflare_hostname", return_value=None), patch.object(tools_client, "_tools_operator_request", return_value=result), patch.object(outcomes.workspace_proxy, "send_message", return_value={"status": "accepted"}) as send:
                actual = tools_client.decide_tool_approval("approval_1.token", "deny" if status == "denied" else "approve", "gmail")
                self.assertIs(actual, result)
                send.assert_called_once_with("thread-34", outcomes.outcome_message(record(status)))

    def test_missing_identity_and_nonterminal_outcomes_do_not_send(self):
        for item in (record() | {"origin_thread_id": None}, record("pending"), record("approved")):
            with patch.object(outcomes.workspace_proxy, "send_message") as send:
                outcomes.notify(item)
                send.assert_not_called()

    def test_failed_delivery_preserves_approval_result_without_retry(self):
        result = {"approval": record()}
        with patch.object(state, "load_cloudflare_hostname", return_value=None), patch.object(tools_client, "_tools_operator_request", return_value=result), patch.object(outcomes.workspace_proxy, "send_message", side_effect=ApiError(HTTPStatus.CONFLICT, "busy")) as send, patch.object(outcomes.host_errors, "report_warning") as warning:
            self.assertIs(tools_client.decide_tool_approval("approval_1.token", "approve", "gmail"), result)
            send.assert_called_once()
            warning.assert_called_once()

    def test_rejected_decision_does_not_notify(self):
        with patch.object(state, "load_cloudflare_hostname", return_value=None), patch.object(tools_client, "_tools_operator_request", side_effect=ApiError(HTTPStatus.CONFLICT, "already decided")), patch.object(outcomes.workspace_proxy, "send_message") as send:
            with self.assertRaises(ApiError):
                tools_client.decide_tool_approval("approval_1.token", "approve", "gmail")
            send.assert_not_called()

    def test_authenticated_origin_reaches_tool_approval_request(self):
        from test_tools_host import FAKE_MANIFEST
        with patch.object(state, "insert_tool_approval", return_value=record() | {"action_id": "write_note", "payload": {}, "created_at": 1, "decided_at": 0}) as insert:
            approvals = tools_host.HostApprovals(FAKE_MANIFEST, tools_host.NO_CONNECTION, "thread-34")
            approvals.request(action_id="write_note", summary="Reviewed email", payload={})
            self.assertEqual(insert.call_args.kwargs["origin_thread_id"], "thread-34")


class ApprovalOriginDatabaseTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def test_origin_survives_approval_execution_and_historical_rows_remain_valid(self):
        for origin in ("thread-34", "app-1", "schedule-3", None):
            item = state.insert_tool_approval("gmail", "send", "Reviewed email", {}, 1,
                                             pending_limit=100, origin_thread_id=origin)
            self.assertEqual(item["origin_thread_id"], origin)
            state.transition_tool_approval(item["approval_id"], "pending", "approved", 2)
            state.transition_tool_approval(item["approval_id"], "approved", "executed", 3, result="Sent.")
            self.assertEqual(state.tool_approval(item["approval_id"])["origin_thread_id"], origin)

    def test_tool_call_captures_host_origin_through_execution(self):
        from test_tools_host import FakeTool
        with state.mutation() as cur:
            state.set_tool_enabled(cur, "fake_notes", True)
        with patch.dict(tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()}):
            pending = tools_api.call_action("call_tool", {
                "tool_id": "fake_notes", "action_id": "write_note", "input": {"text": "hello"},
            }, origin_thread_id="thread-34")
        self.assertEqual(pending["status"], "pending_approval")
        self.assertEqual(state.tool_approval(pending["approval_id"])["origin_thread_id"], "thread-34")


class ApprovalDestinationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(outcomes.workspace_proxy, "_proxy", side_effect=to_workspace))
        self.enterContext(patch.object(agent_messages, "call_admin_api", side_effect=to_admin))

    def test_host_notifications_use_existing_destination_guards(self):
        from host.runtime.core import db
        cases = [
            ("thread-34", (True,)),
            ("app-1", ("codex", "gpt-6-astra", "high", True, False)),
            ("app-1", ("codex", "gpt-6-astra", "high", False, True)),
            ("schedule-1", None),
            ("schedule-1", ("script", "bash", "fixed")),
        ]
        for target, row in cases:
            with self.subTest(target=target, row=row), patch.object(db, "transaction") as transaction, patch.object(threads, "send_thread_message") as send, patch.object(outcomes.host_errors, "report_warning") as warning:
                transaction.return_value.__enter__.return_value.fetchone.return_value = row
                outcomes.notify(record() | {"origin_thread_id": target})
                send.assert_not_called()
                warning.assert_called_once()

    def test_host_notifications_use_current_app_runtime_settings(self):
        from host.runtime.core import db
        with patch.object(db, "transaction") as transaction, patch.object(threads, "send_thread_message", return_value={"status": "accepted"}) as send:
            transaction.return_value.__enter__.return_value.fetchone.return_value = ("codex-2", "gpt-6-astra", "high", False, False)
            outcomes.notify(record() | {"origin_thread_id": "app-1"})
            self.assertEqual(send.call_args.args[1]["agent_runtime"], "codex-2")

    def test_agent_and_approval_messages_use_the_same_workspace_checks(self):
        from host.runtime.core import db
        with patch.object(db, "transaction") as transaction, patch.object(
            agent_messages, "call_admin_api", side_effect=to_admin
        ) as request, patch.object(threads, "send_thread_message", return_value={"status": "accepted"}) as send:
            transaction.return_value.__enter__.return_value.fetchone.return_value = (False,)
            result = agent_messages.send_agent_message(
                {"thread_id": "thread-34", "message": "Review complete."}, sender_thread_id="app-2"
            )
            self.assertEqual(result, {"status": "accepted", "thread_id": "thread-34"})
            request.assert_called_once()
            self.assertIn("Sender thread: app-2", send.call_args.args[1]["message"])
            outcomes.notify(record())
            self.assertEqual(send.call_count, 2)
            self.assertIn("Approval ID: approval_1.token", send.call_args.args[1]["message"])


class GitHubApprovalOutcomeTests(unittest.TestCase):
    def push(self, status="approved"):
        return {"id": "abc123", "owner": "org", "repo": "repo", "status": status,
                "ref_updates": [{"ref": "refs/heads/thread-34/feature"}],
                "origin_thread_id": "thread-34"}

    def test_push_outcomes_send_the_shared_header_and_push_id(self):
        for status, expected in (("approved", "executed"), ("rejected", "denied"), ("failed", "failed")):
            with patch.object(outcomes.workspace_proxy, "send_message") as send:
                outcomes.notify_push(self.push(status))
                send.assert_called_once()
                target, message = send.call_args.args
                self.assertEqual(target, "thread-34")
                self.assertTrue(message.startswith("This is an automated message from Kern.\n\n---\n\n"))
                self.assertIn("Approval ID: push-abc123", message)
                self.assertIn({"executed": "was approved and executed successfully.",
                               "failed": "was approved, but execution failed.",
                               "denied": "was denied."}[expected], message)
                if status != "failed":
                    self.assertNotIn("The push failed.", message)

    def test_admin_push_route_notifies_success_and_only_new_terminal_failures(self):
        from host.runtime.admin_api import service
        from host.network_integrations.github.push_gate import pending
        with patch.object(pending, "approve", return_value=self.push()), patch.object(outcomes.workspace_proxy, "send_message") as send:
            service.resolve_pending_push("abc123", "approve")
            send.assert_called_once()
        failed = self.push("failed") | {"detail": "lease rejected"}
        for error, expected_sends in ((pending.PendingPushError("lease rejected", resolved_push=failed), 1),
                                      (pending.PendingPushError("pending push is already failed"), 0)):
            with patch.object(pending, "approve", side_effect=error), patch.object(outcomes.workspace_proxy, "send_message") as send:
                with self.assertRaises(ApiError):
                    service.resolve_pending_push("abc123", "approve")
                self.assertEqual(send.call_count, expected_sends)

    def test_tcp_identity_matches_the_client_socket_tuple(self):
        connection = Mock()
        connection.getpeername.return_value = ("127.0.0.1", 49123)
        connection.getsockname.return_value = ("127.0.0.1", 7445)
        for output, expected in (("0 0 127.0.0.1:49123 127.0.0.1:7445 cgroup:/kern_agent.slice/kern-agent-thread-thread-34.scope\n", "thread-34"),
                                 ("0 0 127.0.0.1:49123 127.0.0.1:7445 cgroup:/system.slice/kern-tools.service\n", None),
                                 ("", None)):
            with patch.object(peer_identity.subprocess, "run", return_value=SimpleNamespace(stdout=output)) as run:
                self.assertEqual(peer_identity.tcp_peer_thread_id(connection), expected)
                self.assertEqual(run.call_args.args[0][-1],
                                 "src 127.0.0.1 and sport = :49123 and dst 127.0.0.1 and dport = :7445")
        with patch.object(peer_identity.subprocess, "run", side_effect=subprocess.TimeoutExpired("ss", 2)):
            self.assertIsNone(peer_identity.tcp_peer_thread_id(connection))

    def test_push_gate_keeps_the_authenticated_origin(self):
        from contextlib import nullcontext
        from host.config import parse_network_controls
        from host.network_integrations import runtime
        from host.network_integrations.github import guard
        controls = parse_network_controls({"network_integrations": {"github": {
            "enabled": True, "block_direct_main_pushes": False,
            "require_dot_github_approval": True,
            "write_repositories": [{"owner": "org", "repo": "repo"}],
        }}})
        inspected = Mock(touches_github=True, ref_updates=[], paths={".github/workflows/test.yml"})
        inspected.hold_for_approval.return_value = b"held"
        with patch.object(guard.push_gate, "quarantine_lock", return_value=nullcontext()), patch.object(guard, "count_pending_pushes", return_value=0), patch.object(guard, "read_proxy_github_token", return_value=None), patch.object(guard.push_gate, "inspect", return_value=inspected), patch.object(guard, "enqueue_pending_push") as enqueue:
            response, reason = runtime.gate_response(controls, "POST", "github.com", "/org/repo.git/git-receive-pack", b"body", "thread-34")
            self.assertEqual(reason, "github_push_queued_for_approval")
            self.assertEqual(response, b"held")
            self.assertEqual(enqueue.call_args.kwargs["origin_thread_id"], "thread-34")


class GitHubApprovalDatabaseTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        from host.runtime.agent_runtime import orchestrator
        orchestrator._LIVE.clear()
        self.addCleanup(orchestrator._LIVE.clear)
        state.enqueue_pending_push("abc123", "org", "repo", [{"ref": "refs/heads/feature", "old": "0" * 40, "new": "1" * 40}],
                                   [".github/workflows/test.yml"], origin_thread_id="thread-34")
        state.save_proxy_github_token("ghs_working")

    def test_push_result_starts_idle_thread_then_steers_active_thread(self):
        from host.runtime.admin_api import service
        from host.runtime.agent_runtime import orchestrator
        from host.network_integrations.github.push_gate import pending
        from test_admin_api import seed_thread_session, attach_recording_steer_server
        seed_thread_session("thread-34")
        with state.mutation() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id) VALUES ('thread-34')")
        with patch.object(outcomes.workspace_proxy, "_proxy", side_effect=to_workspace), patch.object(agent_messages, "call_admin_api", side_effect=to_admin), patch.object(pending, "_run_helper_json", return_value={"ok": True}), patch.object(orchestrator, "runtime_network_enabled", return_value=True), patch.object(orchestrator, "runtime_status", return_value="active"), patch.object(threads, "_recalled_memory_pages", return_value=([], "")), patch.object(threads, "_memory_context_message", return_value="Identity"), patch.object(orchestrator, "launch_turn", side_effect=attach_recording_steer_server) as launch:
            result = service.resolve_pending_push("abc123", "approve")
            self.assertEqual(result["pending_push"]["origin_thread_id"], "thread-34")
            launch.assert_called_once()
            turn, message, _ = launch.call_args.args
            self.assertIn("Approval ID: push-abc123", message)
            state.enqueue_pending_push("def456", "org", "repo", [], [], origin_thread_id="thread-34")
            service.resolve_pending_push("def456", "reject")
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(len(turn.server.messages), 1)
            self.assertIn("Approval ID: push-def456", turn.server.messages[0])
            with self.assertRaises(ApiError):
                service.resolve_pending_push("abc123", "approve")
            self.assertEqual(len(turn.server.messages), 1)

    def test_replay_failure_notifies_once_and_remains_a_failure(self):
        from host.runtime.admin_api import service
        from host.network_integrations.github.push_gate import pending
        with patch.object(pending, "_run_helper_json", side_effect=pending.HelperError("lease rejected")), patch.object(outcomes.workspace_proxy, "send_message") as send:
            with self.assertRaises(ApiError):
                service.resolve_pending_push("abc123", "approve")
            self.assertEqual(state.get_pending_push("abc123")["status"], "failed")
            send.assert_called_once()
            self.assertIn("lease rejected", send.call_args.args[1])
            with self.assertRaises(ApiError):
                service.resolve_pending_push("abc123", "approve")
            self.assertEqual(send.call_count, 1)
