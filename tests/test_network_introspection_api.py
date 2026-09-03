from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.agent_network import api as network_introspection_api
from host.runtime.core import state
from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection

REPO_ROOT = Path(__file__).resolve().parents[1]


class NetworkIntrospectionTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()

    def start_server(self, agent_uids: frozenset[int] | None = None) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "agent-network.sock")
        server = network_introspection_api.NetworkIntrospectionServer(
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
            connection.request(
                method,
                path,
                body=payload,
                headers={"Content-Type": "application/json"} if payload is not None else {},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_lists_every_integration_including_custom_domains(self) -> None:
        state.save_network_policy(
            {
                "network_integrations": {
                    "github": {
                        "enabled": True,
                        "write_repositories": [{"owner": "infiloop2", "repo": "kern"}],
                        "require_dot_github_approval": True,
                    },
                    "custom": {"domains": {"example.com": {"allow_http_methods": ["GET"]}}},
                },
            },
            "2026-07-16T00:00:00Z",
        )

        result = network_introspection_api.call_action("list_network_integrations", {})

        by_id = {
            entry["integration_id"]: entry
            for entry in result["result"]["network_integrations"]
        }
        self.assertEqual(
            sorted(by_id),
            [
                "bedrock",
                "claude",
                "custom",
                "github",
                "npm_packages",
                "openai",
                "python_packages",
                "xai",
            ],
        )
        self.assertTrue(by_id["github"]["enabled"])
        self.assertEqual(
            by_id["github"]["options"],
            {
                "write_repositories": [{"owner": "infiloop2", "repo": "kern"}],
                "require_dot_github_approval": True,
            },
        )
        self.assertTrue(by_id["custom"]["enabled"])
        self.assertEqual(
            by_id["custom"]["options"]["domains"],
            {"example.com": {"allow_http_methods": ["GET"]}},
        )
        self.assertFalse(by_id["openai"]["enabled"])

    def test_recent_denials_are_joined_with_catalog_guidance(self) -> None:
        state.append_network_event("https", "GET", "allowed.example.com", 443, "/", "", True)
        state.append_network_event(
            "https", "POST", "github.com", 443,
            "/acme/website.git/git-receive-pack", "", False,
            "github_write_repo_required",
        )
        state.append_network_event(
            "https", "CONNECT", "evil.example.com", 443, "", "", False,
            "host_not_allowed",
        )

        result = network_introspection_api.call_action("recent_network_denials", {})

        denials = result["result"]["denials"]
        self.assertEqual([denial["host"] for denial in denials], ["evil.example.com", "github.com"])
        self.assertIn("write repositories", denials[1]["guidance"])
        self.assertIn("custom-domain rule", denials[0]["guidance"])

    def test_repeated_denials_collapse_into_counted_entries(self) -> None:
        for _ in range(3):
            state.append_network_event(
                "https", "GET", "poll.example.com", 443, "/v1/bundle", "", False,
                "host_not_allowed",
            )
        state.append_network_event(
            "https", "CONNECT", "evil.example.com", 443, "", "", False,
            "host_not_allowed",
        )

        result = network_introspection_api.call_action("recent_network_denials", {"limit": 2})

        denials = result["result"]["denials"]
        self.assertEqual(
            [denial["host"] for denial in denials], ["evil.example.com", "poll.example.com"]
        )
        self.assertEqual(denials[0]["count"], 1)
        self.assertNotIn("first_timestamp", denials[0])
        self.assertEqual(denials[1]["count"], 3)
        self.assertLessEqual(denials[1]["first_timestamp"], denials[1]["timestamp"])

    def test_denial_counts_include_duplicates_past_the_limit_page(self) -> None:
        state.append_network_event(
            "https", "GET", "poll.example.com", 443, "/v1/bundle", "", False,
            "host_not_allowed",
        )
        for host in ("one.example.com", "two.example.com"):
            state.append_network_event(
                "https", "CONNECT", host, 443, "", "", False, "host_not_allowed"
            )
        state.append_network_event(
            "https", "GET", "poll.example.com", 443, "/v1/bundle", "", False,
            "host_not_allowed",
        )

        result = network_introspection_api.call_action("recent_network_denials", {"limit": 2})

        denials = result["result"]["denials"]
        self.assertEqual(
            [denial["host"] for denial in denials], ["poll.example.com", "two.example.com"]
        )
        self.assertEqual(denials[0]["count"], 2)

    def test_denial_result_marks_a_bounded_row_scan_as_truncated(self) -> None:
        for _ in range(4):
            state.append_network_event(
                "https", "GET", "poll.example.com", 443, "/v1/bundle", "", False,
                "host_not_allowed",
            )

        with patch.object(network_introspection_api, "_DENIAL_ROW_SCAN_LIMIT", 3):
            result = network_introspection_api.call_action("recent_network_denials", {})

        self.assertTrue(result["result"]["truncated"])
        self.assertEqual(result["result"]["denials"][0]["count"], 3)

    def test_denial_result_marks_a_bounded_distinct_scan_as_truncated(self) -> None:
        for host in ("older.example.com", "newer.example.com"):
            state.append_network_event(
                "https", "CONNECT", host, 443, "", "", False, "host_not_allowed"
            )

        with patch.object(network_introspection_api, "_DENIAL_DISTINCT_SCAN_LIMIT", 1), \
                patch.object(network_introspection_api, "_DENIAL_PAGE_SIZE", 1):
            result = network_introspection_api.call_action("recent_network_denials", {})

        self.assertTrue(result["result"]["truncated"])
        self.assertEqual(
            [denial["host"] for denial in result["result"]["denials"]],
            ["newer.example.com"],
        )

    def test_distinct_budget_stops_inside_the_page_that_exhausts_it(self) -> None:
        for index in range(6):
            state.append_network_event(
                "https", "CONNECT", f"host{index}.example.com", 443, "", "", False,
                "host_not_allowed",
            )

        with patch.object(network_introspection_api, "_DENIAL_DISTINCT_SCAN_LIMIT", 2), \
                patch.object(network_introspection_api, "_DENIAL_PAGE_SIZE", 6):
            result = network_introspection_api.call_action("recent_network_denials", {})

        # The budget is exhausted four rows into the single page; the rest of
        # that page must not be admitted just because it was already fetched.
        self.assertEqual(
            [denial["host"] for denial in result["result"]["denials"]],
            ["host5.example.com", "host4.example.com"],
        )
        self.assertTrue(result["result"]["truncated"])

    def test_repeat_denials_do_not_consume_the_distinct_scan_budget(self) -> None:
        state.append_network_event(
            "https", "CONNECT", "rare.example.com", 443, "", "", False,
            "host_not_allowed",
        )
        for _ in range(network_introspection_api._DENIAL_DISTINCT_SCAN_LIMIT):
            state.append_network_event(
                "https", "GET", "poll.example.com", 443, "/v1/bundle", "", False,
                "host_not_allowed",
            )

        result = network_introspection_api.call_action("recent_network_denials", {})

        denials = result["result"]["denials"]
        self.assertEqual(
            [denial["host"] for denial in denials],
            ["poll.example.com", "rare.example.com"],
        )
        self.assertEqual(
            denials[0]["count"], network_introspection_api._DENIAL_DISTINCT_SCAN_LIMIT
        )
        self.assertEqual(denials[1]["count"], 1)
        self.assertFalse(result["result"]["truncated"])

    def test_limit_validation_and_socket_peer_gate(self) -> None:
        for bad in (0, 101, "5", True):
            with self.subTest(limit=bad), self.assertRaisesRegex(
                network_introspection_api.NetworkToolCallError, "limit"
            ):
                network_introspection_api.call_action("recent_network_denials", {"limit": bad})

        rejected_socket = self.start_server(frozenset())
        status, _body = self.http(rejected_socket, "GET", "/tools")
        self.assertEqual(status, 403)

    def test_mcp_shim_aggregates_the_dedicated_socket(self) -> None:
        network_socket = self.start_server()
        env = os.environ.copy()
        env["KERN_TOOLS_SOCKET"] = "/nonexistent/tools.sock"
        env["KERN_AGENT_NETWORK_SOCKET"] = network_socket
        env["PYTHONPATH"] = str(REPO_ROOT)
        shim = subprocess.Popen(
            [sys.executable, "-m", "host.runtime.agent_shim.mcp_shim"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        def stop_shim() -> None:
            # Closing stdin is the exit signal; the wait is bounded so a
            # wedged shim fails this test instead of hanging the suite; stdout
            # is closed last so the pipe fd is not leaked into later tests.
            shim.stdin.close()
            try:
                shim.wait(timeout=30)
            except subprocess.TimeoutExpired:
                shim.kill()
                shim.wait(timeout=30)
            finally:
                shim.stdout.close()

        self.addCleanup(stop_shim)

        shim.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        shim.stdin.flush()
        listed = json.loads(shim.stdout.readline())
        # The listing is static, so an unreachable tools socket changes nothing
        # about it; the network tools are declared here either way and the call
        # below is what proves this shim reaches the dedicated socket.
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            [
                "list_bundled_tools",
                "describe_tool",
                "call_tool",
                "check_tool_approval",
                "list_network_integrations",
                "recent_network_denials",
                "stage_image",
                "stage_video",
                "search_conversation_history",
                "read_thread_history",
                "workspace_api",
            ],
        )

        shim.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_network_integrations", "arguments": {}},
        }) + "\n")
        shim.stdin.flush()
        called = json.loads(shim.stdout.readline())
        self.assertFalse(called["result"]["isError"])
        tool_result = json.loads(called["result"]["content"][0]["text"])
        self.assertIn("network_integrations", tool_result)
        self.assertNotIn("result", tool_result)
        self.assertIn(
            "custom",
            {entry["integration_id"] for entry in tool_result["network_integrations"]},
        )


if __name__ == "__main__":
    unittest.main()
