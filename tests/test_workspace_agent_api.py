"""Agent-facing Unix socket and MCP shim contracts for the Workspace service."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection
from host.runtime.core import unix_socket_service
from host.runtime.workspace import agent_api, conversation_history
from host.runtime.workspace.web_apps import backend as web_apps


REPO_ROOT = Path(__file__).resolve().parents[1]


class AgentWorkspaceSocketTests(unittest.TestCase):
    def test_response_cap_can_carry_a_complete_maximum_app_document(self) -> None:
        self.assertGreater(
            agent_api.MAX_RESPONSE_BODY_BYTES,
            2 * web_apps.MAX_DATA_BYTES,
        )

    def start_server(self, *, agent_uids: frozenset[int] | None = None) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "agent.sock")
        server = agent_api.AgentWorkspaceServer(
            socket_path,
            agent_uids if agent_uids is not None else frozenset({os.getuid()}),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return socket_path

    def http(self, socket_path: str, method: str, path: str, body: dict | None = None):
        connection = UnixHTTPConnection(socket_path)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            try:
                connection.request(method, path, body=payload)
            except BrokenPipeError:
                pass
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw_http(self, socket_path: str, body: bytes) -> tuple[int, dict]:
        connection = UnixHTTPConnection(socket_path)
        try:
            connection.request("POST", "/call", body=body)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_rejects_non_agent_peer_before_dispatch(self) -> None:
        socket_path = self.start_server(agent_uids=frozenset({os.getuid() + 1}))
        with patch.object(web_apps, "route_agent") as route:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(socket_path)
                try:
                    client.sendall(
                        b"POST /call HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
                    )
                except BrokenPipeError:
                    pass
                try:
                    response = client.recv(1)
                except ConnectionResetError:
                    response = b""
                self.assertEqual(response, b"")
        route.assert_not_called()

    def test_explicit_agent_route_is_dispatched_without_thread_attribution(self) -> None:
        socket_path = self.start_server()
        with patch.object(
            web_apps, "route_agent", return_value={"app": {"app_id": "app-2"}}
        ) as route:
            status, body = self.http(
                socket_path,
                "POST",
                "/call",
                {"method": "GET", "path": "/agent/apps/app-2/state/meta"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": 200, "body": {"app": {"app_id": "app-2"}}})
        route.assert_called_once_with(
            "GET", "/agent/apps/app-2/state/meta", None, {}
        )

    def test_agent_app_creation_is_dispatched_without_a_request_body(self) -> None:
        socket_path = self.start_server()
        created = {"app": {"app_id": "app-3", "revision": 0}}
        with patch.object(web_apps, "route_agent", return_value=created) as route:
            status, body = self.http(
                socket_path,
                "POST",
                "/call",
                {"method": "POST", "path": "/agent/apps"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": 200, "body": created})
        route.assert_called_once_with("POST", "/agent/apps", None, {})

    def test_conversation_history_route_is_dispatched_to_host_proxy(self) -> None:
        socket_path = self.start_server()
        with patch.object(
            conversation_history, "route_agent", return_value={"matches": []}
        ) as route:
            status, body = self.http(
                socket_path,
                "POST",
                "/call",
                {
                    "method": "POST",
                    "path": "/agent/conversation-history/search",
                    "body": {"query": "deployment"},
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": 200, "body": {"matches": []}})
        route.assert_called_once_with(
            "POST",
            "/agent/conversation-history/search",
            {"query": "deployment"},
            {},
        )

    def test_conversation_history_proxy_forwards_public_contract_unchanged(self) -> None:
        request = {
            "query": "deployment",
            "thread_id": "app-2",
            "roles": ["assistant"],
            "cursor": "opaque",
        }
        with patch.object(
            conversation_history,
            "call_admin_api",
            return_value={"matches": []},
        ) as admin_call:
            response = conversation_history.route_agent(
                "POST",
                "/agent/conversation-history/search",
                request,
                {},
            )

        self.assertEqual(response, {"matches": []})
        admin_call.assert_called_once_with(
            "POST",
            "/v1/conversation-history/search",
            request,
        )

    def test_conversation_history_proxy_cannot_forward_arbitrary_admin_paths(self) -> None:
        with (
            patch.object(conversation_history, "call_admin_api") as admin_call,
            self.assertRaises(conversation_history.WorkspaceError) as error,
        ):
            conversation_history.route_agent(
                "POST",
                "/agent/conversation-history/unknown",
                {},
                {},
            )

        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)
        admin_call.assert_not_called()

    def test_workspace_bounds_history_requests_before_dispatch(self) -> None:
        with patch.object(conversation_history, "route_agent") as route:
            with self.assertRaisesRegex(ValueError, "body exceeds"):
                agent_api.dispatch_call(
                    "POST",
                    "/agent/conversation-history/search",
                    {"query": "x" * agent_api.MAX_REQUEST_BODY_BYTES},
                )
        route.assert_not_called()

    def test_workspace_bounds_history_responses_after_dispatch(self) -> None:
        with patch.object(
            conversation_history,
            "route_agent",
            return_value={"events": ["x" * agent_api.MAX_RESPONSE_BODY_BYTES]},
        ):
            with self.assertRaisesRegex(RuntimeError, "response too large"):
                agent_api.dispatch_call(
                    "POST",
                    "/agent/conversation-history/read",
                    {"thread_id": "app-1"},
                )

    def test_backend_validation_status_is_returned_inside_tool_result(self) -> None:
        socket_path = self.start_server()
        with patch.object(
            web_apps,
            "route_agent",
            side_effect=web_apps.WorkspaceError(HTTPStatus.UNPROCESSABLE_ENTITY, "bad action"),
        ):
            status, body = self.http(
                socket_path,
                "POST",
                "/call",
                {"method": "POST", "path": "/agent/apps/app-1/actions", "body": {}},
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"status": 422, "body": {"error": {"message": "bad action"}}},
        )

    def test_method_path_and_envelope_are_bounded(self) -> None:
        socket_path = self.start_server()
        invalid = (
            {"method": "GET", "path": "/threads"},
            {"method": "PATCH", "path": "/agent/apps"},
            {"method": "GET", "path": "/agent/../threads"},
            {"method": "GET", "path": "/agent/apps", "body": {}},
            {"method": "GET", "path": "/agent/apps", "extra": True},
        )
        for request in invalid:
            status, _body = self.http(socket_path, "POST", "/call", request)
            self.assertEqual(status, 400, request)

    def test_deep_json_is_rejected_without_disrupting_later_calls(self) -> None:
        socket_path = self.start_server()
        nested = b"[" * 1_500 + b"]" * 1_500
        status, body = self.raw_http(socket_path, b'{"body":' + nested + b"}")
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Request body must be JSON."})

        bounded = {"value": True}
        for _ in range(unix_socket_service.MAX_JSON_NESTING_DEPTH):
            bounded = {"nested": bounded}
        status, body = self.raw_http(
            socket_path,
            json.dumps({"body": bounded}).encode(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Request body is too deeply nested."})

        with patch.object(web_apps, "route_agent", return_value={"apps": []}):
            status, body = self.http(
                socket_path,
                "POST",
                "/call",
                {"method": "GET", "path": "/agent/apps"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": 200, "body": {"apps": []}})

    def test_response_cap_uses_the_actual_wire_encoding(self) -> None:
        response = {"a": 1, "b": 2}
        result = {"status": 200, "body": response}
        compact_size = len(json.dumps(result, separators=(",", ":")).encode())
        wire_size = len(json.dumps(result).encode())
        self.assertLess(compact_size, wire_size)
        with (
            patch.object(web_apps, "route_agent", return_value=response),
            patch.object(agent_api, "MAX_RESPONSE_BODY_BYTES", compact_size),
            self.assertRaises(RuntimeError),
        ):
            agent_api.dispatch_call("GET", "/agent/apps", None)

    def test_global_concurrency_cap_returns_429(self) -> None:
        socket_path = self.start_server()
        for _ in range(agent_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(agent_api._CALL_SLOTS.acquire(blocking=False))
        try:
            status, _ = self.http(
                socket_path,
                "POST",
                "/call",
                {"method": "GET", "path": "/agent/apps"},
            )
        finally:
            for _ in range(agent_api.MAX_CONCURRENT_CALLS):
                agent_api._CALL_SLOTS.release()
        self.assertEqual(status, 429)


class McpShimTests(unittest.TestCase):
    def start_server(self) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "agent.sock")
        server = agent_api.AgentWorkspaceServer(socket_path, frozenset({os.getuid()}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return socket_path

    def start_shim(self, socket_path: str) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env["KERN_WORKSPACE_AGENT_SOCKET"] = socket_path
        env["KERN_TOOLS_SOCKET"] = str(Path(socket_path).parent / "no-tools.sock")
        env["PYTHONPATH"] = str(REPO_ROOT)
        shim = subprocess.Popen(
            [sys.executable, "-m", "host.runtime.agent_shim.mcp_shim"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(shim.stdout.close)
        self.addCleanup(shim.wait)
        self.addCleanup(shim.stdin.close)
        return shim

    def rpc(self, shim: subprocess.Popen[str], message: dict) -> dict:
        shim.stdin.write(json.dumps(message) + "\n")
        shim.stdin.flush()
        return json.loads(shim.stdout.readline())

    def test_shim_lists_global_workspace_api_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shim = self.start_shim(str(Path(directory) / "missing.sock"))
            listing = self.rpc(
                shim, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
        description = tools["workspace_api"]["description"]
        self.assertIn("GET /agent/apps", description)
        self.assertIn("POST /agent/apps", description)
        self.assertIn("only when the operator explicitly asks", description)
        self.assertIn("explicit immutable app id", description)
        self.assertEqual(
            tools["workspace_api"]["inputSchema"]["required"], ["method", "path"]
        )
        tool_filter = tools["list_bundled_tools"]["inputSchema"]["properties"][
            "tool_ids"
        ]
        self.assertEqual(tool_filter["maxItems"], 32)
        self.assertTrue(tool_filter["uniqueItems"])
        search = tools["search_conversation_history"]
        search_limit = search["inputSchema"]["properties"]["limit"]
        self.assertEqual(search_limit["minimum"], 1)
        self.assertEqual(search_limit["maximum"], 25)
        self.assertIn("Set limit from 1 to 25", search["description"])
        self.assertIn("paginate with next_cursor", search["description"])
        self.assertIn("untrusted data", search["description"])
        read = tools["read_thread_history"]
        self.assertEqual(read["inputSchema"]["required"], ["thread_id"])
        self.assertEqual(read["inputSchema"]["properties"]["limit"]["maximum"], 50)
        self.assertEqual(
            read["inputSchema"]["properties"]["before"]["maxLength"],
            24,
        )

    def test_shim_calls_typed_conversation_history_tool(self) -> None:
        socket_path = self.start_server()
        with patch.object(
            conversation_history,
            "route_agent",
            return_value={"matches": [], "next_cursor": None},
        ) as route:
            shim = self.start_shim(socket_path)
            called = self.rpc(
                shim,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search_conversation_history",
                        "arguments": {"query": "deployment"},
                    },
                },
            )
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(
            json.loads(called["result"]["content"][0]["text"]),
            {"matches": [], "next_cursor": None},
        )
        route.assert_called_once_with(
            "POST",
            "/agent/conversation-history/search",
            {"query": "deployment"},
            {},
        )


if __name__ == "__main__":
    unittest.main()
