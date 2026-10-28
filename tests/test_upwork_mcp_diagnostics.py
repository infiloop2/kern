"""Synthetic MCP errors through transport, adapter and Host warning capture."""

import io
import json
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from host.runtime.core.host_errors import _safe_context
from host.runtime.tools import tools_host
from host.tools import upwork
from host.tools.shared.web import ProviderWarning
from host.tools.upwork import mcp_http
import test_tools_upwork as fixtures


class UpworkMCPDiagnosticTests(unittest.TestCase):
    @contextmanager
    def transport(self, failure, *, sse=False, fail_tool="upwork__find_jobs", status=200):
        calls = []

        @contextmanager
        def respond(method, url, **kwargs):
            request = json.loads(kwargs["data"])
            calls.append(request)
            rpc_method = request["method"]
            if rpc_method == "notifications/initialized":
                yield io.BytesIO(b""), {}
                return
            tool = request["params"].get("name")
            if (fail_tool is None and rpc_method == "initialize") or tool == fail_tool:
                reply = failure
            elif rpc_method == "initialize":
                reply = {"result": {"protocolVersion": mcp_http.PROTOCOL}}
            elif tool == "upwork__find_jobs":
                reply = {"result": fixtures.UpworkTests.job_cost(self)}
            else:
                reply = {"result": fixtures.UpworkTests.preview(self)}
            envelope = {"jsonrpc": "2.0", "id": request["id"], **reply}
            raw = json.dumps(envelope).encode()
            stream = io.BytesIO(b"data: " + raw + b"\n\n" if sse else raw)
            if status is not None:
                stream.status = status
            yield stream, {"content-type": "text/event-stream" if sse else "application/json",
                           "mcp-session-id": "SECRET_SESSION"}

        with patch.object(mcp_http, "open_response_stream", side_effect=respond), patch.object(
            mcp_http, "_request_bytes_and_headers", return_value=(b"", {})) as close:
            yield calls, close

    def captured_job_error(self, failure, **transport_options):
        api = fixtures.connected_api()
        with self.transport(failure, **transport_options) as (calls, close), patch.object(
            tools_host, "enabled_tool", return_value=upwork.BUNDLED_TOOL
        ), patch.object(tools_host, "resolve_connection", return_value=Mock()), patch.object(
            tools_host, "host_api_for", return_value=api
        ), patch.object(tools_host, "_audit"), patch.object(tools_host.host_errors, "report_warning") as report:
            result = tools_host.execute_action("upwork", "get_job",
                {"org_uid": "org-one", "job_id": "123"}, origin_thread_id=None)
            close.assert_called_once()
        report.assert_called_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len([c for c in calls if c["method"] == "tools/call"]),
                         0 if transport_options.get("fail_tool", "x") is None else 1)
        context = _safe_context(report.call_args.kwargs["context"])
        return result, context

    def test_json_and_sse_error_envelopes_reach_operator_only(self):
        for sse in (False, True):
            for kind in ("jsonrpc_error", "tool_error"):
                with self.subTest(sse=sse, kind=kind):
                    message = "Job unavailable PRIVATE_ACCESS_TOKEN SECRET_SESSION"
                    failure = ({"error": {"code": -32001, "message": message,
                                          "data": {"secret": "OMIT_ERROR_DATA"}}}
                               if kind == "jsonrpc_error" else {"result": {"isError": True,
                                   "content": [{"type": "text", "text": message},
                                               {"type": "resource_link", "name": "OMIT_RESOURCE",
                                                "uri": "https://example.test/SECRET_CAPABILITY"}]}})
                    result, context = self.captured_job_error(failure, sse=sse)
                    self.assertEqual(context["operation"], "get_job")
                    self.assertEqual(context["mcp_error_kind"], kind)
                    self.assertEqual(context["http_status"], 200)
                    self.assertTrue(context["http_status_known"])
                    self.assertEqual(context["mcp_method"], "tools/call")
                    self.assertRegex(context["mcp_request_id"], r"^[0-9a-f]{32}$")
                    self.assertIn("Job unavailable", context["provider_response"])
                    if kind == "jsonrpc_error":
                        self.assertEqual(context["mcp_error_code"], -32001)
                    for private in ("PRIVATE_ACCESS_TOKEN", "SECRET_SESSION", "OMIT_ERROR_DATA",
                                    "OMIT_RESOURCE", "SECRET_CAPABILITY"):
                        self.assertNotIn(private, json.dumps(context))
                    self.assertNotIn("Job unavailable", json.dumps(result))
                    self.assertNotIn("provider_response", result)

    def test_redaction_precedes_multibyte_truncation_and_cross_block_join(self):
        failure = {"result": {"isError": True, "content": [
            {"type": "text", "text": "Job unavailable \\\\u0050RIVATE_"},
            {"type": "text", "text": "ACCESS_TOKEN PRIVATE_REFRESH_TOKEN " + "🙂" * 4000},
        ]}}
        result, context = self.captured_job_error(failure)
        self.assertIn("[redacted]", context["provider_response"])
        self.assertNotIn("RIVATE", json.dumps(context))
        self.assertTrue(context["provider_response_truncated"])
        self.assertLessEqual(len(context["provider_response"].encode()), 8192)
        self.assertNotIn("🙂", json.dumps(result, ensure_ascii=False))

    def test_redaction_failure_omits_content_without_losing_error_kind(self):
        failure = {"result": {"isError": True, "content": [{"type": "text", "text": "PRIVATE"}]}}
        with patch.object(upwork, "_redact", side_effect=ValueError("PRIVATE")):
            _, context = self.captured_job_error(failure)
        self.assertTrue(context["provider_response_redaction_failed"])
        self.assertTrue(context["provider_response_omitted"])
        self.assertNotIn("provider_response", context)
        self.assertNotIn("PRIVATE", json.dumps(context))

    def test_initialize_failure_and_unknown_http_status_are_explicit(self):
        failure = {"error": {"code": -32602, "message": "PRIVATE", "data": "PRIVATE"}}
        _, context = self.captured_job_error(failure, fail_tool=None, status=None)
        self.assertEqual(context["operation"], "MCP initialize")
        self.assertEqual(context["mcp_method"], "initialize")
        self.assertEqual(context["http_status"], 0)
        self.assertFalse(context["http_status_known"])
        self.assertEqual(context["mcp_error_code"], -32602)
        self.assertNotIn("PRIVATE", json.dumps(context))

    def test_approved_proposal_failure_stages_do_not_log_copy_or_replay(self):
        stages = [("upwork__manage_proposals", "submit_proposal", 2),
                  ("upwork__get_preview", "get_preview", 3),
                  ("upwork__confirm_preview", "confirm_preview", 4)]
        for tool, operation, count in stages:
            for kind in ("tool_error", "jsonrpc_error"):
                with self.subTest(tool=tool, kind=kind):
                    api = fixtures.connected_api()
                    # Queue with successful synthetic job lookup, then fail only
                    # the chosen stage of the single approved execution.
                    with self.transport({}, fail_tool="none"):
                        pending = upwork.BUNDLED_TOOL.execute("submit_proposal", {
                            "org_uid": "org-one", "job_reference": "123", "charged_amount": 50,
                            "cover_letter": "A specific proposal"}, api)
                    private = "A specific proposal PRIVATE_ACCESS_TOKEN PRIVATE_ANSWER"
                    failure = ({"error": {"code": -32000, "message": private}}
                               if kind == "jsonrpc_error" else {"result": {"isError": True,
                                   "content": [{"type": "text", "text": private}]}})
                    with self.transport(failure, fail_tool=tool) as (calls, close), self.assertRaises(ProviderWarning) as caught:
                        upwork.BUNDLED_TOOL.execute_approved(api.approvals.approve(pending.approval_id), api)
                    close.assert_called_once()
                    error = caught.exception
                    context = _safe_context(tools_host._provider_warning_context("upwork", "submit_proposal", error))
                    self.assertEqual(error.operation, operation)
                    self.assertEqual(context["mcp_error_kind"], kind)
                    self.assertTrue(context["provider_response_omitted"])
                    self.assertNotIn("provider_response", context)
                    self.assertEqual(len([c for c in calls if c["method"] == "tools/call"]), count)
                    for value in ("A specific proposal", "PRIVATE_ACCESS_TOKEN", "PRIVATE_ANSWER"):
                        self.assertNotIn(value, str(error) + json.dumps(context))

    def test_private_reads_and_message_writes_omit_error_text(self):
        for action, tool, params in (
            ("get_proposal", "upwork__list_freelancer_proposals", {"proposal_id": "123"}),
            ("read_messages", "upwork__get_messages", {"room_id": "room-one"}),
            ("send_message", "upwork__send_message", {"room_id": "room-one", "message": "Hello"}),
        ):
            with self.subTest(action=action):
                api = fixtures.connected_api()
                failure = {"result": {"isError": True, "content": [{"type": "text", "text": "PRIVATE_COPY"}]}}
                with self.transport(failure, fail_tool=tool), self.assertRaises(ProviderWarning) as caught:
                    result = upwork.BUNDLED_TOOL.execute(action, {"org_uid": "org-one", **params}, api)
                    if action == "send_message":
                        upwork.BUNDLED_TOOL.execute_approved(api.approvals.approve(result.approval_id), api)
                self.assertEqual(caught.exception.operation, action)
                self.assertEqual(caught.exception.response_body, "")
                self.assertNotIn("PRIVATE_COPY", str(caught.exception))
