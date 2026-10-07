"""Single Codex reconciliation phase runs at startup and daily."""

import unittest
from unittest.mock import patch

from host.runtime.admin_api import service


class CodexMaintenanceTests(unittest.TestCase):
    def test_maintenance_reconciles_at_startup_then_daily(self) -> None:
        with (
            patch.object(service, "prune_state") as prune,
            patch.object(service, "reconcile_codex_sessions") as reconcile,
            patch.object(service.time, "monotonic", side_effect=[0, 3600, 86399, 86400]),
            patch.object(service.time, "sleep", side_effect=[None, None, None, KeyboardInterrupt]),
        ):
            with self.assertRaises(KeyboardInterrupt):
                service.maintenance_loop()
        self.assertEqual(prune.call_count, 4)
        self.assertEqual(reconcile.call_count, 2)

    def test_startup_cleanup_waits_for_recovery_admission(self) -> None:
        order = []
        with (
            patch.object(service, "restart_interrupted_agents", side_effect=lambda _: order.append("recover")) as recover,
            patch.object(service, "prune_state"),
            patch.object(service, "reconcile_codex_sessions", side_effect=lambda: order.append("cleanup")),
            patch.object(service.time, "monotonic", return_value=0),
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                service.maintenance_loop(["thread-1"])
        recover.assert_called_once_with(["thread-1"])
        self.assertEqual(order, ["recover", "cleanup"])
