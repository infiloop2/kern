"""Agent tools surface tests: the Unix-socket service and the MCP stdio shim.

The service runs in-process against the scratch database; the shim runs as a
real subprocess speaking newline-delimited JSON-RPC, exactly as the agent
harnesses launch it.
"""

from __future__ import annotations

from contextlib import contextmanager
import io
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import pg_harness
from test_tools_host import FakeTool

from host.runtime.core import state
from host.runtime.tools import api as tools_api, tools_host
from host.runtime.agent_shim import mcp_shim as tools_mcp_shim
from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection
from host.tools import OpenedStreamingAsset, StreamingAsset

REPO_ROOT = Path(__file__).resolve().parents[1]

# The agent's whole tool surface, in listing order. It is spelled out here
# rather than derived from the shim so that any change to what the model sees
# has to be made deliberately: these declarations head the prompt, and moving
# them re-encodes every cached context behind them.
EXPECTED_SHIM_TOOLS = [
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
]


class _MemoryResponse:
    def __init__(self, data: bytes, **headers: str) -> None:
        self._source = io.BytesIO(data)
        self._headers = headers

    def getheader(self, name: str, default: str = "") -> str:
        return self._headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        return self._source.read(size)


class StreamMaterializationUnitTests(unittest.TestCase):
    def test_materializes_exact_stream_with_private_modes(self) -> None:
        payload = b"v" * 512
        response = _MemoryResponse(
            payload,
            **{
                "Content-Length": str(len(payload)),
                "Content-Type": "video/mp4",
                "X-Kern-Filename": "generated.mp4",
            },
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory}):
            result = tools_mcp_shim._materialize_stream(response)  # type: ignore[arg-type]
            local_path = Path(directory) / result["path"].lstrip("/")
            self.assertEqual(local_path.read_bytes(), payload)
            self.assertEqual(local_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(local_path.parent.stat().st_mode & 0o777, 0o700)

    def test_short_stream_leaves_no_partial_file(self) -> None:
        response = _MemoryResponse(
            b"short",
            **{
                "Content-Length": "512",
                "Content-Type": "video/mp4",
                "X-Kern-Filename": "generated.mp4",
            },
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory}):
            with self.assertRaisesRegex(RuntimeError, "ended early"):
                tools_mcp_shim._materialize_stream(response)  # type: ignore[arg-type]
            self.assertEqual(list((Path(directory) / "tool_assets").iterdir()), [])

    def test_rejects_symlinked_tool_assets_directory(self) -> None:
        response = _MemoryResponse(
            b"x",
            **{
                "Content-Length": "1",
                "Content-Type": "application/octet-stream",
                "X-Kern-Filename": "generated.bin",
            },
        )
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as target:
            (Path(directory) / "tool_assets").symlink_to(target)
            with patch.dict(os.environ, {"HOME": directory}), self.assertRaisesRegex(
                RuntimeError, "storage is unavailable"
            ):
                tools_mcp_shim._materialize_stream(response)  # type: ignore[arg-type]
            self.assertEqual(list(Path(target).iterdir()), [])


class ToolsApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        registry_patch = patch.dict(tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()})
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        with state.mutation() as cur:
            state.save_tool_config_value(cur, "fake_notes", "FAKE_NOTES_TOKEN", "token-1")
            state.set_tool_enabled(cur, "fake_notes", True)


class ActionListingTests(ToolsApiTestCase):
    def test_listing_is_the_static_discovery_surface(self) -> None:
        listing = tools_api.action_listing()
        self.assertEqual(
            [entry["name"] for entry in listing],
            ["list_bundled_tools", "describe_tool", "call_tool", "check_tool_approval"],
        )
        self.assertTrue(all(entry["input_schema"]["type"] == "object" for entry in listing))
        catalog_schema = listing[0]["input_schema"]["properties"]["tool_ids"]
        self.assertEqual(catalog_schema["maxItems"], 32)
        self.assertTrue(catalog_schema["uniqueItems"])

    def test_listing_does_not_move_with_operator_state(self) -> None:
        # The declarations head the model prompt, so anything that varies with
        # enablement re-encodes the whole cached context when an operator
        # toggles an integration.
        baseline = tools_api.action_listing()
        with patch.object(state, "enabled_tool_ids", return_value=set(tools_host.BUNDLED_TOOLS)):
            self.assertEqual(tools_api.action_listing(), baseline)
        with patch.object(state, "enabled_tool_ids", return_value=set()):
            self.assertEqual(tools_api.action_listing(), baseline)

    def test_listing_entries_cannot_be_mutated_by_a_caller(self) -> None:
        tools_api.action_listing()[0]["name"] = "clobbered"
        self.assertEqual(tools_api.action_listing()[0]["name"], "list_bundled_tools")

    def test_describe_tool_exposes_every_bundled_action_contract(self) -> None:
        for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
            described = tools_api.call_action("describe_tool", {"tool_id": tool_id})
            self.assertEqual(described["status"], "executed")
            by_id = {entry["id"]: entry for entry in described["result"]["actions"]}
            for action in tool.manifest.actions:
                with self.subTest(tool_id=tool_id, action=action.id):
                    self.assertEqual(by_id[action.id]["description"], action.description)
                    self.assertEqual(by_id[action.id]["input_schema"], action.input_schema)
                    self.assertEqual(by_id[action.id]["approval"], action.approval)
                    # The result shape travels with the call shape, and only
                    # for the actions that return a JSON result at all.
                    self.assertEqual(
                        by_id[action.id].get("output_schema", {}), action.output_schema
                    )

        def described_action(tool_id: str, action_id: str) -> dict[str, Any]:
            result = tools_api.call_action("describe_tool", {"tool_id": tool_id})["result"]
            return {entry["id"]: entry for entry in result["actions"]}[action_id]

        self.assertIn("individual tradable questions", described_action("polymarket", "list_markets")["description"])
        self.assertIn("umbrella topics", described_action("polymarket", "list_events")["description"])
        self.assertIn("not public-post", described_action("instagram", "get_recent_media")["description"])
        self.assertIn("not an objective global ranking", described_action("instagram_discovery", "get_trending_reels")["description"])
        self.assertIn("not a LinkedIn feed", described_action("linkedin_discovery", "search_posts")["description"])

        # A described result names its fields and says what each one means, so
        # the agent can plan the next call without running this one first.
        messages = described_action("gmail", "search_messages")["output_schema"]["properties"]["messages"]
        self.assertIn("newest first", messages["description"])
        self.assertIn("read_message", messages["items"]["properties"]["id"]["description"])
        # An approval-gated action returns an approval outcome, and an
        # asset-returning action writes a file: neither carries a JSON result.
        self.assertNotIn("output_schema", described_action("gmail", "send_email"))
        self.assertNotIn("output_schema", described_action("openai_images", "generate_image"))

    def test_agent_notes_are_stated_only_for_focused_catalog_entries(self) -> None:
        broad_catalog = {
            entry["tool_id"]: entry
            for entry in tools_api.call_action("list_bundled_tools", {})["result"]["tools"]
        }
        for entry in broad_catalog.values():
            self.assertNotIn("agent_notes", entry)
            self.assertNotIn("actions", entry)

        catalog = {
            entry["tool_id"]: entry
            for entry in tools_api.call_action(
                "list_bundled_tools", {"tool_ids": list(tools_host.BUNDLED_TOOLS)}
            )["result"]["tools"]
        }
        # Always stated, empty included, so "this tool has nothing to add" is
        # distinguishable from "this surface does not carry it".
        for tool_id, entry in catalog.items():
            with self.subTest(tool_id=tool_id):
                self.assertEqual(
                    entry["agent_notes"],
                    tools_host.BUNDLED_TOOLS[tool_id].manifest.agent_notes,
                )
        self.assertEqual(catalog["gmail"]["agent_notes"], "")
        # An agent planning from the catalog learns the approval-gated API
        # post path and that DMs remain operator-sent compose links.
        self.assertIn("post_tweet", catalog["twitter"]["agent_notes"])
        self.assertIn("x.com/messages/compose", catalog["twitter"]["agent_notes"])

        # Broad discovery goes directly to describe_tool, so the described tool
        # repeats its one short note to make that documented path self-contained.
        described = tools_api.call_action("describe_tool", {"tool_id": "twitter"})["result"]
        self.assertEqual(described["agent_notes"], catalog["twitter"]["agent_notes"])
        for action in described["actions"]:
            with self.subTest(action=action["id"]):
                self.assertNotIn("agent_notes", action)

    def test_describe_tool_reports_enablement_and_rejects_unknown_ids(self) -> None:
        described = tools_api.call_action("describe_tool", {"tool_id": "fake_notes"})
        self.assertTrue(described["result"]["enabled"])
        self.assertEqual(described["result"]["display_name"], "Fake Notes")
        self.assertFalse(tools_api.call_action("describe_tool", {"tool_id": "gmail"})["result"]["enabled"])
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown tool_id"):
            tools_api.call_action("describe_tool", {"tool_id": "nope"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "must be a non-empty string"):
            tools_api.call_action("describe_tool", {})
        with self.assertRaisesRegex(tools_host.ToolCallError, "only tool_id"):
            tools_api.call_action("describe_tool", {"tool_id": "fake_notes", "extra": 1})

    def test_call_tool_runs_an_action_and_rejects_bad_addresses(self) -> None:
        result = tools_api.call_action(
            "call_tool", {"tool_id": "fake_notes", "action_id": "read_note", "input": {}}
        )
        self.assertEqual(result["status"], "executed")
        # A missing input is an empty object, not a crash.
        self.assertEqual(
            tools_api.call_action("call_tool", {"tool_id": "fake_notes", "action_id": "read_note"})["status"],
            "executed",
        )
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown tool_id"):
            tools_api.call_action("call_tool", {"tool_id": "nope", "action_id": "read_note"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown action_id"):
            tools_api.call_action("call_tool", {"tool_id": "fake_notes", "action_id": "nope"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "must be a non-empty string"):
            tools_api.call_action("call_tool", {"tool_id": "fake_notes"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "only tool_id, action_id, connection_id, and input"):
            tools_api.call_action(
                "call_tool", {"tool_id": "fake_notes", "action_id": "read_note", "extra": 1}
            )
        # A disabled tool is addressable but refuses, so the agent can tell the
        # operator which integration to enable.
        with self.assertRaisesRegex(tools_host.ToolCallError, "not enabled"):
            tools_api.call_action("call_tool", {"tool_id": "gmail", "action_id": "search_messages"})

    def test_flat_action_names_stay_callable_though_unlisted(self) -> None:
        # Approval records and audit rows address actions this way.
        listed = [entry["name"] for entry in tools_api.action_listing()]
        self.assertNotIn("fake_notes_read_note", listed)
        self.assertEqual(tools_api.call_action("fake_notes_read_note", {})["status"], "executed")

    def test_list_bundled_tools_reports_the_catalog_with_enablement(self) -> None:
        # Enabled and disabled bundled tools both appear, distinguished by the
        # enabled flag, so the agent can ask the operator to enable an existing
        # tool instead of rebuilding it.
        result = tools_api.call_action("list_bundled_tools", {})
        self.assertEqual(result["status"], "executed")
        by_id = {entry["tool_id"]: entry for entry in result["result"]["tools"]}
        self.assertTrue(by_id["fake_notes"]["enabled"])
        gmail = by_id["gmail"]
        self.assertFalse(gmail["enabled"])
        self.assertEqual(gmail["connection"], "oauth")
        self.assertEqual(gmail["display_name"], "Gmail")
        self.assertNotIn("actions", gmail)

        focused = tools_api.call_action(
            "list_bundled_tools", {"tool_ids": ["fake_notes", "gmail"]}
        )["result"]["tools"]
        focused_by_id = {entry["tool_id"]: entry for entry in focused}
        self.assertEqual(
            [action["id"] for action in focused_by_id["fake_notes"]["actions"]],
            ["read_note", "crash_note", "write_note"],
        )
        self.assertIn(
            "search_messages",
            [action["id"] for action in focused_by_id["gmail"]["actions"]],
        )

    def test_call_tool_selects_one_of_multiple_connected_accounts(self) -> None:
        for connection_id, account_id, label, text in (
            ("connection_first", "acct-1", "first@example.com", "first"),
            ("connection_second", "acct-2", "second@example.com", "second"),
        ):
            tools_host.HostCredentials(
                "fake_notes", tools_host.ConnectionScope(connection_id, None)
            ).save(
                {
                    "account": {"id": account_id, "label": label, "scopes": ["notes"]},
                    "secret": {"text": text},
                    "metadata": {},
                }
            )
        catalog = tools_api.call_action(
            "list_bundled_tools", {"tool_ids": ["fake_notes"]}
        )["result"]["tools"][0]
        self.assertEqual(
            [item["connection_id"] for item in catalog["connected_accounts"]],
            ["connection_first", "connection_second"],
        )
        with self.assertRaisesRegex(tools_host.ToolCallError, "multiple connected accounts"):
            tools_api.call_action(
                "call_tool", {"tool_id": "fake_notes", "action_id": "read_note"}
            )
        result = tools_api.call_action(
            "call_tool",
            {
                "tool_id": "fake_notes",
                "action_id": "read_note",
                "connection_id": "connection_second",
            },
        )
        self.assertEqual(result["result"]["text"], "second")

    def test_list_bundled_tools_filters_known_ids_and_reports_unknown_ids(self) -> None:
        result = tools_api.call_action(
            "list_bundled_tools",
            {"tool_ids": ["twitter", "missing-tool", "fake_notes"]},
        )["result"]
        self.assertEqual(
            [entry["tool_id"] for entry in result["tools"]],
            ["twitter", "fake_notes"],
        )
        self.assertEqual(result["unknown_tool_ids"], ["missing-tool"])
        self.assertTrue(result["tools"][1]["enabled"])

        unfiltered = tools_api.call_action("list_bundled_tools", {})["result"]
        self.assertNotIn("unknown_tool_ids", unfiltered)

    def test_list_bundled_tools_rejects_invalid_filters(self) -> None:
        invalid_inputs = (
            None,
            {"extra": True},
            {"tool_ids": []},
            {"tool_ids": ["twitter", "twitter"]},
            {"tool_ids": [""]},
            {"tool_ids": [7]},
            {"tool_ids": [f"tool-{index}" for index in range(33)]},
        )
        for tool_input in invalid_inputs:
            with self.subTest(tool_input=tool_input), self.assertRaises(
                tools_host.ToolCallError
            ):
                tools_api.call_action("list_bundled_tools", tool_input)

    def test_catalog_carries_descriptions_but_not_schemas(self) -> None:
        # Descriptions are what the agent plans from; schemas are what it needs
        # only once it commits to a call, so they stay behind describe_tool.
        catalog = tools_api.call_action(
            "list_bundled_tools", {"tool_ids": ["fake_notes"]}
        )["result"]["tools"]
        by_id = {entry["tool_id"]: entry for entry in catalog}
        for action in by_id["fake_notes"]["actions"]:
            self.assertTrue(action["description"])
            self.assertNotIn("input_schema", action)
        by_action = {action["id"]: action for action in by_id["fake_notes"]["actions"]}
        # Only the exceptional case is spelled out; direct actions stay silent.
        self.assertEqual(by_action["write_note"]["approval"], "operator")
        self.assertNotIn("approval", by_action["read_note"])

    def test_call_action_resolves_names_and_rejects_unknowns(self) -> None:
        result = tools_api.call_action("fake_notes_read_note", {})
        self.assertEqual(result["status"], "executed")
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown tool"):
            tools_api.call_action("fake_notes_missing", {})
        with self.assertRaisesRegex(tools_host.ToolCallError, "not enabled"):
            tools_api.call_action("gmail_search_messages", {})  # resolvable but disabled
        with self.assertRaisesRegex(tools_host.ToolCallError, "must be a string"):
            tools_api.call_action(7, {})

    def test_check_tool_approval_reports_status(self) -> None:
        tools_host.HostCredentials(
            "fake_notes", tools_host.ConnectionScope("connection_test", None)
        ).save(
            {
                "account": {
                    "id": "acct-1",
                    "label": "notes@example.com",
                    "scopes": ["notes"],
                },
                "secret": {"text": ""},
                "metadata": {},
            }
        )
        pending = tools_api.call_action("fake_notes_write_note", {"text": "hello"})
        self.assertEqual(pending["status"], "pending_approval")
        # The token is folded into the id; no separate field.
        self.assertNotIn("approval_check_token", pending)
        check_input = {"approval_id": pending["approval_id"]}
        checked = tools_api.call_action("check_tool_approval", check_input)
        self.assertEqual(checked["result"]["approval_status"], "pending")
        tools_host.decide_approval(pending["approval_id"], "approve")
        checked = tools_api.call_action("check_tool_approval", check_input)
        self.assertEqual(checked["result"]["approval_status"], "executed")
        self.assertEqual(checked["result"]["execution_result"], "Wrote the note (5 chars).")

        failed = tools_api.call_action("fake_notes_write_note", {"text": "fail"})
        tools_host.decide_approval(failed["approval_id"], "approve")
        checked = tools_api.call_action("check_tool_approval", {"approval_id": failed["approval_id"]})
        self.assertEqual(checked["result"]["approval_status"], "failed")
        self.assertEqual(checked["result"]["execution_result"], "Note write failed.")
        # A right-shaped id with the wrong token, and a guessed sequential
        # number, both fail closed.
        number = pending["approval_id"].split(".", 1)[0]
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown approval"):
            tools_api.call_action("check_tool_approval", {"approval_id": number + ".wrong-token"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown approval"):
            tools_api.call_action("check_tool_approval", {"approval_id": number})
        with self.assertRaisesRegex(tools_host.ToolCallError, "requires approval_id"):
            tools_api.call_action("check_tool_approval", {})


class ToolsSocketTests(ToolsApiTestCase):
    def start_server(
        self,
        agent_uids: frozenset[int] | None = None,
        admin_uids: frozenset[int] | None = None,
    ) -> str:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "tools.sock")
        server = tools_api.ToolsServer(
            socket_path,
            agent_uids if agent_uids is not None else frozenset({os.getuid()}),
            admin_uids if admin_uids is not None else frozenset({os.getuid()}),
        )
        self.last_server = server
        import threading

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
                # Early responses close the connection before the body is fully
                # sent: the pre-body 429 on the agent call cap races the client's
                # body write. The response already delivered on the AF_UNIX
                # socket stays readable, so fall through and read it.
                pass
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw_http(self, socket_path: str, request: bytes) -> tuple[int, dict]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(socket_path)
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        header, _, body = response.partition(b"\r\n\r\n")
        status = int(header.split(None, 2)[1])
        return status, json.loads(body)

    def raw_json_http(
        self, socket_path: str, method: str, path: str, body: dict
    ) -> tuple[int, dict]:
        payload = json.dumps(body).encode()
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: local\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        return self.raw_http(socket_path, request)

    def test_server_sweeps_expired_assets_once_per_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = tools_api.ToolsServer(
                str(root / "tools.sock"),
                frozenset({os.getuid()}),
                asset_root=root / "assets",
            )
            self.addCleanup(server.server_close)
            server._next_asset_cleanup = 999.0
            with (
                patch("host.runtime.tools.api.time.monotonic", return_value=1_000.0),
                patch.object(server.asset_store, "cleanup_expired") as cleanup,
            ):
                server.service_actions()
                server.service_actions()

            cleanup.assert_called_once_with()
            self.assertEqual(
                server._next_asset_cleanup,
                1_000.0 + tools_api.ASSET_CLEANUP_INTERVAL_SECONDS,
            )

    def test_serves_listing_and_calls_over_the_socket(self) -> None:
        socket_path = self.start_server()
        status, body = self.http(socket_path, "GET", "/tools")
        self.assertEqual(status, 200)
        self.assertEqual(
            [tool["name"] for tool in body["tools"]],
            ["list_bundled_tools", "describe_tool", "call_tool", "check_tool_approval"],
        )

        status, body = self.http(
            socket_path,
            "POST",
            "/call",
            {"name": "call_tool", "input": {"tool_id": "fake_notes", "action_id": "read_note"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "executed")

        status, body = self.http(socket_path, "POST", "/call", {"name": "nope", "input": {}})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "failed")
        self.assertIn("Unknown tool", body["error"])

        status, body = self.http(socket_path, "GET", "/nope")
        self.assertEqual(status, 404)

    def test_rejects_peers_outside_the_allowed_uids(self) -> None:
        socket_path = self.start_server(agent_uids=frozenset({0}), admin_uids=frozenset({0}))
        status, body = self.http(socket_path, "GET", "/tools")
        self.assertEqual(status, 403)
        self.assertIn("Peer not allowed", body["error"])

    def test_peers_are_scoped_strictly_by_path(self) -> None:
        # The agent peer gets exactly the MCP surface: its uid is rejected on
        # the operator delegation routes.
        agent_only = self.start_server(
            agent_uids=frozenset({os.getuid()}), admin_uids=frozenset({0})
        )
        status, _ = self.http(agent_only, "GET", "/tools")
        self.assertEqual(status, 200)
        status, body = self.raw_http(
            agent_only,
            b"POST /operator/tools/fake_notes/oauth_connect/disconnect HTTP/1.1\r\n"
            b"Host: local\r\n"
            b"Content-Length: 0\r\n\r\n",
        )
        self.assertEqual(status, 403)
        self.assertIn("admin peer", body["error"])
        # The admin peer gets exactly the operator routes: its uid is rejected
        # on the agent MCP surface.
        admin_only = self.start_server(
            agent_uids=frozenset({0}), admin_uids=frozenset({os.getuid()})
        )
        status, body = self.http(admin_only, "GET", "/tools")
        self.assertEqual(status, 403)
        self.assertIn("Peer not allowed", body["error"])
        status, body = self.raw_http(
            admin_only,
            b"POST /call HTTP/1.1\r\n"
            b"Host: local\r\n"
            b"Content-Length: 0\r\n\r\n",
        )
        self.assertEqual(status, 403)
        self.assertIn("Peer not allowed", body["error"])
        status, _ = self.http(
            admin_only, "POST", "/operator/tools/fake_notes/oauth_connect/disconnect", {}
        )
        self.assertEqual(status, 400)

    def test_concurrency_cap_returns_429(self) -> None:
        socket_path = self.start_server()
        for _ in range(tools_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(tools_api._CALL_SLOTS.acquire(blocking=False))
        try:
            # Send headers and body together. The server deliberately returns
            # 429 without reading the body; a split-write HTTP client can race
            # that early close and raise BrokenPipe before reading the response.
            status, body = self.raw_json_http(
                socket_path, "POST", "/call", {"name": "fake_notes_read_note", "input": {}}
            )
        finally:
            for _ in range(tools_api.MAX_CONCURRENT_CALLS):
                tools_api._CALL_SLOTS.release()
        self.assertEqual(status, 429)

    def test_concurrency_cap_applies_before_body_read(self) -> None:
        socket_path = self.start_server()
        for _ in range(tools_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(tools_api._CALL_SLOTS.acquire(blocking=False))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(socket_path)
                connection.sendall(
                    b"POST /call HTTP/1.1\r\n"
                    b"Host: local\r\n"
                    b"Content-Length: 1024\r\n\r\n"
                )
                response = connection.recv(65536)
        finally:
            for _ in range(tools_api.MAX_CONCURRENT_CALLS):
                tools_api._CALL_SLOTS.release()
        self.assertIn(b" 429 ", response)

    def test_operator_routes_bypass_the_agent_call_cap(self) -> None:
        # With every agent-call slot held, an agent /call is capped but an operator
        # route still runs, so a busy agent cannot 429 the operator's approve/deny/
        # connect/disconnect.
        socket_path = self.start_server()
        for _ in range(tools_api.MAX_CONCURRENT_CALLS):
            self.assertTrue(tools_api._CALL_SLOTS.acquire(blocking=False))
        try:
            call_status, _ = self.raw_json_http(
                socket_path, "POST", "/call", {"name": "x", "input": {}}
            )
            op_status, _ = self.http(
                socket_path, "POST", "/operator/tools/nope/oauth_connect/disconnect", {}
            )
        finally:
            for _ in range(tools_api.MAX_CONCURRENT_CALLS):
                tools_api._CALL_SLOTS.release()
        self.assertEqual(call_status, 429)
        # The operator route reached the handler (unknown tool -> 404), not the cap.
        self.assertEqual(op_status, 404)

    def test_streaming_result_writes_host_named_agent_file_without_spool(self) -> None:
        socket_path = self.start_server()
        payload = b"v" * 512

        @contextmanager
        def open_stream():
            yield OpenedStreamingAsset("runway-task.mp4", "video/mp4", len(payload), io.BytesIO(payload))

        with state.mutation() as cur:
            state.set_tool_enabled(cur, "runway", True)
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    tools_host.BUNDLED_TOOLS["runway"],
                    "execute",
                    return_value=StreamingAsset(open_stream),
                ),
                patch.dict(os.environ, {"HOME": directory}),
            ):
                response = tools_mcp_shim._tools_action_request(
                    {"name": "runway_save_video", "input": {"task_id": "task-1"}},
                    socket_path,
                )
            result = response["result"]
            self.assertRegex(result["path"], r"^/tool_assets/asset-[0-9a-f]{32}\.mp4$")
            self.assertEqual((Path(directory) / result["path"].lstrip("/")).read_bytes(), payload)
            self.assertEqual(result, {
                "path": result["path"],
                "media_type": "video/mp4",
                "size_bytes": len(payload),
            })
        spool = Path(socket_path).parent / "assets"
        self.assertFalse(spool.exists() and any(spool.iterdir()))

    def test_streaming_result_rejects_invalid_filename_metadata(self) -> None:
        for filename in ("", ".", "../escape.mp4", "nested/clip.mp4", "bad\nname.mp4", "x" * 256):
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "invalid filename"):
                tools_api.ToolsRequestHandler._validated_stream_metadata(
                    OpenedStreamingAsset(filename, "video/mp4", 1, io.BytesIO(b"v"))
                )

    def test_streaming_result_removes_partial_file_when_source_ends_early(self) -> None:
        socket_path = self.start_server()

        @contextmanager
        def open_stream():
            yield OpenedStreamingAsset("runway-task.mp4", "video/mp4", 512, io.BytesIO(b"short"))

        with state.mutation() as cur:
            state.set_tool_enabled(cur, "runway", True)
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    tools_host.BUNDLED_TOOLS["runway"],
                    "execute",
                    return_value=StreamingAsset(open_stream),
                ),
                patch.dict(os.environ, {"HOME": directory}),
                self.assertRaises((RuntimeError, http.client.IncompleteRead)),
            ):
                tools_mcp_shim._tools_action_request(
                    {"name": "runway_save_video", "input": {"task_id": "task-1"}},
                    socket_path,
                )
            self.assertEqual(list((Path(directory) / "tool_assets").iterdir()), [])

    def test_stalled_request_is_closed_by_the_read_timeout(self) -> None:
        # A peer that connects and never finishes its request must not pin a
        # handler thread: the read timeout closes the connection instead.
        socket_path = self.start_server()
        with patch.object(tools_api.ToolsRequestHandler, "timeout", 0.3):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(socket_path)
                # Send a request line but never the blank line that ends headers.
                connection.sendall(b"GET /tools HTTP/1.1\r\n")
                # The server times out reading the rest and closes the socket,
                # so recv returns EOF well within our client-side timeout.
                self.assertEqual(connection.recv(65536), b"")

    def test_rejects_malformed_or_negative_content_length(self) -> None:
        socket_path = self.start_server()
        for length in (b"not-a-number", b"-1"):
            status, body = self.raw_http(
                socket_path,
                b"POST /call HTTP/1.1\r\n"
                b"Host: local\r\n"
                b"Content-Length: " + length + b"\r\n\r\n{}",
            )
            self.assertEqual(status, 400)
            self.assertIn("Content-Length", body["error"])


class McpShimTests(ToolsApiTestCase):
    def start_shim(self, socket_path: str) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env["KERN_TOOLS_SOCKET"] = socket_path
        # Keep this unit test independent of sockets on the developer host.
        # Tests that do not start an agent-network service must observe the
        # same unavailable-socket behavior locally and in CI.
        env["KERN_AGENT_NETWORK_SOCKET"] = str(
            Path(socket_path).parent / "missing-agent-network.sock"
        )
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["HOME"] = str(Path(socket_path).parent)
        shim = subprocess.Popen(
            [sys.executable, "-m", "host.runtime.agent_shim.mcp_shim"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_shim, shim)
        return shim

    def stop_shim(self, shim: subprocess.Popen[str]) -> None:
        """Shut the shim down deterministically. Closing stdin is the exit
        signal; the wait is bounded so a wedged shim fails this test instead
        of hanging the whole suite; stdout is closed last so the pipe fd is
        never leaked into later tests (unclosed pipes show up as
        ResourceWarning at interpreter exit)."""
        shim.stdin.close()
        try:
            shim.wait(timeout=30)
        except subprocess.TimeoutExpired:
            shim.kill()
            shim.wait(timeout=30)
        finally:
            shim.stdout.close()

    def rpc(self, shim: subprocess.Popen[str], message: dict) -> dict:
        shim.stdin.write(json.dumps(message) + "\n")
        shim.stdin.flush()
        line = shim.stdout.readline()
        self.assertTrue(line, "shim closed stdout unexpectedly")
        return json.loads(line)

    def test_shim_speaks_mcp_over_the_socket(self) -> None:
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        socket_path = str(Path(socket_dir.name) / "tools.sock")
        server = tools_api.ToolsServer(socket_path, frozenset({os.getuid()}))
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Enable Runway and Instagram so their actions are executable. Neither
        # they nor the staging tools appear in the listing — that is constant —
        # but the staged-asset schemas below are reached through describe_tool.
        with state.mutation() as cur:
            state.set_tool_enabled(cur, "runway", True)
            state.set_tool_enabled(cur, "instagram", True)
        shim = self.start_shim(socket_path)

        initialized = self.rpc(shim, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "kern-tools")

        # Notifications get no response; the next reply must match the next id.
        shim.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        listed = self.rpc(shim, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(names, EXPECTED_SHIM_TOOLS)
        self.assertTrue(all("inputSchema" in tool for tool in listed["result"]["tools"]))

        def describe(shim_tool_id: str) -> dict[str, Any]:
            described = self.rpc(shim, {
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "describe_tool", "arguments": {"tool_id": shim_tool_id}},
            })
            self.assertFalse(described["result"]["isError"])
            # The shim unwraps the service envelope to the action result itself.
            body = json.loads(described["result"]["content"][0]["text"])
            return {entry["id"]: entry["input_schema"] for entry in body["actions"]}

        runway = describe("runway")
        self.assertEqual(runway["save_video"]["required"], ["task_id"])
        self.assertNotIn("path", runway["save_video"]["properties"])
        self.assertIn("video_asset_id", runway["edit_video"]["properties"])
        self.assertNotIn("video_path", runway["edit_video"]["properties"])
        self.assertIn("image_asset_id", runway["generate_video"]["properties"])
        self.assertNotIn("image_path", runway["generate_video"]["properties"])
        instagram = describe("instagram")
        self.assertEqual(instagram["post_reel"]["required"], ["video_asset_id"])
        self.assertIn("video_asset_id", instagram["post_reel"]["properties"])
        self.assertNotIn("path", instagram["post_reel"]["properties"])

        video = Path(socket_dir.name) / "clip.mp4"
        video.write_bytes(b"x" * 512)
        staged = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "stage_video",
                    "arguments": {"path": "/clip.mp4", "for_tool": "runway"},
                },
            },
        )
        self.assertFalse(staged["result"]["isError"])
        staged_result = json.loads(staged["result"]["content"][0]["text"])
        metadata = server.asset_store.describe("runway", staged_result["video_asset_id"])
        self.assertEqual(metadata.filename, "clip.mp4")
        self.assertEqual(metadata.size_bytes, 512)
        self.assertNotIn(str(video), staged["result"]["content"][0]["text"])

        image = Path(socket_dir.name) / "frame.png"
        image.write_bytes(b"i" * 512)
        staged_image = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "stage_image",
                    "arguments": {"path": "/frame.png", "for_tool": "runway"},
                },
            },
        )
        self.assertFalse(staged_image["result"]["isError"])
        image_result = json.loads(staged_image["result"]["content"][0]["text"])
        image_metadata = server.asset_store.describe("runway", image_result["image_asset_id"])
        self.assertEqual(image_metadata.filename, "frame.png")
        self.assertEqual(image_metadata.media_type, "image/png")

        symlink = Path(socket_dir.name) / "linked.mp4"
        symlink.symlink_to(video)
        rejected = self.rpc(
            shim,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "stage_video",
                    "arguments": {"path": "/linked.mp4", "for_tool": "runway"},
                },
            },
        )
        self.assertTrue(rejected["result"]["isError"])

        catalog = self.rpc(shim, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_bundled_tools", "arguments": {}}})
        self.assertFalse(catalog["result"]["isError"])
        catalog_text = catalog["result"]["content"][0]["text"]
        self.assertIn('"fake_notes"', catalog_text)
        self.assertIn('"gmail"', catalog_text)  # disabled bundled tools appear too

        called = self.rpc(shim, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "call_tool", "arguments": {"tool_id": "fake_notes", "action_id": "read_note"}}})
        self.assertFalse(called["result"]["isError"])
        self.assertIn('"token-1"', called["result"]["content"][0]["text"])
        # Results reach the model compactly; indentation is ~10% of their bytes.
        self.assertNotIn("\n", called["result"]["content"][0]["text"])

        pending = self.rpc(shim, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "call_tool", "arguments": {"tool_id": "fake_notes", "action_id": "write_note", "input": {"text": "hi"}}}})
        self.assertFalse(pending["result"]["isError"])
        pending_text = pending["result"]["content"][0]["text"]
        self.assertIn("approval_id", pending_text)
        self.assertNotIn("approval_check_token", pending_text)
        self.assertIn("check_tool_approval", pending_text)

        failed = self.rpc(shim, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
        self.assertTrue(failed["result"]["isError"])

        unknown = self.rpc(shim, {"jsonrpc": "2.0", "id": 6, "method": "bogus/method"})
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_shim_lists_the_same_tools_when_the_tools_socket_is_missing(self) -> None:
        # An unreachable service must not withdraw declarations: the model reads
        # a shrinking tool list as "that capability does not exist", and the
        # list growing back re-encodes the whole cached prompt prefix.
        shim = self.start_shim("/nonexistent/tools.sock")
        listed = self.rpc(shim, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], EXPECTED_SHIM_TOOLS)
        # The failure surfaces on the call instead, where it can be reported.
        called = self.rpc(shim, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "call_tool", "arguments": {"tool_id": "gmail", "action_id": "search_messages"}}})
        self.assertTrue(called["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
