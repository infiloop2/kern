import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host.runtime.core import host_errors, host_metrics


class ResourceMetricsTests(unittest.TestCase):
    def test_service_counters_include_zero_and_task_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / host_metrics.RESOURCE_CGROUPS["browser"]
            root.mkdir(parents=True)
            for name, contents in {
                "cpu.stat": "usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\n",
                "memory.current": "4096\n",
                "memory.swap.current": "0\n",
                "pids.current": "3\n",
            }.items():
                (root / name).write_text(contents)
            with patch.object(host_metrics, "CGROUP_ROOT", Path(directory)):
                self.assertEqual(host_metrics.service_resource_snapshot("browser"), {
                    "browser_cpu_usage_usec": 123456, "browser_memory_bytes": 4096,
                    "browser_swap_bytes": 0, "browser_tasks": 3,
                })

    def test_bad_or_missing_counters_do_not_hide_readable_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / host_metrics.RESOURCE_CGROUPS["browser"]
            root.mkdir(parents=True)
            (root / "cpu.stat").write_text("usage_usec invalid\n")
            (root / "memory.current").write_text("-1\n")
            (root / "pids.current").write_text("0")
            with patch.object(host_metrics, "CGROUP_ROOT", Path(directory)):
                self.assertEqual(host_metrics.service_resource_snapshot("browser"), {"browser_tasks": 0})

    def test_unavailable_service_is_unknown_not_zero(self):
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertEqual(host_metrics.service_resource_snapshot("browser"), {})

    def test_host_snapshot_captures_pressure_and_all_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pressure").mkdir()
            (root / "pressure/cpu").write_text("some avg10=12.50 avg60=2.00 total=1234\n")
            (root / "pressure/memory").write_text("full avg10=3.00 avg60=1.00 total=100\n")
            (root / "pressure/io").write_text("some avg10=nan\nfull avg10=101\n")
            (root / "meminfo").write_text(
                "MemTotal: 2000 kB\nMemAvailable: 800 kB\nSwapTotal: 6000 kB\nSwapFree: 5000 kB\n"
            )
            with (
                patch.object(host_metrics, "PROC_ROOT", root),
                patch.object(host_metrics.os, "getloadavg", return_value=(2.5, 1.0, 1.0)),
                patch.object(host_metrics, "service_resource_snapshot", side_effect=lambda name: {f"{name}_tasks": 1}) as services,
            ):
                snapshot = host_metrics.resource_snapshot()
        self.assertEqual(snapshot["host_load_1m"], 2.5)
        self.assertEqual(snapshot["host_memory_available_bytes"], 800 * 1024)
        self.assertEqual(snapshot["host_swap_used_bytes"], 1000 * 1024)
        self.assertEqual(snapshot["host_cpu_pressure_some_avg10"], 12.5)
        self.assertEqual(snapshot["host_memory_pressure_full_avg10"], 3.0)
        self.assertFalse(any(key.startswith("host_io") for key in snapshot))
        self.assertEqual(services.call_count, len(host_metrics.RESOURCE_CGROUPS))
        self.assertIn("agents_tasks", snapshot)
        self.assertIn("browser_tasks", snapshot)
        self.assertIn("admin_tasks", snapshot)

    def test_snapshot_is_best_effort_if_host_files_are_unreadable(self):
        with patch.object(Path, "read_text", side_effect=PermissionError), patch.object(
            host_metrics.os, "getloadavg", side_effect=OSError,
        ):
            self.assertEqual(host_metrics.resource_snapshot(), {})

    def test_all_service_fields_fit_in_diagnostic_context(self):
        # Long-lived cumulative counters and all before/after CPU values must
        # survive the existing 4 KiB diagnostic context cap.
        context = {"timeout_seconds": 60, "elapsed_seconds": 60.5}
        for name in host_metrics.RESOURCE_CGROUPS:
            for suffix in ("cpu_usage_usec", "cpu_usage_usec_before", "memory_bytes", "swap_bytes", "tasks"):
                context[f"{name}_{suffix}"] = 999999999999999
        for resource in ("cpu", "memory", "io"):
            for kind in ("some", "full"):
                context[f"host_{resource}_pressure_{kind}_avg10"] = 100.0
        for key in ("host_load_1m", "host_memory_total_bytes", "host_memory_available_bytes", "host_swap_used_bytes"):
            context[key] = 999999999999999
        self.assertLessEqual(len(json.dumps(context).encode()), host_errors.MAX_CONTEXT_BYTES)
        self.assertEqual(host_errors._safe_context(context), context)
