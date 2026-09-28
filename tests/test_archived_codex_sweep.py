"""Maintenance cadence and provider-failure coverage without PostgreSQL."""

import unittest
from unittest.mock import patch

from host.runtime.admin_api import service, threads


class ArchivedCodexSweepTests(unittest.TestCase):
    def test_maintenance_sweeps_at_startup_then_daily(self) -> None:
        with (
            patch.object(service, "prune_state") as prune,
            patch.object(service, "sweep_archived_codex_sessions") as sweep,
            patch.object(service.time, "monotonic", side_effect=[0, 3600, 86399, 86400]),
            patch.object(service.time, "sleep", side_effect=[None, None, None, KeyboardInterrupt]),
        ):
            with self.assertRaises(KeyboardInterrupt):
                service.maintenance_loop()
        self.assertEqual(prune.call_count, 4)
        self.assertEqual(sweep.call_count, 2)

    def test_no_candidates_starts_no_provider(self) -> None:
        with (
            patch.object(threads.state, "archived_thread_session_ids", return_value=[]),
            patch.object(threads.codex_app_server, "CodexAppServer") as server,
        ):
            self.assertEqual(threads.sweep_archived_codex_sessions(), 0)
        server.assert_not_called()

    def test_start_failure_preserves_all_mappings(self) -> None:
        with (
            patch.object(threads.state, "archived_thread_session_ids", side_effect=[["thread-1"], [], []]),
            patch.object(threads.codex_app_server, "CodexAppServer") as server,
            patch.object(threads.state, "detach_archived_thread_session") as detach,
            patch.object(threads.host_errors, "report_unexpected") as report,
        ):
            server.return_value.start.side_effect = RuntimeError("unavailable")
            self.assertEqual(threads.sweep_archived_codex_sessions(), 0)
        detach.assert_not_called()
        server.return_value.close.assert_called_once()
        report.assert_called_once()

    def test_delete_failure_stops_that_runtime_without_failing_other_accounts(self) -> None:
        with (
            patch.object(threads.state, "archived_thread_session_ids", side_effect=[
                ["thread-1", "thread-2"], ["thread-3"], [],
            ]),
            patch.object(threads.codex_app_server, "CodexAppServer") as server,
            patch.object(threads.orchestrator, "live_thread_ids", return_value=set()),
            patch.object(threads.state, "mutation"),
            patch.object(threads.state, "detach_archived_thread_session", side_effect=["old-1", "old-3"]) as detach,
            patch.object(threads.codex_app_server, "delete_session", side_effect=[RuntimeError("timeout"), None]) as delete,
            patch.object(threads.host_errors, "report_unexpected") as report,
        ):
            self.assertEqual(threads.sweep_archived_codex_sessions(), 1)
        self.assertEqual([call.args[1] for call in detach.call_args_list], ["thread-1", "thread-3"])
        self.assertEqual([call.args[1] for call in delete.call_args_list], ["old-1", "old-3"])
        self.assertEqual(server.return_value.close.call_count, 2)
        report.assert_called_once()
