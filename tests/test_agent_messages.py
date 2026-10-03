"""Cross-thread messaging at the peer-authenticated Workspace boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http import HTTPStatus
from threading import Event
import json
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime.core import db, pgclient
from host.runtime.workspace import agent_api, agent_messages, schedules
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.purpose import validate_purpose
from host.runtime.agent_shim import mcp_shim

SESSION = {"agent_runtime": "codex", "model": "gpt-6-astra", "effort": "high"}


class AgentMessageTests(unittest.TestCase):
    def setUp(self):
        transaction = self.enterContext(patch.object(db, "transaction"))
        self.cursor = transaction.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = (False,)

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
                    agent_messages.send_agent_message(body, sender_thread_id=sender)
                post.assert_not_called()

    def test_spawned_agent_can_reply_to_parent(self):
        with patch.object(agent_messages, "call_admin_api", return_value={"status": "accepted"}) as post:
            result = agent_messages.send_agent_message(
                {"thread_id": "thread-2", "message": "Done"}, sender_thread_id="thread-3"
            )
        self.assertEqual(result, {"status": "accepted", "thread_id": "thread-2"})
        self.assertIn("Sender thread: thread-3", post.call_args.args[2]["message"])

    def test_sender_header_comes_from_host(self):
        self.cursor.fetchone.return_value = ("codex", "gpt-6-astra", "high", False, False)
        with patch.object(
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
        self.assertEqual(set(body), {"message", "agent_runtime", "model", "effort", "peer_sender_thread_id"})
        self.assertEqual(body["peer_sender_thread_id"], "thread-1")

    def test_spawn_agent_creates_a_chat_with_delegation_and_reply_instructions(self):
        with patch.object(
            agent_messages.chat,
            "send_chat_message",
            return_value={"action": "accepted", "thread_id": "thread-12"},
        ) as send:
            result = agent_api.dispatch_call(
                "POST",
                "/agent/agents",
                {"message": "Review the draft.", **SESSION},
                peer_thread_id="thread-1",
            )
        self.assertEqual(
            result["body"], {"status": "accepted", "thread_id": "thread-12"}
        )
        request = send.call_args.args[0]
        self.assertEqual(
            {key: request[key] for key in ("agent_runtime", "model", "effort")},
            SESSION,
        )
        self.assertTrue(
            request["input_message"].startswith(
                agent_messages.MESSAGE_HEADER.format(sender="thread-1")
            )
        )
        self.assertIn("send_agent_message", request["input_message"])
        self.assertTrue(request["input_message"].endswith("Review the draft."))
        self.assertEqual(send.call_args.kwargs["peer_sender_thread_id"], "thread-1")

    def test_spawn_agent_rejects_bad_identity_shape_message_and_configuration(self):
        cases = [
            (None, {"message": "Review", **SESSION}),
            ("thread-1", {"message": "Review", **SESSION, "name": " "}),
            ("thread-1", {"message": "Review", **SESSION, "name": "x" * 101}),
            ("thread-1", {"message": "Review", **SESSION, "purpose": "Review companies"}),
            ("thread-1", {"message": "Review", **SESSION, "thread_id": "thread-2"}),
            ("thread-1", {"message": " ", **SESSION}),
            ("thread-1", {"message": "x" * 10001, **SESSION}),
            (
                "thread-1",
                {"message": "Review", "agent_runtime": "script", "model": "bash", "effort": "fixed"},
            ),
            (
                "thread-1",
                {"message": "Review", "agent_runtime": "codex", "model": "gpt-6-astra", "effort": "nope"},
            ),
        ]
        for sender, body in cases:
            with self.subTest(sender=sender, body=str(body)[:100]), patch.object(
                agent_messages.chat, "send_chat_message"
            ) as send:
                with self.assertRaises(WorkspaceError):
                    agent_messages.spawn_agent(body, sender_thread_id=sender)
                send.assert_not_called()

    def test_spawn_name_is_validated_before_creation(self):
        with patch.object(agent_messages.chat, "send_chat_message",
                          return_value={"action": "accepted", "thread_id": "thread-12"}) as send:
            agent_messages.spawn_agent(
                {"message": "Review", **SESSION, "name": " Research "},
                sender_thread_id="app-3",
            )
        self.assertEqual(send.call_args.kwargs, {
            "peer_sender_thread_id": "app-3", "name": "Research",
        })

    def test_discovery_lists_spawned_metadata_without_transcript(self):
        fields = {"thread_id": "thread-2", "name": "Researcher",
                  "spawned_by_thread_id": "app-3", "status": "idle", **SESSION}
        with patch.object(agent_messages.chat, "list_chat_threads", return_value={
            "threads": [{**fields, "task": "Current task", "archived": False}],
        }) as listed:
            result = agent_api.dispatch_call("GET", "/agent/spawned-agents", None, peer_thread_id="thread-9")
        self.assertEqual(result["body"], {"agents": [fields]})
        listed.assert_called_once_with(spawned=True)

    def test_discovery_rejects_other_methods_queries_and_get_bodies(self):
        for method, path, body in [
            ("POST", "/agent/spawned-agents", {}),
            ("GET", "/agent/spawned-agents?archived=true", None),
            ("GET", "/agent/spawned-agents", {}),
        ]:
            with self.subTest(method=method, path=path), patch.object(agent_messages, "list_spawned_agents") as listed:
                with self.assertRaises((WorkspaceError, ValueError)):
                    agent_api.dispatch_call(method, path, body, peer_thread_id="thread-1")
                listed.assert_not_called()

    def test_archive_requires_host_identity_and_a_chat_target(self):
        for sender, body in [
            (None, {"thread_id": "thread-2"}),
            ("thread-1", {"thread_id": "app-2"}),
            ("thread-1", {"thread_id": "thread-2", "spawned_by_thread_id": "thread-1"}),
            ("thread-1", {"thread_id": "../thread-2"}),
        ]:
            with self.subTest(sender=sender, body=body), patch.object(
                agent_messages.chat, "archive_chat_thread"
            ) as archive, self.assertRaises(WorkspaceError):
                agent_messages.archive_spawned_agent(body, sender)
            archive.assert_not_called()

    def test_archive_enforces_parent_and_idle_before_updating(self):
        for parent, status, expected in [
            ("thread-1", "idle", 200), ("app-3", "idle", 403),
            (None, "idle", 403), ("thread-1", "running", 409),
        ]:
            with self.subTest(parent=parent, status=status):
                self.cursor.reset_mock()
                self.cursor.fetchone.side_effect = [(parent,), (1,), ("thread-2", True)]
                with patch.object(agent_messages.chat, "call_admin_api",
                                  return_value={"thread": {"status": status}}) as host:
                    if expected == 200:
                        result = agent_api.dispatch_call(
                            "POST", "/agent/agents/archive", {"thread_id": "thread-2"},
                            peer_thread_id="thread-1",
                        )
                        self.assertEqual(result["body"], {"thread_id": "thread-2", "archived": True})
                    else:
                        with self.assertRaises(WorkspaceError) as caught:
                            agent_messages.archive_spawned_agent({"thread_id": "thread-2"}, "thread-1")
                        self.assertEqual(caught.exception.status, expected)
                        self.assertFalse(any(call.args[0].startswith("UPDATE") for call in self.cursor.execute.call_args_list))
                    if expected == 403:
                        host.assert_not_called()
                self.assertIn("SELECT spawned_by_thread_id", self.cursor.execute.call_args_list[0].args[0])
                if expected != 403:
                    self.assertIn("FOR UPDATE", self.cursor.execute.call_args_list[1].args[0])

    def test_parent_archive_waits_for_an_operator_send_to_finish_admission(self):
        chat = agent_messages.chat
        sending, release_send, running, archive_attempted = Event(), Event(), Event(), Event()
        original_lock = chat._message_send_lock

        @contextmanager
        def observed_lock(thread_id):
            if sending.is_set():
                archive_attempted.set()
            with original_lock(thread_id):
                yield

        def admit(thread_id, request):
            sending.set()
            self.assertTrue(release_send.wait(5))
            running.set()
            return {"status": "accepted"}

        def status(method, path):
            self.assertTrue(running.is_set(), "archive checked idle during an admitted send")
            return {"thread": {"status": "running"}}

        self.cursor.fetchone.return_value = ("thread-1",)
        with patch.object(chat, "_message_send_lock", side_effect=observed_lock), \
                patch.object(chat, "_require_sendable_thread", return_value=None), \
                patch.object(chat, "_send_with_busy_retry", side_effect=admit), \
                patch.object(chat, "call_admin_api", side_effect=status) as host, \
                ThreadPoolExecutor(max_workers=2) as workers:
            send = workers.submit(chat.send_chat_message, {"thread_id": "thread-2", "input_message": "Continue", **SESSION})
            try:
                self.assertTrue(sending.wait(5))
                archive = workers.submit(agent_messages.archive_spawned_agent, {"thread_id": "thread-2"}, "thread-1")
                self.assertTrue(archive_attempted.wait(5))
                host.assert_not_called()
            finally:
                release_send.set()
            self.assertEqual(send.result(timeout=5)["action"], "accepted")
            with self.assertRaises(WorkspaceError) as caught:
                archive.result(timeout=5)
            self.assertEqual(caught.exception.status, 409)
            self.assertFalse(any(call.args[0].startswith("UPDATE") for call in self.cursor.execute.call_args_list))

    def test_archive_tool_is_listed_and_forwards_to_workspace(self):
        with patch.object(mcp_shim, "_tools_request", return_value={
            "status": 200, "body": {"thread_id": "thread-2", "archived": True},
        }) as request:
            result = mcp_shim._call_tool({"name": "archive_spawned_agent", "arguments": {"thread_id": "thread-2"}})
        self.assertFalse(result["isError"])
        self.assertEqual(request.call_args.args[2], {
            "method": "POST", "path": "/agent/agents/archive", "body": {"thread_id": "thread-2"},
        })
        self.assertIn("archive_spawned_agent", [tool["name"] for tool in mcp_shim._list_tools()])

    def test_spawn_agent_rejects_an_invalid_chat_response(self):
        with patch.object(
            agent_messages.chat,
            "send_chat_message",
            return_value={"action": "accepted", "thread_id": "app-2"},
        ), self.assertRaisesRegex(WorkspaceError, "invalid spawned agent"):
            agent_messages.spawn_agent(
                {"message": "Review", **SESSION}, sender_thread_id="thread-1"
            )

    def test_full_character_allowance_leaves_room_for_utf8_and_header(self):
        with patch.object(
            agent_messages, "call_admin_api", return_value={"status": "accepted"}
        ) as post:
            message = "😀" * 10000
            agent_messages.send_agent_message({"thread_id": "thread-2", "message": message}, sender_thread_id="thread-1")
        delivered = post.call_args.args[2]["message"]
        self.assertTrue(delivered.endswith(message))
        self.assertGreater(len(delivered.encode("utf-8")), 40000)

    def test_reply_to_chat_uses_same_delivery_path_without_runtime_override(self):
        with patch.object(
            agent_messages, "call_admin_api", return_value={"status": "accepted"}
        ) as post:
            agent_messages.send_agent_message({"thread_id": "thread-1", "message": "Review complete."}, sender_thread_id="app-2")
        self.assertEqual(post.call_args.args[1], "/v1/threads/thread-1/messages")
        self.assertEqual(set(post.call_args.args[2]), {"message", "peer_sender_thread_id"})
        self.assertEqual(post.call_args.args[2]["peer_sender_thread_id"], "app-2")
        self.assertIn("Sender thread: app-2", post.call_args.args[2]["message"])

    def test_runtime_error_is_returned_without_retry(self):
        with patch.object(
            agent_messages, "call_admin_api", side_effect=WorkspaceError(HTTPStatus.TOO_MANY_REQUESTS, "runtime at capacity")
        ) as post:
            with self.assertRaisesRegex(WorkspaceError, "runtime at capacity"):
                agent_messages.send_agent_message({"thread_id": "thread-2", "message": "hello"}, sender_thread_id="thread-1")
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
            with self.subTest(target=target, row=row), patch.object(db, "transaction") as transaction, patch.object(agent_messages, "call_admin_api", return_value={"status": "accepted"}) as post:
                transaction.return_value.__enter__.return_value = cur
                with self.assertRaises(WorkspaceError) as caught:
                    agent_messages.deliver_message(target, {"message": "hello"})
                self.assertEqual(caught.exception.status, expected)
                post.assert_not_called()

    def test_workspace_rejects_invalid_or_overridden_message_before_delivery(self):
        for target, body in [
            ("../../app-1", {"message": "hello"}),
            ("app-1", {"message": "hello", "model": "override"}),
            ("app-1", {"message": " "}),
            ("app-1", {"message": "😀" * 13000}),
        ]:
            with self.subTest(target=target), patch.object(db, "transaction") as transaction:
                with self.assertRaises(WorkspaceError):
                    agent_messages.deliver_message(target, body)
                transaction.assert_not_called()

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

    def test_spawn_agent_mcp_uses_the_typed_workspace_route(self):
        body = {"status": "accepted", "thread_id": "thread-9"}
        with patch.object(
            mcp_shim,
            "_tools_request",
            return_value={"status": 200, "body": body},
        ) as request:
            result = mcp_shim._call_tool(
                {"name": "spawn_agent", "arguments": {"message": "Research", **SESSION}}
            )
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), body)
        self.assertEqual(request.call_args.args[2]["path"], "/agent/agents")

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

    def test_spawn_metadata_is_stored_before_delivery_and_survives_archive(self):
        from host.runtime.workspace.chat import backend as chat
        observed = []
        def deliver(thread_id, request):
            with db.transaction() as cur:
                cur.execute("SELECT name, spawned_by_thread_id FROM chat_threads WHERE thread_id = %s", (thread_id,))
                observed.append(cur.fetchone())
            return {"status": "accepted"}
        with patch.object(chat, "_send_with_busy_retry", side_effect=deliver):
            created = agent_messages.spawn_agent({
                **SESSION, "message": "Research", "name": "Researcher",
            }, "app-3")
        self.assertEqual(observed, [("Researcher", "app-3")])
        target = created["thread_id"]
        with self.assertRaises(WorkspaceError) as caught:
            agent_messages.archive_spawned_agent({"thread_id": target}, "thread-9")
        self.assertEqual(caught.exception.status, 403)
        with patch.object(chat, "call_admin_api", return_value={"thread": {"status": "idle"}}):
            agent_messages.archive_spawned_agent({"thread_id": target}, "app-3")
            chat.unarchive_chat_thread(target)
        with db.transaction() as cur:
            cur.execute("SELECT name, spawned_by_thread_id, archived FROM chat_threads WHERE thread_id = %s", (target,))
            self.assertEqual(cur.fetchone(), ("Researcher", "app-3", False))
            cur.execute("INSERT INTO chat_threads (thread_id, archived, spawned_by_thread_id) VALUES"
                        " ('thread-101', FALSE, NULL), ('thread-102', TRUE, 'app-3')")
        summaries = [{"thread_id": thread_id, "status": "idle", **SESSION}
                     for thread_id in (target, "thread-101", "thread-102")]
        with patch.object(chat, "call_admin_api", return_value={"threads": summaries}):
            found = agent_messages.list_spawned_agents()["agents"]
        self.assertEqual([agent["thread_id"] for agent in found], [target])
        self.assertNotIn("purpose", found[0])
        self.assertEqual(found[0]["name"], "Researcher")
        self.assertEqual(found[0]["spawned_by_thread_id"], "app-3")

    def test_schedule_purpose_survives_edit_history_restore_and_delete(self):
        schedule = schedules.create_schedule({**SESSION, "name": "Research", "triggers": [{"type": "daily", "times": ["09:00"], "prompt": "Research"}],   "purpose": "Research companies"}, actor="agent")
        fields = {key: schedule[key] for key in ("name", "triggers", "agent_runtime", "model", "effort")}
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
            agent_messages.deliver_message(schedule["thread_id"], {"message": "hello"})

    def test_app_purpose_is_listed_and_omitting_it_preserves_it(self):
        from host.runtime.workspace.web_apps import backend as apps
        with patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
            app = apps.create_web_app()
        app = apps.rename_web_app(app["app_id"], {"name": "Research", "purpose": "Review company research"})
        app = apps.rename_web_app(app["app_id"], {"name": "Research desk"})
        self.assertEqual(app["purpose"], "Review company research")
        with patch.object(apps, "_host_thread_summaries", return_value=[]):
            self.assertEqual(apps.list_all_web_apps()["apps"][0]["purpose"], "Review company research")
        with patch.object(agent_messages, "call_admin_api", return_value={"status": "accepted"}) as send:
            agent_messages.deliver_message(app["app_id"], {"message": "hello"})
        self.assertEqual(send.call_args.args[2], {"message": "hello", **app["agent_settings"]})

    def test_destination_row_stays_locked_until_send_returns(self):
        from host.runtime.workspace.web_apps import backend as apps
        with patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
            app = apps.create_web_app()
        schedule = schedules.create_schedule({
            **SESSION, "name": "Research", "triggers": [{"type": "daily", "times": ["09:00"], "prompt": "Research"}],
        }, actor="agent")
        with db.transaction() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id) VALUES ('thread-34')")
        for target, query in [
            ("thread-34", "SELECT archived FROM chat_threads WHERE thread_id = %s FOR UPDATE NOWAIT"),
            (app["app_id"], "SELECT archived FROM web_apps WHERE app_id = %s FOR UPDATE NOWAIT"),
            (schedule["thread_id"], "SELECT deleted_at FROM schedules WHERE thread_id = %s FOR UPDATE NOWAIT"),
        ]:
            def attempt_edit(*args):
                # A second connection models a concurrent Workspace edit.
                with self.assertRaises(pgclient.Error) as caught:
                    with db.transaction() as cur:
                        cur.execute(query, (target,))
                self.assertEqual(caught.exception.sqlstate, "55P03")
                return {"status": "accepted"}
            with self.subTest(target=target), patch.object(
                agent_messages, "call_admin_api", side_effect=attempt_edit
            ) as send:
                agent_messages.deliver_message(target, {"message": "hello"})
                send.assert_called_once()
                with db.transaction() as cur:
                    cur.execute(query, (target,))
                    self.assertIsNotNone(cur.fetchone())

    def test_archive_locks_destination_before_checking_idle(self):
        from host.runtime.workspace.chat import backend as chat
        from host.runtime.workspace.web_apps import backend as apps
        with patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
            app = apps.create_web_app()
        with db.transaction() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id) VALUES ('thread-34')")
        for backend, target, action, query in [
            (chat, "thread-34", lambda: chat.set_chat_thread_archived("thread-34", archived=True),
             "SELECT archived FROM chat_threads WHERE thread_id = %s FOR SHARE NOWAIT"),
            (apps, app["app_id"], lambda: apps.set_web_app_archived(app["app_id"], True),
             "SELECT archived FROM web_apps WHERE app_id = %s FOR SHARE NOWAIT"),
        ]:
            def status_check(*args):
                # A notification must not enter between this idle check and
                # the archive/settings update in the same transaction.
                with self.assertRaises(pgclient.Error) as caught:
                    with db.transaction() as cur:
                        cur.execute(query, (target,))
                self.assertEqual(caught.exception.sqlstate, "55P03")
                return {"thread": {**SESSION, "thread_id": target, "status": "idle"}}
            with self.subTest(target=target), patch.object(
                backend, "call_admin_api", side_effect=status_check
            ) as status, patch.object(apps, "active_agent_runtimes", return_value=["codex"]):
                action()
                status.assert_called_once()
                with db.transaction() as cur:
                    cur.execute(query, (target,))
                    self.assertIsNotNone(cur.fetchone())
