from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from host.bootstrap import io_control, render, verify_deploy


class DeviceDiscoveryTests(unittest.TestCase):
    def test_partitions_resolve_to_whole_disks_and_duplicate_mounts_collapse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysdev = root / "dev"
            sysdev.mkdir()
            disk = root / "devices" / "nvme0n1"
            (disk / "slaves").mkdir(parents=True)
            (disk / "dev").write_text("259:2\n")
            part = disk / "nvme0n1p1"
            part.mkdir()
            (part / "partition").write_text("1\n")
            (part / "dev").write_text("259:3\n")
            (sysdev / "259:3").symlink_to(part)
            (sysdev / "259:2").symlink_to(disk)
            # stat only the mount paths; real sysfs fixture reads still execute.
            original_stat = Path.stat
            def stat(path, *args, **kwargs):
                if str(path) in {"/root-mount", "/same-disk"}:
                    return SimpleNamespace(st_dev=os.makedev(259, 3 if str(path) == "/root-mount" else 2))
                return original_stat(path, *args, **kwargs)
            with patch.object(Path, "stat", stat):
                self.assertEqual(io_control.backing_devices(
                    (Path("/root-mount"), Path("/same-disk")), sysdev), ["259:2"])
                (disk / "slaves" / "underlying").touch()
                with self.assertRaisesRegex(RuntimeError, "stacked block device"):
                    io_control.backing_devices((Path("/same-disk"),), sysdev)

    def test_missing_device_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                io_control.backing_devices((Path(directory),), Path(directory) / "no-sysfs")

    def test_enable_configures_each_disk_and_can_be_reapplied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("io.cost.qos", "io.cost.model"):
                (root / name).touch()
            with patch.object(io_control, "backing_devices", return_value=["259:0", "259:2"]):
                with patch.object(Path, "write_text") as write:
                    io_control.enable(root)
                    io_control.enable(root)
            self.assertEqual([call.args[0] for call in write.call_args_list], [
                "259:0 ctrl=auto\n", "259:0 enable=1 ctrl=auto\n",
                "259:2 ctrl=auto\n", "259:2 enable=1 ctrl=auto\n",
            ] * 2)

    def test_missing_controller_fails_without_creating_fake_control_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "CONFIG_BLK_CGROUP_IOCOST"):
                io_control.enable(Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


class ResourceProtectionVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Independent fixture: 2 GiB host, protected parent AND service leaves.
        self.memory = 2 * 1024**3
        values = {
            "system.slice": (20, 100),
            "system.slice/kern-admin-api.service": (8, 200),
            "system.slice/kern-postgres.service": (8, 200),
            "system.slice/kern-cloudflared.service": (4, 200),
            "kern_workspace.slice": (4, 50),
            "kern_workspace.slice/kern-workspace.service": (4, 200),
            "kern_agent.slice": (0, 25),
        }
        for group, (percent, weight) in values.items():
            path = self.root / group
            path.mkdir(parents=True, exist_ok=True)
            (path / "memory.low").write_text(str((self.memory * percent // 100) // 4096 * 4096))
            (path / "io.weight").write_text(f"default {weight}\n")
        (self.root / "io.cost.qos").write_text("259:2 enable=1 ctrl=auto rpct=0.00\n259:0 enable=1 ctrl=auto\n")
        (self.root / "io.cost.model").write_text("259:2 ctrl=auto model=linear\n259:0 ctrl=auto model=linear\n")
        self.enterContext(patch.object(io_control, "backing_devices", return_value=["259:0", "259:2"]))

    def check(self, cloudflare=True):
        return verify_deploy.check_resource_protection(cloudflare, self.root, self.memory)

    def test_effective_protection_passes(self):
        self.assertEqual(self.check(), [])

    def test_unprotected_parent_fails_even_when_services_have_memory_low(self):
        (self.root / "system.slice/memory.low").write_text("0\n")
        self.assertTrue(any("system.slice memory.low=0" in error for error in self.check()))

    def test_weights_without_an_enabled_controller_fail(self):
        (self.root / "io.cost.qos").write_text("259:2 enable=0 ctrl=auto\n")
        self.assertEqual(len(self.check()), 2)

    def test_agent_default_weight_and_device_override_are_detected(self):
        (self.root / "kern_agent.slice/io.weight").write_text("default 100\n259:2 10000\n")
        self.assertTrue(any("kern_agent.slice io.weight" in error for error in self.check()))

    def test_no_cloudflare_configuration_does_not_require_tunnel_cgroup(self):
        for file in (self.root / "system.slice/kern-cloudflared.service").iterdir():
            file.unlink()
        self.assertEqual(self.check(False), [])
        self.assertEqual(len(self.check(True)), 2)

    def test_discovery_failure_is_reported(self):
        with patch.object(io_control, "backing_devices", side_effect=OSError("missing disk")):
            self.assertTrue(any("missing disk" in error for error in self.check()))


class BootstrapProtectionTests(unittest.TestCase):
    def test_existing_cgroups_are_updated_without_restarting_control_services(self):
        bootstrap = render._render_bootstrap()
        apply = bootstrap.split("apply_live_resource_protection() {", 1)[1].split("\n}", 1)[0]
        for unit, percent, weight in (
            ("system.slice", 20, 100),
            ("kern_workspace.slice", 4, 50),
            ("kern-workspace.service", 4, 200),
            ("kern-admin-api.service", 8, 200),
            ("kern-postgres.service", 8, 200),
            ("kern-cloudflared.service", 4, 200),
        ):
            self.assertIn(f"systemctl set-property --runtime {unit} MemoryLow={percent}% IOWeight={weight}", apply)
        self.assertIn("systemctl set-property --runtime kern_agent.slice IOWeight=25", apply)
        self.assertNotIn("systemctl restart", apply)
        self.assertIn("  start_services\n  apply_live_resource_protection\n  verify_deployment", bootstrap)

    def test_boot_and_reconfigure_enable_controller_before_postgres(self):
        bootstrap = render._render_bootstrap()
        self.assertLess(bootstrap.index("  configure_resource_protection\n"), bootstrap.index("  setup_postgres\n"))
        self.assertIn("systemctl enable kern-io-control.service", bootstrap)
        self.assertIn("systemctl restart kern-io-control.service", bootstrap)
        self.assertIn("RequiresMountsFor=/mnt/kern-admin /mnt/kern-agent", bootstrap)
        self.assertIn("Before=kern-postgres.service kern-admin-api.service kern-workspace.service kern-cloudflared.service", bootstrap)
        self.assertNotIn("Requires=kern-io-control.service", bootstrap)
        self.assertIn("MemoryLow=20%", bootstrap)
        self.assertIn("MemoryLow=4%", bootstrap)
        self.assertIn("kern-admin-api|kern-postgres) low=8%", bootstrap)
        self.assertIn("kern-cloudflared) low=4%", bootstrap)
        self.assertIn("systemctl start kern_agent.slice kern_workspace.slice", bootstrap)
        self.assertIn("kern-io-control.service", verify_deploy.CORE_UNITS)
