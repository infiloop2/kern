"""Persisted resource validation and effective CPU capacity."""

from dataclasses import asdict
import json
import os
import stat
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host.cli import lifecycle_lima as lima
from host.cli.lifecycle_types import LifecycleCommand
from host.config import ConfigError
from host.runtime.core.cpu_resources import available_cpu_count


class ResourceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("limactl"), "real Lima CLI required")
    def test_metadata_is_not_a_real_lima_instance(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"LIMA_HOME": home}):
            lima._save_resources(lima.LimaConfig("test", cpus=6))
            result = subprocess.run(["limactl", "list", "--format=json"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")

    def test_resource_intent_syncs_file_then_rename_and_parent_directories(self) -> None:
        events = []
        fsync, replace = os.fsync, os.replace

        def sync(fd):
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            fsync(fd)

        def rename(source, target):
            events.append("rename")
            replace(source, target)

        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"LIMA_HOME": home}):
            with patch.object(os, "fsync", side_effect=sync), patch.object(os, "replace", side_effect=rename):
                lima._save_resources(lima.LimaConfig("test", cpus=6))
            self.assertEqual(events, ["file", "rename", "directory", "directory"])

    def test_metadata_is_namespaced_private_and_validated(self) -> None:
        command = LifecycleCommand(mode="recover", agent_name="test", provider="lima")
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"LIMA_HOME": home}):
            config = lima.LimaConfig("test", cpus=6, memory_gib=28, root_disk_gib=32)
            lima._save_resources(config)
            self.assertEqual(lima._resolve_resources(command), config)
            path = lima._resource_path("test")
            self.assertEqual(path.parent.name, ".kern-config")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for key, value in (("cpus", True), ("memory_gib", 0), ("agent_name", "other"), ("root_disk_gib", "32")):
                with self.subTest(key=key):
                    saved = asdict(config)
                    saved[key] = value
                    path.write_text(json.dumps(saved))
                    with self.assertRaises(ConfigError):
                        lima._resolve_resources(command)
            path.write_text("not json")
            with self.assertRaises(ConfigError):
                lima._resolve_resources(command)

    def test_new_namespace_uses_defaults_and_override_preserves_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"LIMA_HOME": home}):
            command = LifecycleCommand(mode="recover", agent_name="test", provider="lima", lima_cpus=8)
            lima._save_resources(lima.LimaConfig("test", cpus=6, memory_gib=28))
            resolved = lima._resolve_resources(command)
            self.assertEqual((resolved.cpus, resolved.memory_gib), (8, 28))
            with patch.dict(os.environ, {"LIMA_HOME": str(Path(home) / "other")}):
                default = lima._resolve_resources(LifecycleCommand(mode="start", agent_name="test", provider="lima"))
                self.assertEqual((default.cpus, default.memory_gib), (2, 2))

    def test_cpu_count_respects_affinity_and_handles_unavailable_information(self) -> None:
        with patch("os.sched_getaffinity", return_value={2, 4, 6}), patch("os.cpu_count", return_value=12):
            self.assertEqual(available_cpu_count(), 3)
        with patch("os.sched_getaffinity", side_effect=OSError), patch("os.cpu_count", return_value=None):
            self.assertEqual(available_cpu_count(), 1)
