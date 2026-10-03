"""Daily execution quotas: host paths plus real database concurrency and rollover."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

import pg_harness
from host.runtime.core import db, state
from host.runtime.core.state import tools as tool_state
from host.runtime.tools import api as tools_api, tools_host
from host.tools import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from test_tools_host import FAKE_MANIFEST, FakeTool, ToolsHostTestCase, _connection, _fake_account


def limited_manifest(limit=2):
    return replace(FAKE_MANIFEST, actions=tuple(
        replace(spec, limit_runs_per_day=limit) for spec in FAKE_MANIFEST.actions
    ))


class RunLimitUnitTests(unittest.TestCase):
    def setUp(self):
        self.tool = Mock(manifest=limited_manifest())
        self.tool.execute.return_value = ActionExecuted({"text": "hello"})
        self.tool.execute_approved.return_value = ApprovalExecuted("Done.")
        api = Mock()
        api.assets._approved_execution.return_value = nullcontext()
        for name, value in (("enabled_tool", self.tool), ("resolve_connection", _connection()),
                            ("connection_scope", _connection()), ("host_api_for", api)):
            self.enterContext(patch.object(tools_host, name, return_value=value))
        self.enterContext(patch.object(tools_host, "_audit"))
        self.consume = self.enterContext(patch.object(state, "consume_tool_action_run", return_value=True))
        self.reached = self.enterContext(patch.object(state, "tool_action_run_limit_reached", return_value=False))

    def call(self, action="read_note", payload=None, **kwargs):
        return tools_host.execute_action("fake_notes", action, payload or {}, "thread-352", **kwargs)

    def approved(self):
        return tools_host._execute_approved({
            "tool_id": "fake_notes", "action_id": "write_note", "approval_id": "approval_1.token",
            "connection_id": "connection_test", "account_id": None, "account_label": "",
            "payload": {"text": "hello"},
            "status": "approved", "summary": "Write.", "created_at": 1, "decided_at": 2,
        }, None)

    def test_direct_calls_share_counter_across_threads_and_accounts(self):
        self.consume.side_effect = [True, False]
        self.assertEqual(self.call()["status"], "executed")
        with self.assertRaisesRegex(tools_host.ToolCallError, "Daily run limit.*00:00 UTC"):
            tools_host.execute_action("fake_notes", "read_note", {}, "other-thread", connection_id="other-account")
        self.assertEqual(self.tool.execute.call_count, 1)
        self.assertEqual(self.consume.call_args_list[0], self.consume.call_args_list[1])

    def test_failed_executions_still_consume(self):
        self.tool.execute.return_value = ActionFailed("Provider rejected it.")
        self.assertEqual(self.call()["status"], "failed")
        self.consume.assert_called_once_with("fake_notes", "read_note", 2)
        self.tool.execute_approved.return_value = ActionFailed("Provider rejected it.")
        self.assertEqual(self.approved()["status"], "failed")
        self.assertEqual(self.consume.call_count, 2)

    def test_approval_request_checks_without_consuming(self):
        self.tool.execute.return_value = ActionPendingApproval("approval_1.token", "Write.")
        self.assertEqual(self.call("write_note", {"text": "hello"})["status"], "pending_approval")
        self.consume.assert_not_called()
        self.reached.assert_called_once_with("fake_notes", "write_note", 2)
        self.reached.return_value = True
        with self.assertRaisesRegex(tools_host.ToolCallError, "Daily run limit"):
            self.call("write_note", {"text": "hello"})
        self.assertEqual(self.tool.execute.call_count, 1)

    def test_approved_execution_rechecks_and_does_not_invoke_exhausted_action(self):
        self.consume.side_effect = [True, False]
        self.assertEqual(self.approved()["status"], "executed")
        result = self.approved()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Daily run limit", result["error"])
        self.assertEqual(self.tool.execute_approved.call_count, 1)
        self.reached.assert_not_called()

    def test_invalid_input_and_unlimited_actions_do_not_touch_counters(self):
        with self.assertRaises(tools_host.ToolCallError):
            self.call(payload={"unknown": "field"})
        self.tool.manifest = FAKE_MANIFEST
        self.call()
        self.consume.assert_not_called()
        self.reached.assert_not_called()

    def test_counter_storage_failure_prevents_execution(self):
        self.consume.side_effect = RuntimeError("storage unavailable")
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            self.call()
        self.assertEqual(self.approved()["status"], "failed")
        self.tool.execute.assert_not_called()
        self.tool.execute_approved.assert_not_called()

    def test_manifest_validation_and_initial_rollout(self):
        for invalid in (0, -1, True, False, 1.5, "50"):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "limit_runs_per_day"):
                limited_manifest(invalid)
        for valid in (None, 1, 50):
            limited_manifest(valid)
        declared = [(tool_id, spec.id, spec.limit_runs_per_day)
                    for tool_id, tool in tools_host.BUNDLED_TOOLS.items()
                    for spec in tool.manifest.actions if spec.limit_runs_per_day is not None]
        self.assertEqual(declared, [("zoho_mail", "send_email", 50)])

    def test_discovery_exposes_only_declared_limits(self):
        with patch.object(state, "enabled_tool_ids", return_value=set()), patch.object(state, "tool_connections", return_value=[]):
            catalog = tools_api.call_action("list_bundled_tools", {"tool_ids": ["zoho_mail"]}, None)["result"]
            described = tools_api.call_action("describe_tool", {"tool_id": "zoho_mail"}, None)["result"]
        for actions in (catalog["tools"][0]["actions"], described["actions"]):
            self.assertEqual({a["id"]: a["limit_runs_per_day"] for a in actions if "limit_runs_per_day" in a}, {"send_email": 50})


class RunLimitDatabaseTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def test_concurrent_callers_cannot_exceed_cap(self):
        # Independent tools processes do not share mutation()'s Python lock.
        # Use separate DB transactions here to exercise the SQL conflict gate.
        with patch.object(tool_state, "mutation", db.transaction), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: state.consume_tool_action_run("zoho_mail", "send_email", 50), range(80)))
        self.assertEqual(sum(results), 50)
        self.assertTrue(state.tool_action_run_limit_reached("zoho_mail", "send_email", 50))
        with db.transaction() as cur:
            cur.execute("SELECT runs FROM tool_action_runs")
            self.assertEqual(cur.fetchall(), [(50,)])

    def test_utc_rollover_resets_one_row_and_actions_are_independent(self):
        self.assertFalse(state.tool_action_run_limit_reached("zoho_mail", "send_email", 1))
        self.assertTrue(state.consume_tool_action_run("zoho_mail", "send_email", 1))
        self.assertFalse(state.consume_tool_action_run("zoho_mail", "send_email", 1))
        self.assertTrue(state.consume_tool_action_run("zoho_mail", "other_action", 1))
        self.assertTrue(state.consume_tool_action_run("other_tool", "send_email", 1))
        with state.mutation() as cur:
            cur.execute("UPDATE tool_action_runs SET day = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date - 1")
        self.assertFalse(state.tool_action_run_limit_reached("zoho_mail", "send_email", 1))
        self.assertTrue(state.consume_tool_action_run("zoho_mail", "send_email", 1))
        with db.transaction() as cur:
            cur.execute("SELECT runs, day = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date FROM tool_action_runs WHERE tool_id = 'zoho_mail' AND action_id = 'send_email'")
            self.assertEqual(cur.fetchone(), (1, True))
            cur.execute("SELECT COUNT(*) FROM tool_action_runs")
            self.assertEqual(cur.fetchone()[0], 3)

    def test_tools_role_can_enforce_counter_without_delete_permission(self):
        with db.transaction() as cur:
            for privilege, expected in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False)):
                cur.execute("SELECT has_table_privilege('kern-tools', 'tool_action_runs', %s)", (privilege,))
                self.assertEqual(cur.fetchone()[0], expected)


class RunLimitApprovalLifecycleTests(ToolsHostTestCase):
    def test_pending_approvals_do_not_reserve_and_over_limit_decision_is_terminal(self):
        self.prepare_fake_tool()
        tools_host.HostCredentials("fake_notes", _connection()).save({
            "account": _fake_account(), "secret": {"text": ""}, "metadata": {},
        })
        self.enterContext(patch.object(FakeTool, "manifest", limited_manifest(1)))
        approvals = [tools_host.execute_action("fake_notes", "write_note", {"text": "hello"}, None)["approval_id"] for _ in range(3)]
        denied = tools_host.decide_approval(approvals[0], "deny", None)
        self.assertEqual(denied["approval"]["status"], "denied")
        self.assertFalse(state.tool_action_run_limit_reached("fake_notes", "write_note", 1))
        executed = tools_host.decide_approval(approvals[1], "approve", None)
        self.assertEqual(executed["approval"]["status"], "executed", executed["result"])
        with patch.object(FakeTool, "execute_approved") as execute:
            blocked = tools_host.decide_approval(approvals[2], "approve", None)
        execute.assert_not_called()
        self.assertEqual(blocked["approval"]["status"], "failed")
        self.assertIn("Daily run limit", blocked["approval"]["result"])
        with self.assertRaisesRegex(tools_host.ToolCallError, "not pending"):
            tools_host.decide_approval(approvals[2], "approve", None)
