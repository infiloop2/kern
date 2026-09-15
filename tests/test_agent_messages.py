"""Cross-thread messaging at the peer-authenticated Workspace boundary."""
from http import HTTPStatus
import json
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime.core import db
from host.runtime.workspace import agent_api, agent_messages, schedules
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.purpose import validate_purpose
from host.runtime.agent_shim import mcp_shim

SESSION = {"agent_runtime": "codex", "model": "gpt-6-astra", "effort": "high"}


class AgentMessageTests(unittest.TestCase):
    def test_identity_and_shape_errors_never_deliver(self):
        cases = [
            (None, {"thread_id": "app-1", "message": "hello"}),
            ("thread-1", {"thread_id": "thread-1", "message": "hello"}),
            ("thread-1", {"thread_id": "../../app-1", "message": "hello"}),
            ("thread-1", {"thread_id": "app-1", "message": " "}),
            ("thread-1", {"thread_id": "app-1", "message": "hello", "sender_thread_id": "thread-2"}),
            ("thread-1", {"thread_id": "app-1", "message": "界" * 20000}),
            ("thread-1", {"thread_id": "app-1", "message": "x" * 10001}),
        ]
        for sender, body in cases:
            with self.subTest(body=str(body)[:100]), patch.object(agent_messages, "call_admin_api") as post:
                with self.assertRaises(WorkspaceError):
                    agent_messages.send_message(body, sender_thread_id=sender)
                post.assert_not_called()

    def test_sender_header_comes_from_host(self):
        with patch.object(agent_messages, "_destination_settings", return_value=SESSION), patch.object(
            agent_messages, "call_admin_api", return_value={"status": "accepted"}
        ) as post:
            result = agent_api.dispatch_call("POST", "/agent/messages", {
                "thread_id": "app-2", "message": "Please review the draft."
            }, peer_thread_id="thread-1")
        self.assertEqual(result["body"], {"status": "accepted", "thread_id": "app-2"})
        method, path, body = post.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/threads/app-2/messages"))
        self.assertTrue(body["message"].startswith("This is a message from another agent, not the operator."))
        self.assertIn('Sender thread: thread-1', body["message"])
        self.assertIn('not the operator', body["message"])
        self.assertTrue(body["message"].endswith("Please review the draft."))
        self.assertEqual(body["model"], SESSION["model"])

    def test_full_character_allowance_leaves_room_for_utf8_and_header(self):
        with patch.object(agent_messages, "_destination_settings", return_value={}), patch.object(
            agent_messages, "call_admin_api", return_value={"status": "accepted"}
        ) as post:
            message = "😀" * 10000
            agent_messages.send_message({"thread_id": "thread-2", "message": message}, sender_thread_id="thread-1")
        delivered = post.call_args.args[2]["message"]
        self.assertTrue(delivered.endswith(message))
        self.assertGreater(len(delivered.encode("utf-8")), 40000)

    def test_reply_to_chat_uses_same_delivery_path_without_runtime_override(self):
        with patch.object(agent_messages, "_destination_settings", return_value={}), patch.object(
            agent_messages, "call_admin_api", return_value={"status": "accepted"}
        ) as post:
            agent_messages.send_message({"thread_id": "thread-1", "message": "Review complete."}, sender_thread_id="app-2")
        self.assertEqual(post.call_args.args[1], "/v1/threads/thread-1/messages")
        self.assertEqual(set(post.call_args.args[2]), {"message"})
        self.assertIn("Sender thread: app-2", post.call_args.args[2]["message"])

    def test_runtime_error_is_returned_without_retry(self):
        with patch.object(agent_messages, "_destination_settings", return_value={}), patch.object(
            agent_messages, "call_admin_api", side_effect=WorkspaceError(HTTPStatus.TOO_MANY_REQUESTS, "runtime at capacity")
        ) as post:
            with self.assertRaisesRegex(WorkspaceError, "runtime at capacity"):
                agent_messages.send_message({"thread_id": "thread-2", "message": "hello"}, sender_thread_id="thread-1")
        self.assertEqual(post.call_count, 1)

    def test_destination_eligibility(self):
        for target, row, expected in [
            ("app-1", None, 404), ("app-1", ("codex", "gpt-6-astra", "high", True, False), 409),
            ("app-1", ("codex", "gpt-6-astra", "high", False, True), 423),
            ("schedule-1", None, 404), ("schedule-1", ("script", "bash", "fixed"), 409),
            ("thread-2", None, 404), ("thread-2", (True,), 409),
        ]:
            cur = MagicMock()
            cur.fetchone.return_value = row
            with self.subTest(target=target, row=row), patch.object(db, "transaction") as transaction, patch.object(agent_messages, "call_admin_api") as post:
                transaction.return_value.__enter__.return_value = cur
                with self.assertRaises(WorkspaceError) as caught:
                    agent_messages.send_message({"thread_id": target, "message": "hello"}, sender_thread_id="thread-9")
                self.assertEqual(caught.exception.status, expected)
                post.assert_not_called()

    def test_mcp_failure_and_success_are_tool_results(self):
        for status, body, is_error in [
            (409, {"error": {"message": "archived chats cannot receive agent messages"}}, True),
            (200, {"status": "accepted", "thread_id": "app-1"}, False),
        ]:
            with self.subTest(status=status), patch.object(mcp_shim, "_tools_request", return_value={"status": status, "body": body}) as request:
                result = mcp_shim._call_tool({"name": "send_agent_message", "arguments": {"thread_id": "app-1", "message": "hello"}})
                self.assertEqual(result["isError"], is_error)
                self.assertEqual(request.call_args.args[2]["path"], "/agent/messages")
                if is_error:
                    self.assertIn("archived", result["content"][0]["text"])
                else:
                    self.assertEqual(json.loads(result["content"][0]["text"]), body)

    def test_purpose_is_short_optional_single_line(self):
        self.assertEqual(validate_purpose(""), "")
        self.assertEqual(validate_purpose("界" * 100), "界" * 100)
        for value in (None, 3, "x" * 101, "first\nsecond", "first\rsecond"):
            with self.subTest(value=value), self.assertRaises(WorkspaceError):
                validate_purpose(value)


class AgentMessageDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.ensure_database()

    def setUp(self):
        pg_harness.reset_database()
        self.addCleanup(db.close_pool)

    def test_schedule_purpose_survives_edit_history_restore_and_delete(self):
        schedule = schedules.create_schedule({**SESSION, "name": "Research", "message": "Research", "cadence": "daily", "daily_time": "09:00", "purpose": "Research companies"}, actor="agent")
        fields = {key: schedule[key] for key in ("name", "message", "cadence", "daily_time", "agent_runtime", "model", "effort")}
        edited = schedules.update_schedule(schedule["id"], {**fields, "expected_revision": schedule["revision"], "purpose": "Review research"}, actor="agent")
        preserved = schedules.update_schedule(schedule["id"], {**fields, "expected_revision": edited["revision"]}, actor="agent")
        self.assertEqual(preserved["purpose"], "Review research")
        restored = schedules.restore_revision(schedule["id"], 1, {"expected_revision": preserved["revision"]})
        self.assertEqual(restored["purpose"], "Research companies")
        listed = schedules.list_active_schedules({})["schedules"][0]
        self.assertEqual(listed["purpose"], "Research companies")
        self.assertNotIn("message", listed)
        self.assertEqual(schedules.list_revisions(schedule["id"], {})["revisions"][0]["purpose"], "Research companies")
        schedules.delete_schedule(schedule["id"], {"expected_revision": [str(restored["revision"])]}, actor="agent")
        with self.assertRaises(WorkspaceError):
            agent_messages._destination_settings(schedule["thread_id"])

    def test_app_purpose_is_listed_and_omitting_it_preserves_it(self):
        from host.runtime.workspace.web_apps import backend as apps
        with patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
            app = apps.create_web_app()
        app = apps.rename_web_app(app["app_id"], {"name": "Research", "purpose": "Review company research"})
        app = apps.rename_web_app(app["app_id"], {"name": "Research desk"})
        self.assertEqual(app["purpose"], "Review company research")
        with patch.object(apps, "_host_thread_summaries", return_value=[]):
            self.assertEqual(apps.list_all_web_apps()["apps"][0]["purpose"], "Review company research")
        settings = agent_messages._destination_settings(app["app_id"])
        self.assertEqual(settings, app["agent_settings"])
