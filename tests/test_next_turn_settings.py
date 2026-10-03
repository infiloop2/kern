"""Saved configuration must never reconfigure an admitted turn."""
import unittest
from unittest.mock import patch

from host.runtime.admin_api import threads
from host.runtime.agent_runtime import orchestrator
from host.runtime.workspace.web_apps import backend as apps

OLD = {"agent_runtime": "claude_code", "model": "claude-opus-5-5", "effort": "high"}
NEW = {"agent_runtime": "codex", "model": "gpt-6.1-sol", "effort": "high"}


class NextTurnSettingsTests(unittest.TestCase):
    def test_app_saves_without_contacting_or_reconfiguring_the_live_turn(self):
        with patch.object(apps.db, "transaction") as transaction, patch.object(apps, "call_admin_api") as host, patch.object(apps, "_web_app_summary", return_value={"agent_settings": NEW}):
            transaction.return_value.__enter__.return_value.fetchone.side_effect = [(1,), ("saved",)]
            self.assertEqual(apps.set_app_agent_settings("app-1", NEW)["agent_settings"], NEW)
            host.assert_not_called()

    def test_followups_keep_the_live_runtime(self):
        for thread_id in ("app-1", "schedule-1", "thread-1"):
            with (
                self.subTest(thread_id=thread_id),
                patch.object(threads.state, "schedule_thread_is_owned", return_value=True),
                patch.object(threads.state, "thread_session_config", return_value={**OLD, "status": "running"}),
                patch.object(orchestrator, "steer_live_turn", return_value=True) as steer,
                patch.object(threads.state, "mutation") as mutation,
                patch.object(threads, "_public_thread", side_effect=lambda tid, runtime, model, effort: {"agent_runtime": runtime, "model": model, "effort": effort}),
            ):
                result = threads.send_thread_message(thread_id, {"message": "Keep going", **NEW}, None, True)
                self.assertEqual(result["thread"], OLD)
                self.assertEqual(steer.call_args.args[:3], (thread_id, OLD["agent_runtime"], "Keep going"))
                mutation.assert_not_called()
