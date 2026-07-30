from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import socket
import unittest
import unittest.mock
from unittest.mock import call, patch

from host.apps.agent_chat import backend

APP_DIR = Path(__file__).resolve().parents[1] / "host" / "apps" / "agent_chat"


class AgentChatBackendTests(unittest.TestCase):
    def _request(self, method: str, path: str, body: dict | None = None):
        """Drive the request handler over an in-memory socket pair: the
        agent's loopback TCP egress is dropped on a Kern host, so HTTP-level
        routing is exercised without a listening TCP server."""
        client, server_side = socket.socketpair()
        self.addCleanup(client.close)
        payload = b"" if body is None else json.dumps(body).encode()
        lines = [
            f"{method} {path} HTTP/1.1",
            "Host: agent-chat",
            "X-Kern-App-Proxy: agent_chat",
        ]
        if payload:
            lines.append(f"Content-Length: {len(payload)}")
        client.sendall("\r\n".join(lines).encode() + b"\r\n\r\n" + payload)
        backend.Handler(server_side, ("127.0.0.1", 0), None)
        server_side.close()
        raw = b""
        while chunk := client.recv(65536):
            raw += chunk
        head, _, response_body = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(response_body)

    def test_session_options_endpoint_exposes_the_creation_matrix(self) -> None:
        status, body = self._request("GET", "/session-options")

        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "session_options": {
                    "codex": {
                        "gpt-5.6-terra": ["high", "max", "ultra"],
                        "gpt-5.6-sol": ["high", "max", "ultra"],
                        "gpt-5.6-luna": ["high", "max"],
                    },
                    "claude_code": {
                        "claude-opus-5": ["high", "max", "ultracode"],
                        "claude-fable-5": ["high", "max", "ultracode"],
                        "claude-sonnet-5": ["high", "max", "ultracode"],
                    },
                    "hermes": {
                        "deepseek.v3.2": ["high"],
                        "qwen.qwen3-coder-next": ["high"],
                        "moonshotai.kimi-k2.5": ["high"],
                    },
                }
            },
        )

    def test_event_page_reserves_bridge_space_for_metadata(self) -> None:
        text_budget = backend.THREAD_EVENT_PAGE * backend.THREAD_EVENT_MESSAGE_BYTES
        self.assertLessEqual(
            text_budget,
            backend.MAX_ADMIN_RESPONSE_BYTES - 256 * 1024,
        )

    def test_composer_uses_send_button_spinner_without_process_phases(self) -> None:
        source = (APP_DIR / "ui" / "agent_chat.js").read_text()
        css = (APP_DIR / "ui" / "agent_chat.css").read_text()
        self.assertNotIn('attachmentActivity = "Sending…"', source)
        self.assertIn('sendButton.classList.toggle("sending", sendingMessage)', source)
        self.assertIn(".send-button.sending::after", css)
        self.assertIn('attachmentActivity = "Stopping…"', source)
        self.assertNotIn("Waiting for agent to start", source)
        self.assertEqual(
            (backend.SEND_BUSY_RETRIES - 1) * backend.SEND_BUSY_RETRY_DELAY_SECONDS,
            10,
        )

    def test_composer_restores_the_new_thread_draft_on_startup(self) -> None:
        source = (APP_DIR / "ui" / "agent_chat.js").read_text()
        startup = source.rsplit("setSessionOptions();", 1)[1]
        self.assertLess(
            startup.index("restoreComposerDraft();"),
            startup.index("updateComposer();"),
        )

    def test_composer_omits_unchanged_session_configuration(self) -> None:
        source = (APP_DIR / "ui" / "agent_chat.js").read_text()
        self.assertIn("const changingSession = sessionConfigurationChanged();", source)
        self.assertIn("if (startingNewThread || changingSession)", source)
        self.assertIn("const preservingRecordedSession =", source)
        self.assertNotIn(
            'const request = { input_message: "", agent_runtime: runtime, model, effort };',
            source,
        )

    def test_follow_up_forwards_selected_session_configuration(self) -> None:
        response = {
            "status": "accepted",
            "thread": {"thread_id": "existing", "status": "running"},
        }
        with (
            patch("host.apps.agent_chat.backend._require_sendable_thread") as require,
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=response) as admin_call,
        ):
            self.assertEqual(
                backend.send_app_message(
                    {
                        "input_message": "continue",
                        "thread_id": "existing",
                        "agent_runtime": "codex",
                        "model": "gpt-5.6-sol",
                        "effort": "high",
                    }
                ),
                {"action": "accepted", "thread_id": "existing"},
            )

        require.assert_called_once_with("existing")
        admin_call.assert_called_once_with(
            "POST",
            "/v1/threads/existing/messages",
            {
                "message": "continue",
                "agent_runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        )

    def test_send_without_thread_id_reserves_the_next_successive_name(self) -> None:
        request = {
            "input_message": "start",
            "agent_runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "max",
        }
        response = {
            "status": "accepted",
            "thread": {"thread_id": "thread-4", "status": "running"},
        }
        with (
            patch("host.apps.agent_chat.backend._reserve_generated_thread_id", return_value="thread-4") as reserve,
            patch("host.apps.agent_chat.backend._require_sendable_thread") as require,
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=response) as admin_call,
        ):
            self.assertEqual(
                backend.send_app_message(request),
                {"action": "accepted", "thread_id": "thread-4"},
            )

        reserve.assert_called_once_with()
        require.assert_called_once_with("thread-4")
        admin_call.assert_called_once_with(
            "POST",
            "/v1/threads/thread-4/messages",
            {
                "message": "start",
                "agent_runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "max",
            },
        )

    def test_send_retries_transient_turn_lifecycle_conflicts(self) -> None:
        for message in (
            "the agent is starting; retry shortly",
            "the agent is finishing; retry shortly",
        ):
            with self.subTest(message=message):
                busy = backend.AppError(HTTPStatus.CONFLICT, message)
                with (
                    patch("host.apps.agent_chat.backend._require_sendable_thread"),
                    patch(
                        "host.apps.agent_chat.backend.call_admin_api",
                        side_effect=(busy, busy, {"status": "accepted"}),
                    ) as admin_call,
                    patch.object(backend, "SEND_BUSY_RETRY_DELAY_SECONDS", 0),
                ):
                    result = backend.send_app_message(
                        {"thread_id": "thread-1", "input_message": "go"}
                    )

                self.assertEqual(result, {"action": "accepted", "thread_id": "thread-1"})
                self.assertEqual(admin_call.call_count, 3)

    def test_send_surfaces_a_persistently_busy_thread(self) -> None:
        busy = backend.AppError(
            HTTPStatus.CONFLICT,
            "the agent is finishing; retry shortly",
        )
        with (
            patch("host.apps.agent_chat.backend._require_sendable_thread"),
            patch("host.apps.agent_chat.backend.call_admin_api", side_effect=busy) as admin_call,
            patch.object(backend, "SEND_BUSY_RETRY_DELAY_SECONDS", 0),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.send_app_message({"thread_id": "thread-1", "input_message": "go"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn(backend.SEND_RETRY_MARKER, error.exception.message)
        self.assertEqual(admin_call.call_count, backend.SEND_BUSY_RETRIES)

    def test_send_does_not_retry_other_conflicts(self) -> None:
        conflict = backend.AppError(
            HTTPStatus.CONFLICT,
            "Hermes cannot accept another message while running; wait for it to finish",
        )
        with (
            patch("host.apps.agent_chat.backend._require_sendable_thread"),
            patch("host.apps.agent_chat.backend.call_admin_api", side_effect=conflict) as admin_call,
            self.assertRaises(backend.AppError) as error,
        ):
            backend.send_app_message({"thread_id": "thread-1", "input_message": "go"})

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(admin_call.call_count, 1)

    def test_send_rejects_an_invalid_host_send_status(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_sendable_thread"),
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                return_value={"status": "bogus"},
            ),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.send_app_message({"thread_id": "thread-1", "input_message": "go"})

        self.assertEqual(error.exception.status, HTTPStatus.BAD_GATEWAY)

    def test_archived_threads_reject_sends_before_the_host_call(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchone.return_value = (True,)
        with (
            patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction),
            patch("host.apps.agent_chat.backend.call_admin_api") as admin_call,
            self.assertRaises(backend.AppError) as error,
        ):
            backend.send_app_message(
                {"thread_id": "thread-1", "input_message": "revive?"}
            )

        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("read-only", error.exception.message)
        admin_call.assert_not_called()

    def test_sendable_thread_row_is_reserved_before_the_archive_check(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchone.return_value = (False,)
        with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
            backend._require_sendable_thread("thread-1")

        insert = next(
            item for item in cursor.execute.call_args_list
            if "INSERT INTO threads" in item.args[0]
        )
        self.assertIn("ON CONFLICT (thread_id) DO NOTHING", insert.args[0])
        self.assertEqual(insert.args[1], ("thread-1",))

    def test_app_session_options_must_be_provided_together(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_sendable_thread"),
            patch("host.apps.agent_chat.backend.call_admin_api") as admin_call,
            self.assertRaises(backend.AppError) as error,
        ):
            backend.send_app_message(
                {
                    "input_message": "start",
                    "thread_id": "partial",
                    "agent_runtime": "codex",
                }
            )

        self.assertEqual(error.exception.status, 400)
        self.assertIn("must be provided together", error.exception.message)
        admin_call.assert_not_called()

    def test_host_send_errors_pass_through_unchanged(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_sendable_thread"),
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                side_effect=backend.AppError(HTTPStatus.BAD_REQUEST, "session configuration required"),
            ),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.send_app_message({"input_message": "start", "thread_id": "unknown"})

        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.message, "session configuration required")

    def test_generated_thread_names_count_over_all_recorded_threads(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        for rows, expected in (
            ([], "thread-1"),
            ([("thread-2",), ("thread-11",), ("archived-name",), ("thread-03",)], "thread-12"),
        ):
            cursor.fetchall.return_value = rows
            cursor.fetchone.return_value = (expected,)
            with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
                self.assertEqual(backend._reserve_generated_thread_id(), expected)
            insert_args, _kwargs = cursor.execute.call_args
            self.assertIn("ON CONFLICT (thread_id) DO NOTHING", insert_args[0])
            self.assertEqual(insert_args[1], (expected,))
            cursor.reset_mock()

    def test_generated_thread_name_retries_past_a_concurrent_reservation(self) -> None:
        """A lost insert race means another request took the name: the next
        attempt sees the committed row and reserves the following number."""
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchall.side_effect = ([("thread-1",)], [("thread-1",), ("thread-2",)])
        cursor.fetchone.side_effect = (None, ("thread-3",))
        with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
            self.assertEqual(backend._reserve_generated_thread_id(), "thread-3")

    def test_list_app_threads_joins_host_status_against_recorded_names(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        # thread-orphan is known to the host but has no app thread row (its
        # reservation was archived or never recorded here); it must stay out
        # of the index even though the host returns a summary for it.
        cursor.fetchall.return_value = [
            ("thread-1", "Customer launch"),
            ("thread-2", "thread-2"),
        ]
        first_page = {
            "threads": [
                {
                    "thread_id": "thread-1",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "last_used_at": "2026-07-17T10:00:00Z",
                    "status": "running",
                },
                {
                    "thread_id": "thread-orphan",
                    "agent_runtime": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "last_used_at": "2026-07-17T12:00:00Z",
                    "status": "idle",
                },
            ],
            "next_before": "next-token",
        }
        second_page = {
            "threads": [
                {
                    "thread_id": "thread-2",
                    "agent_runtime": "claude_code",
                    "model": "claude-opus-5",
                    "effort": "max",
                    "last_used_at": "2026-07-17T09:00:00Z",
                    "status": "idle",
                },
            ]
        }
        with (
            patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction),
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                side_effect=(first_page, second_page),
            ) as admin_call,
        ):
            response = backend.list_app_threads()

        self.assertEqual(
            admin_call.call_args_list,
            [
                call("GET", "/v1/threads?limit=100"),
                call("GET", "/v1/threads?limit=100&before=next-token"),
            ],
        )
        # Only threads recorded by the app, newest-first by last_used_at.
        self.assertEqual(
            [thread["thread_id"] for thread in response["threads"]],
            ["thread-1", "thread-2"],
        )
        first = response["threads"][0]
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["name"], "Customer launch")
        self.assertFalse(first["archived"])
        self.assertEqual(response["threads"][1]["status"], "idle")

    def test_list_app_threads_can_select_archived_threads(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchall.return_value = [("thread-1", "thread-1")]
        summaries = {
            "threads": [{
                "thread_id": "thread-1",
                "agent_runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "last_used_at": "2026-07-17T10:00:00Z",
                "status": "idle",
            }]
        }
        with (
            patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=summaries),
        ):
            response = backend.list_app_threads(archived=True)

        self.assertTrue(response["threads"][0]["archived"])
        archived_query = next(
            item for item in cursor.execute.call_args_list
            if "WHERE archived" in item.args[0]
        )
        self.assertEqual(archived_query.args[1], (True,))

    def test_list_app_threads_keeps_threads_on_a_superseded_model(self) -> None:
        # A thread started under an earlier catalog stays listed with the model
        # it recorded; the option matrix only gates what may be created.
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchall.return_value = [("thread-1", "thread-1")]
        summaries = {
            "threads": [
                {
                    "thread_id": "thread-1",
                    "agent_runtime": "claude_code",
                    "model": "opus",
                    "effort": "high",
                    "last_used_at": "2026-07-17T10:00:00Z",
                    "status": "idle",
                }
            ]
        }
        with (
            patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=summaries),
        ):
            response = backend.list_app_threads()

        self.assertEqual(response["threads"][0]["model"], "opus")

    def test_list_app_threads_rejects_an_invalid_host_status(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchall.return_value = [("thread-1", "thread-1")]
        summaries = {
            "threads": [{
                "thread_id": "thread-1",
                "agent_runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "last_used_at": "2026-07-17T10:00:00Z",
                "status": "queued",
            }]
        }
        with (
            patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=summaries),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.list_app_threads()

        self.assertEqual(error.exception.status, HTTPStatus.BAD_GATEWAY)

    def test_list_app_thread_events_proxies_with_since(self) -> None:
        events = {"events": [{"seq": 5, "thread_id": "thread-1", "event_type": "thread.message"}]}
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=events) as admin_call,
        ):
            response = backend.list_app_thread_events("thread-1", {"since": ["2"]})

        admin_call.assert_called_once_with(
            "GET",
            "/v1/threads/thread-1/events"
            f"?limit={backend.THREAD_EVENT_PAGE}"
            f"&message_bytes={backend.THREAD_EVENT_MESSAGE_BYTES}"
            "&event_type=thread.message"
            "&event_type=thread.activity"
            "&event_type=thread.error"
            "&event_type=thread.stopped"
            "&since=2",
        )
        self.assertEqual(response, events)

    def test_list_app_thread_events_opens_latest_and_pages_before(self) -> None:
        events = {"events": [{"seq": 5, "thread_id": "thread-1", "event_type": "thread.message"}]}
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            patch("host.apps.agent_chat.backend.call_admin_api", return_value=events) as admin_call,
        ):
            self.assertEqual(backend.list_app_thread_events("thread-1", {}), events)
            self.assertEqual(
                backend.list_app_thread_events("thread-1", {"before": ["5"]}),
                events,
            )

        self.assertEqual(
            admin_call.call_args_list,
            [
                call(
                    "GET",
                    "/v1/threads/thread-1/events"
                    f"?limit={backend.THREAD_EVENT_PAGE}"
                    f"&message_bytes={backend.THREAD_EVENT_MESSAGE_BYTES}"
                    "&event_type=thread.message"
                    "&event_type=thread.activity"
                    "&event_type=thread.error"
                    "&event_type=thread.stopped",
                ),
                call(
                    "GET",
                    "/v1/threads/thread-1/events"
                    f"?limit={backend.THREAD_EVENT_PAGE}"
                    f"&message_bytes={backend.THREAD_EVENT_MESSAGE_BYTES}"
                    "&event_type=thread.message"
                    "&event_type=thread.activity"
                    "&event_type=thread.error"
                    "&event_type=thread.stopped"
                    "&before=5",
                ),
            ],
        )

    def test_list_app_thread_events_can_page_conversation_without_activity(self) -> None:
        events = {
            "events": [
                {"seq": 5, "thread_id": "thread-1", "event_type": "thread.message"}
            ]
        }
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                return_value=events,
            ) as admin_call,
        ):
            response = backend.list_app_thread_events(
                "thread-1",
                {"activity": ["false"], "before": ["5"]},
            )

        admin_call.assert_called_once_with(
            "GET",
            "/v1/threads/thread-1/events"
            f"?limit={backend.THREAD_EVENT_PAGE}"
            f"&message_bytes={backend.THREAD_EVENT_MESSAGE_BYTES}"
            "&event_type=thread.message"
            "&event_type=thread.error"
            "&event_type=thread.stopped"
            "&before=5",
        )
        self.assertEqual(response, events)

    def test_list_app_thread_events_rejects_non_numeric_since(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.list_app_thread_events("thread-1", {"since": ["nope"]})

        self.assertEqual(error.exception.status, 400)

    def test_list_app_thread_events_rejects_combined_cursors(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_app_thread"),
            self.assertRaises(backend.AppError) as error,
        ):
            backend.list_app_thread_events(
                "thread-1",
                {"since": ["2"], "before": ["5"]},
            )

        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_stop_route_proxies_to_the_host_thread_stop(self) -> None:
        with (
            patch("host.apps.agent_chat.backend._require_app_thread") as require,
            patch(
                "host.apps.agent_chat.backend.call_admin_api",
                return_value={"status": "accepted"},
            ) as admin_call,
        ):
            status, body = self._request("POST", "/threads/thread-1/stop", {})

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "accepted"})
        require.assert_called_once_with("thread-1", include_archived=True)
        admin_call.assert_called_once_with("POST", "/v1/threads/thread-1/stop", {})

    def test_thread_tasks_route_is_removed(self) -> None:
        status, body = self._request("GET", "/threads/thread-1/tasks")

        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(body, {"error": {"message": "route not found"}})

    def test_archive_state_can_be_reversed(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            ("thread-1", True),
            ("thread-1", False),
        ]
        with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
            self.assertEqual(
                backend.archive_app_thread("thread-1"),
                {"thread_id": "thread-1", "archived": True},
            )
            self.assertEqual(
                backend.unarchive_app_thread("thread-1"),
                {"thread_id": "thread-1", "archived": False},
            )

        update_calls = [
            item for item in cursor.execute.call_args_list
            if "UPDATE threads SET archived" in item.args[0]
        ]
        self.assertEqual(
            [item.args[1] for item in update_calls],
            [(True, "thread-1"), (False, "thread-1")],
        )

    def test_thread_can_be_renamed_without_changing_its_id(self) -> None:
        cursor = unittest.mock.MagicMock()
        transaction = unittest.mock.MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchone.return_value = ("thread-1", "Release planning")
        with patch("host.apps.agent_chat.backend.db.transaction", return_value=transaction):
            self.assertEqual(
                backend.rename_app_thread("thread-1", {"name": "  Release planning  "}),
                {"thread_id": "thread-1", "name": "Release planning"},
            )

        update = next(
            item for item in cursor.execute.call_args_list
            if "UPDATE threads SET name" in item.args[0]
        )
        self.assertEqual(update.args[1], ("Release planning", "thread-1"))

    def test_thread_name_must_be_nonempty_and_bounded(self) -> None:
        for name in ("   ", "x" * (backend.THREAD_NAME_MAX_CHARS + 1)):
            with self.subTest(name=name), self.assertRaises(backend.AppError) as error:
                backend.rename_app_thread("thread-1", {"name": name})
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    unittest.main()
