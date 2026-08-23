from __future__ import annotations

import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.agent_runtime import grok_agent, thread_scope
from host.runtime.agent_runtime.grok_agent import GrokAcpServer, GrokAgentError


# A scripted stand-in for `grok agent stdio`, speaking the ACP framing the real
# binary speaks: JSON-RPC 2.0, newline delimited, extension methods carrying the
# leading underscore. The shapes below are the ones grok 1.0.5 actually
# returned on a live subscription login.
FAKE_SERVER = r"""
import json, sys

AUTHENTICATED = %s
FAIL_SUBSCRIPTION = %s

def send(obj):
    obj.setdefault("jsonrpc", "2.0")
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"id": mid, "result": {"protocolVersion": 1,
                                    "authMethods": [{"id": "grok.com"}]}})
    elif method == "_x.ai/auth/info":
        send({"id": mid, "result": {
            "methodId": "grok.com" if AUTHENTICATED else None,
            "email": "operator@example.com" if AUTHENTICATED else None,
            "teamId": "team-1" if AUTHENTICATED else None,
            "organizationId": None,
            "principalType": "User" if AUTHENTICATED else None,
            "principalId": "acct-1" if AUTHENTICATED else None,
            "teamBlockedReasons": ["BLOCKED_REASON_NO_LOGS"] if AUTHENTICATED else [],
            "codingDataRetentionOptOut": True if AUTHENTICATED else None,
        }})
    elif method == "_x.ai/auth/check_subscription":
        if FAIL_SUBSCRIPTION:
            send({"id": mid, "error": {"code": -32000,
                                       "message": "permission-denied",
                                       "data": "log into console.x.ai and update the permissions"}})
        else:
            send({"id": mid, "result": {"authenticated": True, "meta": {}}})
    elif method == "_x.ai/billing":
        send({"id": mid, "result": {"config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                              "start": "2026-08-12T12:09:12+00:00",
                              "end": "2026-08-19T12:09:12+00:00"},
            "onDemandCap": {"val": 0}, "prepaidBalance": {"val": 0},
            "isUnifiedBillingUser": True}, "subscription_tier": "SuperGrok"}})
    elif method == "_x.ai/auth/get_url":
        send({"id": mid, "result": {
            "auth_url": "https://accounts.x.ai/oauth2/device?user_code=563J-PW2K",
            "external_provider": False, "mode": "device"}})
    elif method == "authenticate":
        # Long-running on the real server: the response only arrives once the
        # operator approves. This stand-in answers immediately.
        send({"id": mid, "result": {"_meta": {"email": "operator@example.com"}}})
"""


def fake_command(authenticated: bool = True, fail_subscription: bool = False) -> list[str]:
    return [sys.executable, "-c", FAKE_SERVER % (authenticated, fail_subscription)]


FAKE_TURN_SERVER = r"""
import json, sys

MODE = sys.argv[1]
prompt_id = None
session_id = None

def send(obj):
    obj.setdefault("jsonrpc", "2.0")
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params", {})
    if method == "initialize":
        send({"id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        meta = params.get("_meta", {})
        if params.get("cwd") != "/mnt/kern-agent/agent-home" or params.get("mcpServers") != []:
            send({"id": mid, "error": {"message": "bad new-session boundary"}})
        elif meta.get("modelId") != "grok-4.6" or meta.get("reasoningEffort") != "high" or meta.get("yoloMode") is not True:
            send({"id": mid, "error": {"message": "bad model metadata"}})
        else:
            session_id = "grok-session-new"
            send({"id": mid, "result": {"sessionId": session_id}})
    elif method == "session/load":
        if MODE == "missing":
            send({"id": mid, "error": {"message": "session not found: deleted"}})
            continue
        session_id = params.get("sessionId")
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD"}},
              "_meta": {"isReplay": True}}})
        send({"id": mid, "result": {"sessionId": session_id}})
    elif method == "session/prompt":
        prompt_id = mid
        if params.get("prompt") != [{"type": "text", "text": "hello"}]:
            send({"id": mid, "error": {"message": "bad prompt blocks"}})
            continue
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Think"}}}})
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": " harder"}}}})
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "tool_call", "toolCallId": "tool-1", "title": "Run tests", "kind": "execute", "status": "pending"}}})
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "tool_call_update", "toolCallId": "tool-1", "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}]}}})
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello"}}}})
        if MODE != "steer":
            send({"method": "session/update", "params": {"sessionId": session_id,
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " world"}}}})
            send({"id": prompt_id, "result": {"stopReason": "end_turn"}})
    elif method == "_x.ai/interject":
        if params.get("sessionId") != session_id or params.get("text") != "also test":
            send({"id": mid, "error": {"message": "bad interjection"}})
            continue
        send({"id": mid, "result": {"result": {"status": "queued"}}})
        send({"method": "session/update", "params": {"sessionId": session_id,
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " steered"}}}})
        send({"id": prompt_id, "result": {"stopReason": "end_turn"}})
"""


def turn_command(mode: str = "normal") -> list[str]:
    return [sys.executable, "-c", FAKE_TURN_SERVER, mode]


class GrokAcpTransportTests(unittest.TestCase):
    def test_initialize_negotiates_the_pinned_protocol(self) -> None:
        server = GrokAcpServer(command=fake_command())
        try:
            server.start(init_timeout=30)
            self.assertEqual(server.initialize_result["protocolVersion"], 1)
        finally:
            server.close()

    def test_a_different_protocol_major_is_refused(self) -> None:
        command = [sys.executable, "-c", (
            'import json,sys\n'
            'for line in sys.stdin:\n'
            '    m=json.loads(line)\n'
            '    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":m["id"],'
            '"result":{"protocolVersion":2}})+"\\n"); sys.stdout.flush()\n'
        )]
        server = GrokAcpServer(command=command)
        try:
            with self.assertRaises(GrokAgentError) as error:
                server.start(init_timeout=30)
            self.assertIn("protocol 2", str(error.exception))
        finally:
            server.close()

    def test_a_missing_protocol_version_is_refused(self) -> None:
        command = [sys.executable, "-c", (
            'import json,sys\n'
            'for line in sys.stdin:\n'
            '    m=json.loads(line)\n'
            '    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":m["id"],'
            '"result":{}})+"\\n"); sys.stdout.flush()\n'
        )]
        server = GrokAcpServer(command=command)
        try:
            with self.assertRaisesRegex(GrokAgentError, "protocol None"):
                server.start(init_timeout=30)
        finally:
            server.close()

    def test_an_agent_request_is_answered_rather_than_ignored(self) -> None:
        # ACP is bidirectional. An unanswered agent->client request would wedge
        # the agent behind a reply that never comes, so the client refuses it
        # explicitly.
        command = [sys.executable, "-c", (
            'import json,sys\n'
            'first=True\n'
            'for line in sys.stdin:\n'
            '    m=json.loads(line)\n'
            '    if m.get("method")=="initialize":\n'
            '        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":m["id"],'
            '"result":{"protocolVersion":1}})+"\\n"); sys.stdout.flush()\n'
            '        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":9001,'
            '"method":"fs/read_text_file","params":{}})+"\\n"); sys.stdout.flush()\n'
            '    elif m.get("id")==9001:\n'
            '        sys.stdout.write(json.dumps({"jsonrpc":"2.0",'
            '"method":"echo","params":{"code":m.get("error",{}).get("code")}})+"\\n")\n'
            '        sys.stdout.flush()\n'
        )]
        server = GrokAcpServer(command=command)
        try:
            server.start(init_timeout=30)
            echoed = server.read_message(timeout=15)
            self.assertEqual(echoed["params"]["code"], grok_agent.JSONRPC_METHOD_NOT_FOUND)
        finally:
            server.close()

    def test_unrelated_messages_do_not_reset_a_call_timeout(self) -> None:
        server = GrokAcpServer()
        with (
            patch.object(server, "_write_request_locked", return_value=7),
            patch.object(
                server,
                "_next_message",
                side_effect=[{"method": "status/update"}, {"id": 7, "result": "ok"}],
            ) as next_message,
            patch.object(grok_agent.time, "monotonic", side_effect=[10.0, 11.0, 12.0]),
        ):
            self.assertEqual(server.call("probe", {}, timeout=5.0), "ok")

        self.assertEqual(
            [entry.args[0] for entry in next_message.call_args_list],
            [4.0, 3.0],
        )

    def test_the_launcher_takes_no_web_search_argument(self) -> None:
        # Web search is not offered, so the launcher passes
        # --disable-web-search unconditionally and there is no decision for
        # this module to state.
        server = GrokAcpServer()
        self.assertEqual(
            server._command[len(grok_agent.DEFAULT_COMMAND)], "--thread-scope"
        )
        self.assertFalse([arg for arg in server._command if arg.startswith("web-search")])

    def test_a_non_turn_production_server_gets_a_unique_named_scope(self) -> None:
        with patch.object(grok_agent.secrets, "token_hex", return_value="a" * 16):
            server = GrokAcpServer(command=grok_agent.PRODUCTION_COMMAND)

        self.assertEqual(server._thread_id, "grok-probe-" + "a" * 16)
        self.assertEqual(
            server._command,
            [
                *grok_agent.PRODUCTION_COMMAND,
                "--thread-scope",
                "grok-probe-" + "a" * 16,
            ],
        )

    def test_close_reaps_a_non_turn_production_scope(self) -> None:
        with (
            patch.object(grok_agent.secrets, "token_hex", return_value="b" * 16),
            patch.object(
                thread_scope.subprocess,
                "run",
                return_value=MagicMock(returncode=0),
            ) as run,
        ):
            GrokAcpServer(command=grok_agent.PRODUCTION_COMMAND).close()

        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "grok-probe-" + "b" * 16],
        )


class GrokTurnTests(unittest.TestCase):
    def test_new_session_streams_activity_and_returns_the_answer(self) -> None:
        ready: list[bool] = []
        session_ids: list[str] = []
        events: list[str | dict[str, object]] = []
        server = GrokAcpServer(
            command=turn_command(),
            on_ready=lambda: ready.append(True) is None or True,
            on_session_id=session_ids.append,
        )
        try:
            server.start()
            session_id, answer = grok_agent.run_turn(
                server, "hello", None, "grok-4.6", "high", events.append
            )
        finally:
            server.close()

        self.assertEqual((session_id, answer), ("grok-session-new", "Hello world"))
        self.assertEqual(ready, [True])
        self.assertEqual(session_ids, ["grok-session-new"])
        self.assertEqual(events[-1], "Hello world")
        activities = [event for event in events if isinstance(event, dict)]
        self.assertEqual(
            [(event["kind"], event["phase"]) for event in activities],
            [
                ("reasoning", "started"),
                ("reasoning", "started"),
                ("command", "started"),
                ("command", "completed"),
                ("reasoning", "completed"),
            ],
        )
        self.assertEqual(activities[3]["title"], "Run tests")
        # Each streamed chunk carries only its own text. Carrying the trace so
        # far would re-store every prefix, which is quadratic in the length of
        # the reasoning. The completed record holds the whole trace.
        reasoning = [event for event in activities if event["kind"] == "reasoning"]
        self.assertEqual(
            [event["detail"] for event in reasoning],
            ["Think", " harder", "Think harder"],
        )
        # The streamed chunks are marked append-only so the reader rebuilds the
        # trace; the completed record carries the whole thing and replaces it.
        self.assertEqual(
            [event.get("append_detail") for event in reasoning],
            [True, True, None],
        )

    def test_resume_loads_the_session_and_does_not_replay_old_history(self) -> None:
        events: list[str | dict[str, object]] = []
        server = GrokAcpServer(command=turn_command())
        try:
            server.start()
            session_id, answer = grok_agent.run_turn(
                server, "hello", "existing-session", "grok-4.6", "high", events.append
            )
        finally:
            server.close()

        self.assertEqual((session_id, answer), ("existing-session", "Hello world"))
        self.assertNotIn("OLD", events)

    def test_missing_resumed_session_has_a_typed_error(self) -> None:
        server = GrokAcpServer(command=turn_command("missing"))
        try:
            server.start()
            with self.assertRaises(grok_agent.GrokSessionNotFoundError):
                grok_agent.run_turn(
                    server, "hello", "deleted", "grok-4.6", "high", lambda _event: None
                )
        finally:
            server.close()

    def test_live_steer_is_acknowledged_and_folded_into_the_running_prompt(self) -> None:
        events: list[str | dict[str, object]] = []
        ready = threading.Event()
        result: list[tuple[str, str]] = []
        errors: list[BaseException] = []
        server = GrokAcpServer(
            command=turn_command("steer"),
            on_ready=lambda: ready.set() is None or True,
        )
        server.start()

        def run() -> None:
            try:
                result.append(
                    grok_agent.run_turn(
                        server, "hello", None, "grok-4.6", "high", events.append
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        try:
            worker.start()
            self.assertTrue(ready.wait(timeout=10))
            server.steer("also test")
            worker.join(timeout=10)
        finally:
            server.close()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [("grok-session-new", "Hello steered")])

    def test_steer_is_rejected_when_prompt_response_precedes_its_ack(self) -> None:
        server = GrokAcpServer(command=turn_command())
        server._active_session_id = "session-1"
        server._active_prompt_id = 10
        server._accepting_steers = True
        server._response_sequences[10] = 1

        def acknowledge(request_id: int, *, timeout: float) -> dict[str, object]:
            self.assertEqual((request_id, timeout), (11, 30))
            server._response_sequences[request_id] = 2
            return {"result": {"result": {"status": "queued"}}}

        with (
            patch.object(server, "begin_call", return_value=11),
            patch.object(server, "wait_response", side_effect=acknowledge),
            self.assertRaises(grok_agent.GrokTurnFinishing),
        ):
            server.steer("too late")
        self.assertFalse(server._accepting_steers)

    def test_late_interjection_error_is_turn_finishing(self) -> None:
        server = GrokAcpServer(command=turn_command())
        server._active_session_id = "session-1"
        server._active_prompt_id = 10
        server._accepting_steers = True
        server._response_sequences[10] = 1

        def reject(request_id: int, *, timeout: float) -> dict[str, object]:
            self.assertEqual((request_id, timeout), (11, 30))
            server._response_sequences[request_id] = 2
            return {"error": {"message": "prompt already completed"}}

        with (
            patch.object(server, "begin_call", return_value=11),
            patch.object(server, "wait_response", side_effect=reject),
            self.assertRaises(grok_agent.GrokTurnFinishing),
        ):
            server.steer("too late")
        self.assertFalse(server._accepting_steers)

    def test_interjection_timeout_after_prompt_response_is_turn_finishing(self) -> None:
        server = GrokAcpServer(command=turn_command())
        server._active_session_id = "session-1"
        server._active_prompt_id = 10
        server._accepting_steers = True

        def time_out(request_id: int, *, timeout: float) -> dict[str, object]:
            self.assertEqual((request_id, timeout), (11, 30))
            server._response_sequences[10] = 1
            raise grok_agent.GrokTimeout("timed out")

        with (
            patch.object(server, "begin_call", return_value=11),
            patch.object(server, "wait_response", side_effect=time_out),
            self.assertRaises(grok_agent.GrokTurnFinishing),
        ):
            server.steer("too late")
        self.assertFalse(server._accepting_steers)

    def test_interjection_send_failure_after_prompt_response_is_turn_finishing(self) -> None:
        server = GrokAcpServer(command=turn_command())
        server._active_session_id = "session-1"
        server._active_prompt_id = 10
        server._accepting_steers = True

        def fail_send(_method: str, _params: dict[str, object]) -> int:
            server._response_sequences[10] = 1
            raise BrokenPipeError("closed")

        with (
            patch.object(server, "begin_call", side_effect=fail_send),
            self.assertRaises(grok_agent.GrokTurnFinishing),
        ):
            server.steer("too late")
        self.assertFalse(server._accepting_steers)

    def test_interjection_send_failure_before_prompt_completion_is_typed(self) -> None:
        server = GrokAcpServer(command=turn_command())
        server._active_session_id = "session-1"
        server._active_prompt_id = 10
        server._accepting_steers = True

        with (
            patch.object(server, "begin_call", side_effect=BrokenPipeError("closed")),
            self.assertRaisesRegex(GrokAgentError, "transport closed"),
        ):
            server.steer("still active")


class GrokAccountStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        grok_agent.clear_live_validation_failure()
        self.addCleanup(grok_agent.clear_live_validation_failure)

    def status(self, *, authenticated=True, fail_subscription=False, account=None,
               attested=None, attest_error=None, **kwargs):
        if account is not None:
            account = dict(account)
            account.setdefault("access_token_sha256", "a" * 64)
            if attested is None and attest_error is None:
                attested = {"account_id": account.get("account_id")}
        with (
            patch.object(grok_agent, "DEFAULT_COMMAND", fake_command(authenticated, fail_subscription)),
            patch.object(grok_agent, "read_grok_account", return_value=account),
            patch.object(
                grok_agent,
                "read_attested_identity",
                return_value=attested,
                side_effect=attest_error,
            ),
        ):
            return grok_agent.account_status(**kwargs)

    def test_a_logged_out_server_awaits_login(self) -> None:
        status, detail, metadata = self.status(authenticated=False)
        self.assertEqual(status, "awaiting_login")
        self.assertIsNone(detail)
        self.assertIsNone(metadata)

    def test_an_active_account_carries_its_anchor_and_usage(self) -> None:
        status, detail, metadata = self.status(
            account={"account_id": "acct-1", "access_token_sha256": "a" * 64},
            attested={"account_id": "acct-1", "email": "operator@example.com"},
        )
        self.assertEqual((status, detail), ("active", None))
        self.assertEqual(metadata["account_id"], "acct-1")
        self.assertEqual(metadata["email"], "operator@example.com")
        self.assertEqual(metadata["team_id"], "team-1")
        self.assertEqual(metadata["access_token_sha256"], "a" * 64)
        self.assertIs(metadata["coding_data_retention_opt_out"], True)
        self.assertIs(metadata["zdr_enabled"], True)

    def test_coding_data_opt_out_is_inactive_when_grok_reports_opted_in(self) -> None:
        metadata = grok_agent._safe_account_metadata(
            {"codingDataRetentionOptOut": False}
        )
        self.assertIs(metadata["coding_data_retention_opt_out"], False)

    def test_coding_data_opt_out_is_unknown_when_absent_or_malformed(self) -> None:
        self.assertNotIn(
            "coding_data_retention_opt_out", grok_agent._safe_account_metadata({})
        )
        self.assertNotIn(
            "coding_data_retention_opt_out",
            grok_agent._safe_account_metadata(
                {"codingDataRetentionOptOut": "true"}
            ),
        )

    def test_zdr_is_inactive_when_grok_reports_no_zdr_team_reason(self) -> None:
        metadata = grok_agent._safe_account_metadata({"teamBlockedReasons": []})
        self.assertIs(metadata["zdr_enabled"], False)

    def test_zdr_is_unknown_when_the_grok_field_is_absent_or_malformed(self) -> None:
        self.assertNotIn("zdr_enabled", grok_agent._safe_account_metadata({}))
        self.assertNotIn(
            "zdr_enabled",
            grok_agent._safe_account_metadata({"teamBlockedReasons": "BLOCKED_REASON_NO_LOGS"}),
        )

    def test_usage_is_omitted_when_the_provider_reports_no_percentage(self) -> None:
        # xAI omits the percentage on a unified-billing subscription. Grok's own
        # client falls back to 0.0, which would paint a permanently green bar.
        _status, _detail, metadata = self.status(account={"account_id": "acct-1"})
        self.assertNotIn("grok_usage", metadata)

    def test_a_readable_percentage_becomes_the_usage_block(self) -> None:
        usage = grok_agent._safe_usage_metadata({
            "config": {
                "creditUsagePercent": 42.5,
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                  "end": "2026-08-19T12:09:12+00:00"},
            },
            "subscription_tier": "SuperGrok",
        })
        self.assertEqual(usage["usage_percent"], 42.5)
        self.assertEqual(usage["period_type"], "weekly")
        self.assertEqual(usage["subscription_tier"], "SuperGrok")
        self.assertIn("resets_at", usage)

    def test_an_unreadable_account_never_reports_active(self) -> None:
        # Reporting active without the anchor would show a connected badge over
        # a cleared pin, with every data-plane request denied.
        status, detail, metadata = self.status(account=None)
        self.assertEqual(status, "error")
        self.assertIn("account id is unavailable", detail)
        self.assertIsNone(metadata)

    def test_the_shown_email_is_the_one_xai_attests(self) -> None:
        # Neither the agent's own report nor a decoded claim: the card shows
        # what xAI returns for this exact token. The stand-in ACP server reports
        # operator@example.com and the auth file carries claimed@example.com;
        # neither is what the operator sees.
        _status, _detail, metadata = self.status(
            account={
                "account_id": "acct-1",
                "email": "claimed@example.com",
                "access_token_sha256": "a" * 64,
            },
            attested={"account_id": "acct-1", "email": "attested@example.com"},
        )
        self.assertEqual(metadata["email"], "attested@example.com")
        self.assertEqual(metadata["account_id"], "acct-1")

    def test_an_unattested_account_never_reports_active(self) -> None:
        status, detail, metadata = self.status(
            account={
                "account_id": "acct-1",
                "email": "claimed@example.com",
                "access_token_sha256": "a" * 64,
            },
            attest_error=grok_agent.GrokAgentError("could not attest the Grok account"),
        )
        self.assertEqual(status, "error")
        self.assertIn("could not attest", detail)
        self.assertIsNone(metadata)

    def test_an_attestation_for_another_account_fails_closed(self) -> None:
        status, detail, metadata = self.status(
            account={"account_id": "acct-1", "access_token_sha256": "a" * 64},
            attested={"account_id": "acct-2", "email": "elsewhere@example.com"},
        )
        self.assertEqual(status, "error")
        self.assertIn("different account", detail)
        self.assertIsNone(metadata)

    def test_a_disagreeing_account_id_fails_closed(self) -> None:
        status, detail, _metadata = self.status(account={"account_id": "someone-else"})
        self.assertEqual(status, "error")
        self.assertIn("different account", detail)

    def test_an_entitlement_refusal_is_an_error_not_a_login_prompt(self) -> None:
        # A fresh login cannot fix a console-team permission problem, so routing
        # the operator into one would be a dead end.
        status, detail, _metadata = self.status(
            account={"account_id": "acct-1"}, fail_subscription=True
        )
        self.assertEqual(status, "error")
        self.assertIn("permission-denied", detail)

    def test_the_entitlement_verdict_is_remembered_between_polls(self) -> None:
        self.status(account={"account_id": "acct-1"}, fail_subscription=True)
        # The second poll reuses the verdict instead of generating fresh
        # provider traffic; a server that would now succeed changes nothing.
        status, detail, _metadata = self.status(account={"account_id": "acct-1"})
        self.assertEqual(status, "error")
        self.assertIn("permission-denied", detail)

    def test_an_operator_refresh_bypasses_the_remembered_verdict(self) -> None:
        self.status(account={"account_id": "acct-1"}, fail_subscription=True)
        status, _detail, _metadata = self.status(
            account={"account_id": "acct-1"}, force_provider_probe=True
        )
        self.assertEqual(status, "active")


class GrokAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        grok_agent._ATTESTED_IDENTITY.clear()
        grok_agent._ATTESTATION_FAILURES.clear()
        self.addCleanup(grok_agent._ATTESTED_IDENTITY.clear)
        self.addCleanup(grok_agent._ATTESTATION_FAILURES.clear)

    def test_identity_is_bound_to_the_observed_hash_and_memoized(self) -> None:
        token_hash = "a" * 64
        proc = MagicMock(
            returncode=0,
            stdout=(
                '{"access_token_sha256":"%s","account_id":"acct-1",'
                '"email":"operator@example.com"}' % token_hash
            ),
            stderr="",
        )
        with patch.object(grok_agent, "_run_account_helper", return_value=proc) as run:
            expected = {
                "account_id": "acct-1",
                "email": "operator@example.com",
            }
            self.assertEqual(grok_agent.read_attested_identity(token_hash), expected)
            self.assertEqual(grok_agent.read_attested_identity(token_hash), expected)
        run.assert_called_once_with(
            [*grok_agent.DEFAULT_ACCOUNT_COMMAND, "--attest"]
        )

    def test_identity_for_a_different_token_is_rejected(self) -> None:
        proc = MagicMock(
            returncode=0,
            stdout=(
                '{"access_token_sha256":"%s","account_id":"acct-1",'
                '"email":"operator@example.com"}' % ("b" * 64)
            ),
            stderr="",
        )
        with (
            patch.object(grok_agent, "_run_account_helper", return_value=proc),
            self.assertRaisesRegex(GrokAgentError, "token changed"),
        ):
            grok_agent.read_attested_identity("a" * 64)

    def test_provider_rejection_is_an_account_error(self) -> None:
        proc = MagicMock(returncode=1, stdout="", stderr="HTTP 401")
        with (
            patch.object(grok_agent, "_run_account_helper", return_value=proc),
            self.assertRaisesRegex(GrokAgentError, "HTTP 401"),
        ):
            grok_agent.read_attested_identity("a" * 64)

    def test_failed_attestation_is_backed_off_then_retried(self) -> None:
        token_hash = "a" * 64
        proc = MagicMock(returncode=1, stdout="", stderr="provider unavailable")
        with (
            patch.object(grok_agent, "_run_account_helper", return_value=proc) as run,
            patch.object(
                grok_agent.time,
                "monotonic",
                side_effect=[100.0, 101.0, 341.0, 342.0],
            ),
        ):
            for _ in range(3):
                with self.assertRaisesRegex(GrokAgentError, "provider unavailable"):
                    grok_agent.read_attested_identity(token_hash)
        self.assertEqual(run.call_count, 2)

    def test_operator_refresh_bypasses_failed_attestation_backoff(self) -> None:
        token_hash = "a" * 64
        proc = MagicMock(returncode=1, stdout="", stderr="provider unavailable")
        with (
            patch.object(grok_agent, "_run_account_helper", return_value=proc) as run,
            patch.object(grok_agent.time, "monotonic", side_effect=[100.0, 101.0]),
        ):
            with self.assertRaises(GrokAgentError):
                grok_agent.read_attested_identity(token_hash)
            with self.assertRaises(GrokAgentError):
                grok_agent.read_attested_identity(token_hash, force=True)
        self.assertEqual(run.call_count, 2)

    def test_incomplete_provider_identity_is_rejected(self) -> None:
        proc = MagicMock(
            returncode=0,
            stdout='{"access_token_sha256":"%s"}' % ("a" * 64),
            stderr="",
        )
        with (
            patch.object(grok_agent, "_run_account_helper", return_value=proc),
            self.assertRaisesRegex(GrokAgentError, "incomplete"),
        ):
            grok_agent.read_attested_identity("a" * 64)


class GrokLoginTests(unittest.TestCase):
    def tearDown(self) -> None:
        grok_agent.close_login_server()

    def test_a_device_login_publishes_the_code_from_the_url(self) -> None:
        # get_url returns no code field of its own; it is a query parameter.
        with patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()):
            login = grok_agent.start_device_login()
        self.assertEqual(login.user_code, "563J-PW2K")
        self.assertTrue(login.login_url.startswith("https://accounts.x.ai/"))

    def test_a_non_device_login_mode_is_refused(self) -> None:
        # A loopback flow would redirect to a port nothing here is listening on,
        # so it would hang rather than fail.
        command = [sys.executable, "-c", (
            'import json,sys\n'
            'for line in sys.stdin:\n'
            '    m=json.loads(line)\n'
            '    if m.get("method")=="initialize":\n'
            '        r={"protocolVersion":1}\n'
            '    elif m.get("method")=="_x.ai/auth/get_url":\n'
            '        r={"auth_url":"http://localhost:1410/cb","mode":"loopback"}\n'
            '    else:\n'
            '        continue\n'
            '    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":r})+"\\n")\n'
            '    sys.stdout.flush()\n'
        )]
        with patch.object(grok_agent, "DEFAULT_COMMAND", command):
            with self.assertRaises(GrokAgentError) as error:
                grok_agent.start_device_login()
        self.assertIn("unsupported login mode", str(error.exception))

    def test_an_existing_credential_is_distinguished_from_a_missing_url(self) -> None:
        with patch.object(grok_agent, "GrokAcpServer") as server_type:
            server = server_type.return_value
            server.begin_call.return_value = 2
            server.call.side_effect = [
                None,
                {"methodId": "grok.com", "principalId": "acct-1"},
            ]
            with self.assertRaises(grok_agent.GrokLoginAlreadyAuthenticated):
                grok_agent.start_device_login()
        server.close.assert_called_once()

    def test_initialization_failure_closes_the_server(self) -> None:
        with patch.object(grok_agent, "GrokAcpServer") as server_type:
            server = server_type.return_value
            server.start.side_effect = GrokAgentError("initialization failed")
            with self.assertRaisesRegex(GrokAgentError, "initialization failed"):
                grok_agent.start_device_login()
        server.close.assert_called_once()

    def test_each_login_gets_its_own_id(self) -> None:
        # Two openings in the same second normally share an authenticate id --
        # a fresh server reaches authenticate as request 2 almost every time --
        # so an id built from those two parts collides. The finishing refresh
        # closes the parked server by login id, so a collision reaps the
        # replacement and strands its device code.
        with patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()):
            first = grok_agent.start_device_login()
            second = grok_agent.start_device_login()
        self.assertNotEqual(first.login_id, second.login_id)
        grok_agent.close_login_server()

    def test_the_anchor_is_captured_when_the_login_resolves(self) -> None:
        with patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()):
            login = grok_agent.start_device_login()
            # The status poller is the sole reader of the parked server, so it
            # is what observes the resolved authenticate.
            with patch.object(
                grok_agent,
                "read_grok_account",
                return_value={"account_id": "acct-1"},
            ):
                grok_agent.account_status()
            self.assertEqual(
                grok_agent.read_completed_login_account_id(login.login_id), "acct-1"
            )

    def test_completed_login_identity_does_not_follow_the_auth_file(self) -> None:
        with (
            patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()),
            patch.object(
                grok_agent,
                "read_grok_account",
                return_value={
                    "account_id": "attacker-account",
                    "access_token_sha256": "a" * 64,
                },
            ),
            patch.object(
                grok_agent,
                "read_attested_identity",
                return_value={"account_id": "attacker-account"},
            ),
        ):
            login = grok_agent.start_device_login()
            status, detail, _account = grok_agent.account_status()
            self.assertEqual(status, "error")
            self.assertIn("different account", str(detail))
            self.assertEqual(
                grok_agent.read_completed_login_account_id(login.login_id), "acct-1"
            )

    def test_completion_waits_until_the_acp_identity_is_available(self) -> None:
        with (
            patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()),
            patch.object(
                grok_agent,
                "_authenticated_account_id",
                side_effect=[None, "acct-1"],
            ),
        ):
            login = grok_agent.start_device_login()
            grok_agent.account_status()
            self.assertIsNone(
                grok_agent.read_completed_login_account_id(login.login_id)
            )
            grok_agent.account_status()
            self.assertEqual(
                grok_agent.read_completed_login_account_id(login.login_id), "acct-1"
            )

    def test_completion_retries_a_transient_acp_identity_read(self) -> None:
        with patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()):
            login = grok_agent.start_device_login()
            server = grok_agent._current_login_server()
            self.assertIsNotNone(server)
            assert server is not None
            with patch.object(
                server,
                "call",
                side_effect=[
                    GrokAgentError("auth info is settling"),
                    {"principalType": "User", "principalId": "acct-1"},
                ],
            ):
                grok_agent._collect_parked_login(server)
                self.assertIsNone(
                    grok_agent.read_completed_login_account_id(login.login_id)
                )
                grok_agent._collect_parked_login(server)
            self.assertEqual(
                grok_agent.read_completed_login_account_id(login.login_id), "acct-1"
            )

    def test_an_uncompleted_login_yields_no_account(self) -> None:
        with patch.object(grok_agent, "DEFAULT_COMMAND", fake_command()):
            login = grok_agent.start_device_login()
            self.assertIsNone(grok_agent.read_completed_login_account_id(login.login_id))


class GrokUserCodeTests(unittest.TestCase):
    def test_the_code_is_read_from_any_supported_query_key(self) -> None:
        for key in ("user_code", "userCode", "code"):
            self.assertEqual(
                grok_agent._user_code_from_url(f"https://accounts.x.ai/d?{key}=ABC-123"),
                "ABC-123",
            )

    def test_a_url_without_a_code_is_not_invented(self) -> None:
        self.assertIsNone(grok_agent._user_code_from_url("https://accounts.x.ai/device"))


if __name__ == "__main__":
    unittest.main()
