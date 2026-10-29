"""Discovery contracts and per-tool payload sizes, without a database.

Run with tests/scripts/test test_tool_discovery to print default describe sizes.
Sizes use UTF-8 json.dumps of the full executed response, disabled/no accounts.
"""

import json
import unittest
from unittest.mock import patch

from host.runtime.core import state
from host.runtime.tools import api, tools_host


class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(state, "enabled_tool_ids", return_value=set()))
        self.enterContext(patch.object(state, "tool_connections", return_value=[]))

    def test_default_payloads_for_every_bundled_tool(self) -> None:
        for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
            with self.subTest(tool_id=tool_id):
                payload = api._describe_tool({"tool_id": tool_id})
                serialized = json.dumps(payload)
                self.assertNotIn('"input_protections":', serialized)
                self.assertNotIn('"output_schema":', serialized)
                self.assertNotIn("agent_notes", payload["result"])
                print(f"{tool_id}: {len(serialized.encode('utf-8'))} bytes")
                explicit = api._describe_tool({
                    "tool_id": tool_id, "include_output_schema": False, "include_notes": False,
                })
                self.assertEqual(explicit, payload)
                expanded = api._describe_tool({
                    "tool_id": tool_id, "include_output_schema": True, "include_notes": True,
                })["result"]
                self.assertEqual(expanded["agent_notes"], tool.manifest.agent_notes)
                catalog = api._list_bundled_tools({"tool_ids": [tool_id]})["result"]["tools"][0]
                self.assertNotIn("agent_notes", catalog)
                explicit_catalog = api._list_bundled_tools({"tool_ids": [tool_id], "include_notes": False})["result"]["tools"][0]
                self.assertEqual(explicit_catalog, catalog)
                with_notes = api._list_bundled_tools({"tool_ids": [tool_id], "include_notes": True})["result"]["tools"][0]
                self.assertEqual(with_notes, {**catalog, "agent_notes": expanded["agent_notes"]})
                for action, detailed, spec in zip(payload["result"]["actions"], expanded["actions"], tool.manifest.actions):
                    self.assertEqual(action["input_schema"], spec.input_schema)
                    self.assertNotIn("input_protections", detailed)
                    self.assertEqual(detailed.get("output_schema", {}), spec.output_schema)
                    self.assertEqual(action["returns"], "approval" if spec.approval == "operator"
                                     else "asset" if spec.returns_asset else "json")
                    filtered = api._describe_tool({
                        "tool_id": tool_id, "action_ids": [spec.id],
                        "include_output_schema": True, "include_notes": True,
                    })["result"]
                    self.assertEqual(filtered, {**expanded, "actions": [detailed]})

    def test_description_flags_require_booleans(self) -> None:
        for flag in ("include_output_schema", "include_notes"):
            for invalid in (None, 0, 1, "false", [], {}):
                with self.subTest(flag=flag, invalid=invalid), self.assertRaisesRegex(
                    tools_host.ToolCallError, "must be a boolean"
                ):
                    api._describe_tool({"tool_id": "gmail", flag: invalid})

    def test_catalog_notes_flag_and_broad_catalog(self) -> None:
        for invalid in (None, 0, 1, "false", [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(tools_host.ToolCallError, "must be a boolean"):
                api._list_bundled_tools({"tool_ids": ["gmail"], "include_notes": invalid})
        for entry in api._list_bundled_tools({"include_notes": True})["result"]["tools"]:
            self.assertNotIn("agent_notes", entry)
            self.assertNotIn("actions", entry)
