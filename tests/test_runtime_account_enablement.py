"""Additional accounts are idle until the operator explicitly enables them."""
from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host import session_options
from host.runtime.admin_api import runtime_accounts
from host.runtime.admin_api.errors import ApiError
from host.runtime.agent_runtime import orchestrator
from host.runtime.core import db, state


class RuntimeAccountGateTests(unittest.TestCase):
    def test_registry_identifies_only_additional_accounts(self):
        self.assertEqual(session_options.OPTIONAL_ACCOUNT_RUNTIMES, ("codex-2", "codex-3", "grok-2"))

    def test_disabled_accounts_never_reach_provider_probe_even_when_forced(self):
        for runtime in session_options.OPTIONAL_ACCOUNT_RUNTIMES:
            with (self.subTest(runtime=runtime),
                patch.object(state, "runtime_account_enabled", return_value=False),
                patch.object(orchestrator, "_mark_runtime_deactivated", return_value="deactivated") as deactivate,
                patch.object(orchestrator, "harness_adapter") as provider,
            ):
                self.assertFalse(orchestrator.runtime_network_enabled(runtime))
                self.assertEqual(orchestrator.refresh_runtime_status(runtime, force_provider_probe=True), "deactivated")
                deactivate.assert_called_once_with(runtime)
                provider.assert_not_called()

    def test_enabled_account_still_requires_provider_switch(self):
        for enabled in (True, False):
            with (self.subTest(enabled=enabled),
                patch.object(state, "runtime_account_enabled", return_value=True),
                patch.object(orchestrator.network_policy, "load_policy", return_value={
                    "network_integrations": {"openai": {"enabled": enabled}},
                }),
            ):
                self.assertEqual(orchestrator.runtime_network_enabled("codex-2"), enabled)

    def test_primary_accounts_need_no_account_row(self):
        for runtime in ("codex", "grok", "claude_code", "hermes", "script"):
            with patch("host.runtime.core.state.accounts._read") as read:
                self.assertTrue(state.runtime_account_enabled(runtime))
                read.assert_not_called()

    def test_manual_refresh_skips_disabled_slots(self):
        with (
            patch.object(state, "runtime_account_enabled", side_effect=lambda runtime: runtime not in session_options.OPTIONAL_ACCOUNT_RUNTIMES),
            patch.object(orchestrator, "refresh_runtime_status") as refresh,
            patch.object(orchestrator, "runtime_network_enabled", return_value=True),
            patch.object(runtime_accounts, "current_agent_accounts", return_value={"accounts": []}),
        ):
            runtime_accounts.refresh_agent_runtime_accounts(None)
            self.assertEqual([call.args[0] for call in refresh.call_args_list], ["codex", "claude_code", "grok", "hermes"])
            refresh.reset_mock()
            runtime_accounts.refresh_agent_runtime_accounts({"agent_runtime": "grok-2"})
            refresh.assert_not_called()

    def test_background_loop_does_no_refresh_for_disabled_slot(self):
        with (
            patch.object(state, "runtime_account_enabled", return_value=False),
            patch.object(orchestrator, "runtime_status", return_value="deactivated"),
            patch.object(orchestrator, "refresh_runtime_status") as refresh,
            patch.object(orchestrator.time, "sleep", side_effect=InterruptedError),
        ):
            with self.assertRaises(InterruptedError):
                orchestrator.runtime_status_loop("codex-2")
            refresh.assert_not_called()

    def test_account_toggle_validates_runtime_and_strict_boolean(self):
        for body in (None, {}, {"agent_runtime": "codex", "enabled": True},
                     {"agent_runtime": [], "enabled": True},
                     {"agent_runtime": "grok-2", "enabled": 1},
                     {"agent_runtime": "codex-2", "enabled": "false"},
                     {"agent_runtime": "codex-2", "enabled": True, "extra": True}):
            with self.subTest(body=body), patch.object(orchestrator, "set_runtime_account_enabled") as write:
                with self.assertRaises(ApiError):
                    runtime_accounts.set_agent_runtime_account_enabled(body)
                write.assert_not_called()

    def test_probe_finishing_after_disable_cannot_publish_active_account(self):
        @contextmanager
        def mutation(*, after_commit):
            yield MagicMock()
            for callback in after_commit:
                callback()

        provider = MagicMock(collect_login_before_probe=False)
        provider.account_status.return_value = ("active", None, {"account_id": "old"})
        with (
            patch.object(orchestrator, "runtime_network_enabled", side_effect=[True, False]),
            patch.object(orchestrator, "harness_adapter", return_value=provider),
            patch.object(state, "mutation", mutation),
            patch.object(state, "runtime_account_enabled", return_value=False),
            patch.object(state, "set_oauth_login"),
            patch.object(state, "append_agent_event"),
            patch.object(orchestrator, "_sync_runtime_proxy_pin_in") as pin,
            patch.object(orchestrator, "_close_login_flow"),
            patch.object(orchestrator, "_stop_runtime_processes"),
            patch.object(orchestrator, "_trusted_active_account") as trust,
            patch.object(orchestrator, "_capture_completed_token_login"),
            patch.dict(orchestrator._RUNTIME_STATUSES, {}, clear=True),
        ):
            self.assertEqual(orchestrator.refresh_runtime_status("codex-2"), "deactivated")
            trust.assert_not_called()
            self.assertIsNone(pin.call_args.args[2])
            self.assertEqual(orchestrator.runtime_status("codex-2"), "deactivated")


class RuntimeAccountPersistenceTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()
        self.enterContext(patch.dict(orchestrator._RUNTIME_STATUSES, {}, clear=True))

    def test_extra_accounts_default_disabled_and_persist_opt_in(self):
        for runtime in session_options.OPTIONAL_ACCOUNT_RUNTIMES:
            self.assertFalse(state.runtime_account_enabled(runtime))
            with state.mutation() as cur:
                state.set_runtime_account_enabled(cur, runtime, True)
            self.assertTrue(state.runtime_account_enabled(runtime))
        state.save_network_policy({"network_integrations": {}}, "now")
        self.assertTrue(state.runtime_account_enabled("codex-2"))
        self.assertFalse(orchestrator.runtime_network_enabled("codex-2"))

    def test_account_flag_survives_refresh_and_linked_account_reset(self):
        with state.mutation() as cur:
            state.set_runtime_account_enabled(cur, "codex-2", True)
        account = {"account_id": "approved", "operator_approval": orchestrator.OPENAI_OPERATOR_APPROVAL}
        state.save_openai_account(account, runtime_type="codex-2")
        self.assertTrue(state.runtime_account_enabled("codex-2"))
        state.save_openai_account(account | {"email": "updated@example.com"}, runtime_type="codex-2")
        self.assertTrue(state.runtime_account_enabled("codex-2"))
        state.save_openai_account(None, runtime_type="codex-2")
        self.assertTrue(state.runtime_account_enabled("codex-2"))
        with state.mutation() as cur:
            state.set_runtime_account_enabled(cur, "codex-2", False)
        state.save_openai_account(account, runtime_type="codex-2")
        self.assertFalse(state.runtime_account_enabled("codex-2"))

    def test_disabling_grok_two_revokes_status_probe_authorization(self):
        state.save_network_policy({"network_integrations": {"xai": {"enabled": True}}}, "now")
        for runtime, account_id in (("grok", "primary"), ("grok-2", "extra")):
            state.save_xai_account({"account_id": account_id, "operator_approval": orchestrator.XAI_OPERATOR_APPROVAL}, runtime_type=runtime)
        self.assertEqual(state.read_proxy_xai_status_probe_account_ids(), {"primary"})
        with state.mutation() as cur:
            state.set_runtime_account_enabled(cur, "grok-2", True)
        self.assertEqual(state.read_proxy_xai_status_probe_account_ids(), {"primary", "extra"})
        with patch.object(orchestrator, "_stop_runtime_processes"), patch.object(orchestrator, "_close_login_flow"):
            orchestrator.set_runtime_account_enabled("grok-2", False)
        self.assertEqual(state.read_proxy_xai_status_probe_account_ids(), {"primary"})
        self.assertEqual(state.read_xai_account(runtime_type="grok-2")["account_id"], "extra")
        with db.transaction() as cur:
            cur.execute("SELECT event_type FROM agent_events ORDER BY seq")
            self.assertEqual(cur.fetchall(), [("agent_runtime.account_disabled",)])

    def test_disable_keeps_anchor_clears_pin_and_login_and_stops_work(self):
        runtime = "codex-2"
        account = {"account_id": "approved", "operator_approval": orchestrator.OPENAI_OPERATOR_APPROVAL,
                   "email": "saved@example.com", "codex_usage": {"used_percent": 50}}
        state.save_openai_account(account, runtime_type=runtime)
        state.save_proxy_openai_account_id("approved", runtime_type=runtime)
        with state.mutation() as cur:
            state.set_runtime_account_enabled(cur, runtime, True)
            state.set_oauth_login(cur, runtime, {"status": "awaiting_login", "login_url": "https://example.com/login",
                "device_code": "code", "login_id": "pending", "expires_at": "2099-01-01T00:00:00Z"})
        with patch.object(orchestrator, "_stop_runtime_processes") as stop, patch.object(orchestrator, "_close_login_flow") as close:
            runtime_accounts.set_agent_runtime_account_enabled({"agent_runtime": runtime, "enabled": False})
        self.assertFalse(state.runtime_account_enabled(runtime))
        self.assertEqual(state.read_openai_account(runtime_type=runtime)["account_id"], "approved")
        self.assertIsNone(state.read_proxy_openai_account_id(runtime_type=runtime))
        self.assertIsNone(state.oauth_login(runtime))
        close.assert_called_once_with(runtime)
        stop.assert_called_once()
        record = runtime_accounts._current_agent_account(orchestrator.all_runtime_status_records(), runtime)
        self.assertEqual(record, {"agent_runtime": runtime, "provider": "openai", "status": "deactivated", "enabled": False,
                                  "account_id": "approved", "email": "saved@example.com"})
        with self.assertRaises(ApiError):
            runtime_accounts.start_codex_2_oauth_login()
