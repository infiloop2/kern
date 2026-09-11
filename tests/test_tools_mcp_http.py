"""Transport boundaries for bundled adapters of remote MCP providers."""

import json
import io
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from host.tools.upwork import mcp_http
from host.tools.shared.web import WebRequestError


class MCPHTTPTests(unittest.TestCase):
    def test_json_and_sse_results_match_request_ids(self):
        value = {"jsonrpc": "2.0", "id": "one", "result": {"tools": []}}
        raw = json.dumps(value).encode()
        self.assertEqual(mcp_http._result(raw, "application/json; charset=utf-8", "one"), {"tools": []})
        stream = b': keepalive\r\n\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\ndata: ' + raw + b"\r\n\r\n"
        self.assertEqual(mcp_http._stream_result(io.BytesIO(stream), "one"), {"tools": []})
        with self.assertRaises(RuntimeError):
            mcp_http._result(raw, "application/json", "two")

    def test_server_requests_and_provider_errors_are_never_executed_or_echoed(self):
        for message in (
            {"jsonrpc": "2.0", "id": "one", "method": "sampling/createMessage", "params": {"secret": "DO_NOT_ECHO"}},
            {"jsonrpc": "2.0", "id": "one", "error": {"message": "DO_NOT_ECHO"}},
            {"jsonrpc": "2.0", "id": "one", "result": []},
        ):
            with self.subTest(message=message), self.assertRaises(RuntimeError) as error:
                mcp_http._result(json.dumps(message).encode(), "application/json", "one")
            self.assertNotIn("DO_NOT_ECHO", str(error.exception))

    def test_invalid_json_content_types_and_duplicate_results_fail_closed(self):
        for raw, media in (
            (b"[]", "application/json"),
            (b"\xff", "application/json"),
            (b'{"jsonrpc":"2.0","id":"one","result":{"x":NaN}}', "application/json"),
            (b'{"jsonrpc":"2.0","id":"one","result":{"x":1e999}}', "application/json"),
            (b'{"jsonrpc":"2.0","id":"one","id":"one","result":{}}', "application/json"),
            (b'{"jsonrpc":"2.0","id":"one","result":{"isError":true,"isError":false}}', "application/json"),
            (b"<html>private</html>", "text/html"),
            (b'data: {"jsonrpc":"2.0","id":"one","result":{}}\n\n' * 2, "text/event-stream"),
        ):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                mcp_http._result(raw, media, "one")

    def test_sse_duplicate_envelope_keys_are_rejected(self):
        stream = b'data: {"jsonrpc":"2.0","id":"one","result":{"isError":true,"isError":false}}\n\n'
        with self.assertRaises(RuntimeError):
            mcp_http._stream_result(io.BytesIO(stream), "one")

    def test_initialization_advertises_no_client_capabilities_and_binds_session(self):
        requests = []

        @contextmanager
        def respond(method, url, **kwargs):
            payload = json.loads(kwargs["data"])
            requests.append((payload, dict(kwargs["headers"])))
            self.assertEqual(url, "https://mcp.upwork.com/mcp")
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(kwargs["headers"]["User-Agent"], "Kern/v1")
            if payload["method"] == "initialize":
                self.assertEqual(payload["params"]["capabilities"], {})
                yield io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": mcp_http.PROTOCOL}}).encode()), {"content-type": "application/json", "mcp-session-id": "session-1"}
                return
            if payload["method"] == "notifications/initialized":
                self.assertNotIn("id", payload)
                yield io.BytesIO(b""), {}
                return
            yield io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"content": []}}).encode()), {"content-type": "application/json"}

        with patch.object(mcp_http, "open_response_stream", side_effect=respond):
            connection = mcp_http.MCPConnection("PRIVATE_TOKEN")
            connection.initialize()
            self.assertEqual(connection.call("reviewed_read", {}), {"content": []})
        self.assertEqual(len(requests), 3)
        for payload, headers in requests[1:]:
            self.assertEqual(headers["Mcp-Session-Id"], "session-1")
            self.assertEqual(headers["MCP-Protocol-Version"], mcp_http.PROTOCOL)
            self.assertNotIn("PRIVATE_TOKEN", json.dumps(payload))

    def test_missing_or_malformed_content_is_not_success(self):
        connection = mcp_http.MCPConnection("token")
        for result in ({}, {"content": [{}]}, {"content": [{"type": "unknown"}]}, {"content": [{"type": "image"}]}, {"content": [{"type": "resource", "resource": {}}]}, {"structuredContent": {}}, {"content": None}, {"content": [{"type": "text"}]}, {"content": [], "isError": "false"}):
            with self.subTest(result=result), patch.object(connection, "request", return_value=result), self.assertRaises(RuntimeError):
                connection.call("fixed_read", {})

    def test_valid_non_text_blocks_are_omitted_without_fetching(self):
        result = {"content": [
            {"type": "text", "text": "Readable"},
            {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"},
            {"type": "audio", "data": "YXVkaW8=", "mimeType": "audio/wav"},
            {"type": "resource_link", "name": "Private", "uri": "https://example.test/private"},
            {"type": "resource", "resource": {"uri": "file:///private", "text": "PRIVATE"}},
            {"type": "resource", "resource": {"uri": "file:///binary", "blob": "cHJpdmF0ZQ=="}},
        ]}
        self.assertEqual(mcp_http.result_text(result), "Readable")

    def test_failed_call_is_not_replayed(self):
        with patch.object(mcp_http, "open_response_stream", side_effect=WebRequestError("redacted", status=404)) as request:
            with self.assertRaises(WebRequestError):
                mcp_http.MCPConnection("token").call("reviewed_write", {})
        request.assert_called_once()

    def test_error_tool_result_is_redacted(self):
        connection = mcp_http.MCPConnection("token")
        with patch.object(connection, "request", return_value={"isError": True, "content": [{"text": "PRIVATE"}]}):
            with self.assertRaisesRegex(RuntimeError, "provider request failed") as error:
                connection.call("reviewed_read", {})
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_streaming_sse_returns_without_waiting_for_eof(self):
        raw = (': keepalive\r\n\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n'
               'data: {"jsonrpc":"2.0","id":"one","result":{"text":"café"}}\r\n\r\n').encode()
        class OpenStream:
            def __init__(self):
                self.offset = 0
            def read(self, size):
                if self.offset == len(raw):
                    raise AssertionError("client waited for EOF after the result")
                chunk = raw[self.offset:self.offset + 1]
                self.offset += 1
                return chunk
        self.assertEqual(mcp_http._stream_result(OpenStream(), "one"), {"text": "café"})

    def test_streaming_sse_limits_bytes_notifications_and_elapsed_time(self):
        notification = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        for data in (b":" * (mcp_http.MAX_BYTES + 1), notification * 101, b"data: incomplete"):
            with self.subTest(size=len(data)), self.assertRaises(RuntimeError):
                mcp_http._stream_result(io.BytesIO(data), "one")
        with patch.object(mcp_http.time, "monotonic", side_effect=[0, 31]), self.assertRaises(RuntimeError):
            mcp_http._stream_result(io.BytesIO(notification), "one")

    def test_session_delete_is_once_and_cleanup_failure_does_not_replay(self):
        for failure in (None, WebRequestError("redacted", status=405), OSError("timeout")):
            connection = mcp_http.MCPConnection("token")
            connection.headers["Mcp-Session-Id"] = "session-one"
            with patch.object(mcp_http, "_request_bytes_and_headers", side_effect=failure) as request:
                connection.close()
                connection.close()
            request.assert_called_once()
            self.assertEqual(request.call_args.args, ("DELETE", mcp_http.ENDPOINT))
            self.assertEqual(request.call_args.kwargs["headers"]["Mcp-Session-Id"], "session-one")

    def test_trickling_json_obeys_wall_clock_and_updates_socket_timeout(self):
        clock = [0.0]
        stream = Mock(spec=mcp_http.http.client.HTTPResponse)
        stream.fp = Mock()
        def trickle(size):
            clock[0] += 20
            return b" "
        stream.read1.side_effect = trickle
        @contextmanager
        def respond(*args, **kwargs):
            yield stream, {"content-type": "application/json"}
        with patch.object(mcp_http.time, "monotonic", side_effect=lambda: clock[0]), patch.object(mcp_http, "open_response_stream", side_effect=respond):
            connection = mcp_http.MCPConnection("token")
            with self.assertRaisesRegex(RuntimeError, "deadline expired"):
                connection.call("fixed_read", {})
        self.assertEqual([call.args[0] for call in stream.fp.raw._sock.settimeout.call_args_list], [30, 10])
        self.assertEqual(stream.read1.call_count, 2)

    def test_shared_deadline_prevents_a_late_confirmation_request(self):
        clock = [0.0]
        @contextmanager
        def respond(*args, **kwargs):
            payload = json.loads(kwargs["data"])
            yield io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"content": []}}).encode()), {"content-type": "application/json"}
        with patch.object(mcp_http.time, "monotonic", side_effect=lambda: clock[0]), patch.object(mcp_http, "open_response_stream", side_effect=respond) as request:
            connection = mcp_http.MCPConnection("token", deadline=210)
            connection.call("upwork__get_preview", {})
            clock[0] = 211
            with self.assertRaisesRegex(RuntimeError, "deadline expired"):
                connection.call("upwork__confirm_preview", {})
        request.assert_called_once()
