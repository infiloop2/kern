"""CI database coverage for IndexNow's real host guard and approval path."""

import json
from unittest.mock import patch

from host.runtime.core import state
from host.runtime.tools import tools_host
from host.tools import indexnow
from test_tools_host import ToolsHostTestCase


class IndexNowHostTests(ToolsHostTestCase):
    def setUp(self):
        super().setUp()
        with state.mutation() as cur:
            state.set_tool_enabled(cur, "indexnow", True)
            state.save_tool_config_value(cur, "indexnow", "INDEXNOW_KEY", "test-key")
        self.request = self.enterContext(patch.object(indexnow, "request_bytes"))

    def call(self, tool_input):
        return tools_host.execute_action("indexnow", "submit_urls", tool_input, origin_thread_id=None)

    def test_guard_runs_for_operator_action_before_approval_queue(self):
        for url in ("https://example.com/alice@example.com", "https://example.com/page?email=alice%2540example.com"):
            with self.subTest(url=url), patch.object(state, "insert_tool_approval", wraps=state.insert_tool_approval) as queue:
                result = self.call(json.dumps({"urls": ["https://example.com/safe", url]}))
                self.assertEqual(result["status"], "failed")
                self.assertIn("email", result["error"])
                queue.assert_not_called()
        self.request.assert_not_called()

    def test_exact_batch_and_private_binding_survive_host_api_recreation(self):
        urls = ["https://example.com/%6eew?version=2", "https://example.com/deleted?source=public"]
        pending = self.call({"urls": urls})
        self.assertEqual(pending["status"], "pending_approval")
        approval_id = pending["approval_id"]
        record = state.tool_approval(approval_id)
        self.assertEqual(record["payload"]["urls"], urls)
        self.assertNotIn("test-key", json.dumps(record["payload"]))
        self.request.assert_not_called()
        result = tools_host.decide_approval(approval_id, "approve", public_hostname=None)
        self.assertEqual(result["approval"]["status"], "executed")
        self.assertEqual(json.loads(self.request.call_args.kwargs["data"])["urlList"], urls)
        self.request.assert_called_once()
        with self.assertRaisesRegex(tools_host.ToolCallError, "not pending"):
            tools_host.decide_approval(approval_id, "approve", public_hostname=None)

    def test_denial_and_configuration_change_do_not_submit(self):
        denied = self.call({"urls": ["https://example.com/new"]})
        self.assertEqual(tools_host.decide_approval(denied["approval_id"], "deny", public_hostname=None)["approval"]["status"], "denied")
        pending = self.call({"urls": ["https://example.com/new"]})
        with state.mutation() as cur:
            state.save_tool_config_value(cur, "indexnow", "INDEXNOW_KEY", "changed-key")
        result = tools_host.decide_approval(pending["approval_id"], "approve", public_hostname=None)
        self.assertEqual(result["approval"]["status"], "failed")
        self.assertIn("key changed", result["result"]["error"])
        self.request.assert_not_called()
