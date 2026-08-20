"""Unit tests for the Lima local host provider.

These tests run without limactl: every ``limactl`` invocation goes through the
module's single subprocess wrapper, which the tests replace with a stateful
fake that records exact argument arrays. The assertions follow the contracts
in ``docs/architecture/host-provider-design.md``: exact-name discovery that
fails closed, an isolated plain-mode VM definition with no secrets, launch
failures that delete only the instance they created, and no code path that
ever deletes a durable data disk.
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.bootstrap import render
from host.config import ConfigError
from host.cli import lifecycle as deploy
from host.cli import lifecycle_lima as lima
from host.cli import power
from host.cli.lifecycle_types import LifecycleCommand
from host.version import repo_version
from tests.smoke.smoke_lima import LimaSmoke, SMOKE_WORKDIR_PREFIX

SAMPLE_SSH_PUBLIC_KEY = "ssh-ed25519 AAAATEST operator@example"
SAMPLE_ADMIN_PASSWORD_SHA256 = "f" * 64


def _fake_deploy_key(workdir: object) -> Path:
    key_path = Path(str(workdir)) / "deploy_key"
    key_path.write_text("private")
    key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAADEPLOY kern-deploy\n")
    return key_path


def _sample_config(agent_name: str = "kern-test") -> lima.LimaConfig:
    return lima.LimaConfig(agent_name=agent_name)


def _fixture_dir() -> str:
    """A test-process-lifetime directory, removed at interpreter exit."""
    path = tempfile.mkdtemp(prefix="kern-lima-test-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def _existing_instance_record(
    status: str = "Running", ssh_port: int = 60022, stored_version: str = "0.0.0"
) -> dict[str, object]:
    """An instance record as a previous Kern launch would have left it."""
    instance_dir = _fixture_dir()
    definition = lima._render_vm_definition(_sample_config()).replace(
        f"# kern-version: {repo_version()}", f"# kern-version: {stored_version}"
    )
    (Path(instance_dir) / "lima.yaml").write_text(definition)
    return {
        "name": lima._instance_name("kern-test"),
        "status": status,
        "sshLocalPort": ssh_port,
        "dir": instance_dir,
    }


class FakeLimactl:
    """Stateful fake for the ``limactl`` subprocess wrapper.

    Tracks the instance and disk lifecycle the way Lima does: standalone
    disks survive instance deletion, and a deleted instance releases its
    disks. Records every argument array for exact-sequence assertions.
    """

    def __init__(
        self,
        *,
        version: str = "limactl version 1.0.4",
        instance: dict[str, object] | None = None,
        disks: list[dict[str, object]] | None = None,
    ) -> None:
        self.version = version
        self.instance = instance
        self.disks = [dict(source) for source in disks or []]
        self.calls: list[tuple[str, ...]] = []
        self.fail_on: dict[str, Exception] = {}
        self.fail_after_create: Exception | None = None
        # When set, create persists an unexpected definition so the launch's
        # pre-start integrity check can be exercised.
        self.created_definition_override: str | None = None
        self.fail_after_delete: Exception | None = None
        self.drop_disk_after_delete: str | None = None

    def __call__(
        self,
        config: lima.LimaConfig,
        *args: str,
        timeout: int,
        input_text: str | None = None,
        progress_message: str | None = None,
    ) -> str:
        del config, input_text, progress_message
        assert timeout > 0
        self.calls.append(args)
        head = " ".join(args[:2])
        for prefix, error in self.fail_on.items():
            if " ".join(args).startswith(prefix):
                raise error
        if args[0] == "--version":
            return self.version + "\n"
        if args[0] == "validate":
            return ""
        if args[:2] == ("disk", "list"):
            return "".join(json.dumps(disk) + "\n" for disk in self.disks)
        if args[0] == "list":
            return "" if self.instance is None else json.dumps(self.instance) + "\n"
        if args[:2] == ("disk", "create"):
            self.disks.append({"name": args[2], "instance": ""})
            return ""
        if args[0] == "create":
            name = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--name="))
            # Like limactl, copy the submitted definition into the record's
            # instance directory before create can fail.
            instance_dir = _fixture_dir()
            definition = self.created_definition_override
            if definition is None:
                definition = Path(args[-1]).read_text()
            (Path(instance_dir) / "lima.yaml").write_text(definition)
            self.instance = {
                "name": name,
                "status": "Stopped",
                "sshLocalPort": 0,
                "dir": instance_dir,
            }
            if self.fail_after_create is not None:
                raise self.fail_after_create
            return ""
        if args[0] == "start":
            assert self.instance is not None
            self.instance = {**self.instance, "status": "Running", "sshLocalPort": 60022}
            for disk in self.disks:
                disk["instance"] = self.instance["name"]
            return ""
        if args[0] == "stop":
            assert self.instance is not None
            self.instance = {**self.instance, "status": "Stopped", "sshLocalPort": 0}
            return ""
        if args[0] == "delete":
            self.instance = None
            for disk in self.disks:
                disk["instance"] = ""
            if self.drop_disk_after_delete is not None:
                self.disks = [
                    disk
                    for disk in self.disks
                    if disk["name"] != self.drop_disk_after_delete
                ]
            if self.fail_after_delete is not None:
                raise self.fail_after_delete
            return ""
        raise AssertionError(f"unexpected limactl invocation: {head}")


class LimaNamingTests(unittest.TestCase):
    def test_names_are_deterministic_and_lima_safe(self) -> None:
        self.assertEqual(lima._instance_name("alice"), lima._disk_name("alice", "admin")[: -len("-admin")])
        for role in ("admin", "agent"):
            name = lima._disk_name("alice", role)
            self.assertTrue(name.startswith("kern-alice-"))
            self.assertTrue(name.endswith(f"-{role}"))

    def test_normalization_collisions_stay_distinct(self) -> None:
        # "My_Agent" and "my-agent" normalize to the same Lima-safe spelling;
        # the digest keeps their resources distinct.
        self.assertNotEqual(lima._instance_name("My_Agent"), lima._instance_name("my-agent"))
        self.assertTrue(lima._instance_name("My_Agent").startswith("kern-my-agent-"))

    def test_names_with_colliding_short_digests_stay_distinct(self) -> None:
        first = "a" * 32 + "-78766"
        second = "a" * 32 + "-61679"
        self.assertEqual(
            hashlib.sha256(first.encode()).hexdigest()[:8],
            hashlib.sha256(second.encode()).hexdigest()[:8],
        )
        self.assertNotEqual(lima._instance_name(first), lima._instance_name(second))

    def test_normalization_produces_valid_lima_identifier_chunks(self) -> None:
        lima_identifier = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")
        names = ("-foo", "foo-", "__foo--bar__", "-_-", "_")
        for agent_name in names:
            with self.subTest(agent_name=agent_name):
                instance_name = lima._instance_name(agent_name)
                self.assertRegex(instance_name, lima_identifier)
                self.assertNotIn("--", instance_name)
        self.assertNotEqual(lima._local_key("-_-"), lima._local_key("_"))

    def test_normalization_trims_a_separator_exposed_by_truncation(self) -> None:
        local_key = lima._local_key("a" * 15 + "-b")
        self.assertRegex(local_key, r"^a{15}-[0-9a-f]{24}$")
        self.assertNotIn("--", local_key)

    def test_long_names_are_bounded(self) -> None:
        name = lima._instance_name("a" * 50)
        self.assertLessEqual(len(name), len("kern-") + 16 + 1 + 24)

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown storage role"):
            lima._disk_name("alice", "root")


class LimaSmokeContractTests(unittest.TestCase):
    def test_temp_home_leaves_room_for_lima_socket(self) -> None:
        instance_name = lima._instance_name("lima-smoke-2292317860")
        workdir = Path("/tmp") / f"{SMOKE_WORKDIR_PREFIX}{'x' * 16}"
        socket_path = workdir / "lima-home" / instance_name / "ssh.sock.1234567890123456"
        self.assertLess(len(os.fsencode(socket_path)), 108)

    def test_deterministic_definition_is_a_valid_smoke_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            instance_dir.mkdir()
            definition = lima._render_vm_definition(_sample_config()).encode()
            (instance_dir / "lima.yaml").write_bytes(definition)
            smoke = LimaSmoke(root / "smoke")
            with patch.object(smoke, "_instance_record", return_value={"dir": str(instance_dir)}):
                self.assertEqual(
                    smoke._definition_signature(),
                    hashlib.sha256(definition).hexdigest(),
                )


class LimaConfigTests(unittest.TestCase):
    def test_rejects_invalid_agent_name(self) -> None:
        with self.assertRaisesRegex(ConfigError, "agent name"):
            lima.build_lima_config(LifecycleCommand(mode="deploy", agent_name="bad name!"))


class LimaVmDefinitionTests(unittest.TestCase):
    def test_definition_is_isolated_and_secret_free(self) -> None:
        definition = lima._render_vm_definition(_sample_config())
        # Plain mode plus explicit statements of the same guarantees: no host
        # filesystem mounts, no container runtime, Lima never formats the
        # durable disks.
        self.assertIn("plain: true", definition)
        self.assertIn("mounts: []", definition)
        self.assertIn("system: false", definition)
        self.assertIn("user: false", definition)
        self.assertEqual(definition.count("format: false"), 2)
        admin_disk = lima._disk_name("kern-test", "admin")
        agent_disk = lima._disk_name("kern-test", "agent")
        self.assertIn(f'- name: "{admin_disk}"', definition)
        self.assertIn(f'- name: "{agent_disk}"', definition)
        self.assertIn("cpus: 2", definition)
        self.assertIn('memory: "2GiB"', definition)
        self.assertIn("releases/22.04/release", definition)
        self.assertIn('arch: "x86_64"', definition)
        self.assertIn('arch: "aarch64"', definition)
        self.assertIn(f"# kern-version: {repo_version()}", definition)
        # The provision script copies only non-secret disk metadata; nothing
        # secret may appear in the definition Lima persists on the host.
        self.assertIn("mode: system", definition)
        # Lima's cidata contract uses a plural count and singular indexed
        # variables: LIMA_CIDATA_DISKS plus LIMA_CIDATA_DISK_<n>_NAME/DEVICE.
        self.assertIn("${LIMA_CIDATA_DISKS:-}", definition)
        # The lima.env fallback must auto-export what it sources, or the
        # python3 child that writes the metadata file sees an empty environment.
        self.assertIn("set -a\n        . /mnt/lima-cidata/lima.env\n        set +a", definition)
        self.assertIn('f"LIMA_CIDATA_DISK_{index}_NAME"', definition)
        self.assertIn('f"LIMA_CIDATA_DISK_{index}_DEVICE"', definition)
        self.assertNotIn('f"LIMA_CIDATA_DISKS_{index}_NAME"', definition)
        # Lima v2.1.1 emits bare virtio basenames (for example "vdb") in
        # cidata. The handoff validates that exact grammar and records the
        # actual guest block-device path; the live smoke exercises this with
        # real additional disks on every PR.
        self.assertIn('re.fullmatch(r"vd[a-z]", device)', definition)
        self.assertIn("import json\n      import os\n      import re", definition)
        self.assertIn('f"/dev/{device}"', definition)
        self.assertIn("invalid Lima additional-disk device", definition)
        self.assertIn("/run/kern-provider/lima-disks.json", definition)
        for marker in ("password", "token", "PRIVATE KEY", "payload"):
            self.assertNotIn(marker, definition)

    def test_reconfigure_version_hint_must_match_target(self) -> None:
        for stored_version, expected_error in (
            (repo_version(), None),
            ("0.0.0", "reconfigure requires preserved state to match"),
            ("not-a-version", "invalid Kern version hint"),
        ):
            with self.subTest(stored_version=stored_version):
                record = _existing_instance_record(stored_version=stored_version)
                instance = lima.LimaInstance(
                    name=str(record["name"]),
                    status=str(record["status"]),
                    ssh_local_port=int(record["sshLocalPort"]),
                    dir=str(record["dir"]),
                )
                command = LifecycleCommand(
                    mode="reconfigure", agent_name="kern-test", provider="lima"
                )
                if expected_error is None:
                    lima._check_stored_version_hint(command, instance, repo_version())
                else:
                    with self.assertRaisesRegex(ConfigError, expected_error):
                        lima._check_stored_version_hint(command, instance, repo_version())


class LimaSubprocessTests(unittest.TestCase):
    def test_limactl_reports_elapsed_progress_while_waiting(self) -> None:
        release_run = threading.Event()
        progress_reported = threading.Event()
        messages: list[str] = []

        def slow_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            self.assertTrue(release_run.wait(timeout=1))
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def record_progress(message: str) -> None:
            messages.append(message)
            progress_reported.set()
            release_run.set()

        with patch.object(lima.subprocess, "run", side_effect=slow_run), patch.object(
            lima, "_log", side_effect=record_progress
        ), patch.object(lima, "_LIVE_PROGRESS_INTERVAL_SECONDS", 0.01):
            output = lima._limactl(
                _sample_config(),
                "create",
                timeout=60,
                progress_message="still creating the Lima instance",
            )

        self.assertEqual(output, "")
        self.assertTrue(progress_reported.is_set())
        self.assertEqual(messages, ["still creating the Lima instance (1s elapsed)"])


class LimaInspectTests(unittest.TestCase):
    def test_inspect_selects_exact_names_only(self) -> None:
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance={"name": instance_name, "status": "Running", "sshLocalPort": 60022, "dir": "/x"},
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": lima._disk_name("kern-test", "agent"), "instance": ""},
                {"name": "unrelated-disk", "instance": "other-vm"},
            ],
        )
        with patch.object(lima, "_limactl", fake):
            inventory = lima._inspect(_sample_config())
        assert inventory.instance is not None
        self.assertEqual(inventory.instance.status, "Running")
        self.assertEqual(inventory.instance.ssh_local_port, 60022)
        self.assertEqual(set(inventory.disks), {"admin", "agent"})
        self.assertEqual(inventory.disks["agent"].instance, "")

    def test_inspect_accepts_legacy_field_spellings(self) -> None:
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance={"Name": instance_name, "Status": "Stopped", "SSHLocalPort": 0},
            disks=[{"Name": lima._disk_name("kern-test", "admin"), "Instance": ""}],
        )
        with patch.object(lima, "_limactl", fake):
            inventory = lima._inspect(_sample_config())
        assert inventory.instance is not None
        self.assertEqual(inventory.instance.status, "Stopped")
        self.assertIsNone(inventory.instance.ssh_local_port)
        self.assertEqual(set(inventory.disks), {"admin"})

    def test_inspect_rejects_disk_attached_to_foreign_instance(self) -> None:
        fake = FakeLimactl(
            disks=[{"name": lima._disk_name("kern-test", "admin"), "instance": "someone-else"}],
        )
        with patch.object(lima, "_limactl", fake):
            with self.assertRaisesRegex(ConfigError, "unexpected Lima instance someone-else"):
                lima._inspect(_sample_config())

    def test_inspect_rejects_duplicate_disk_records(self) -> None:
        disk = {"name": lima._disk_name("kern-test", "admin"), "instance": ""}
        fake = FakeLimactl(disks=[disk, dict(disk)])
        with patch.object(lima, "_limactl", fake):
            with self.assertRaisesRegex(ConfigError, "duplicate records"):
                lima._inspect(_sample_config())

    def test_inspect_rejects_malformed_output(self) -> None:
        def bad_output(config: lima.LimaConfig, *args: str, timeout: int, input_text: str | None = None) -> str:
            return "not json\n"

        with patch.object(lima, "_limactl", bad_output):
            with self.assertRaisesRegex(ConfigError, "could not parse limactl list"):
                lima._inspect(_sample_config())

    def test_inspect_rejects_invalid_ssh_port(self) -> None:
        fake = FakeLimactl(
            instance={
                "name": lima._instance_name("kern-test"),
                "status": "Running",
                "sshLocalPort": "60022",
            },
        )
        with patch.object(lima, "_limactl", fake):
            with self.assertRaisesRegex(ConfigError, "invalid SSH port"):
                lima._inspect(_sample_config())

    def test_preflight_rejects_out_of_range_limactl(self) -> None:
        for version in ("limactl version 0.23.2", "limactl version 3.0.0"):
            with self.subTest(version=version):
                fake = FakeLimactl(version=version)
                with patch.object(lima, "_limactl", fake), patch.object(
                    lima.shutil, "which", return_value="/usr/bin/limactl"
                ):
                    with self.assertRaisesRegex(ConfigError, "outside Kern's tested range"):
                        lima._preflight(_sample_config())

    def test_preflight_requires_limactl_on_path(self) -> None:
        with patch.object(lima.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ConfigError, "limactl was not found on PATH"):
                lima._preflight(_sample_config())


class LimaPreflightMatrixTests(unittest.TestCase):
    def _inventory(
        self, *, instance: bool, roles: set[str]
    ) -> lima.LimaInventory:
        instance_record = (
            lima.LimaInstance(
                name=lima._instance_name("kern-test"),
                status="Running",
                ssh_local_port=60022,
                dir=None,
            )
            if instance
            else None
        )
        disks = {
            role: lima.LimaDisk(name=lima._disk_name("kern-test", role), instance="")
            for role in roles
        }
        return lima.LimaInventory(instance=instance_record, disks=disks)

    def test_operation_matrix(self) -> None:
        cases = [
            ("deploy", True, {"admin", "agent"}, "deploy requires no existing Kern Lima instance"),
            ("deploy", False, {"admin"}, "deploy requires no existing Kern Lima data disks"),
            ("deploy", False, set(), None),
            ("upgrade", False, {"admin", "agent"}, "upgrade requires an existing Kern Lima instance"),
            ("upgrade", True, {"admin"}, "missing agent"),
            ("upgrade", True, {"admin", "agent"}, None),
            ("recover", True, {"admin", "agent"}, "recover requires no existing Kern Lima instance"),
            ("recover", False, set(), "found none, missing admin, agent"),
            ("recover", False, {"admin", "agent"}, None),
            ("reconfigure", False, {"admin", "agent"}, "reconfigure requires an existing Kern Lima instance"),
            ("reconfigure", True, {"admin", "agent"}, None),
        ]
        for mode, has_instance, roles, expected_error in cases:
            with self.subTest(mode=mode, instance=has_instance, roles=sorted(roles)):
                command = LifecycleCommand(mode=mode, agent_name="kern-test", provider="lima")
                inventory = self._inventory(instance=has_instance, roles=roles)
                if expected_error is None:
                    lima._validate_lima_preflight(command, inventory)
                else:
                    with self.assertRaisesRegex(ConfigError, expected_error):
                        lima._validate_lima_preflight(command, inventory)

    def test_operation_matrix_rejects_unsupported_instance_state(self) -> None:
        inventory = self._inventory(instance=True, roles={"admin", "agent"})
        assert inventory.instance is not None
        inventory = lima.LimaInventory(
            instance=lima.LimaInstance(
                name=inventory.instance.name,
                status="Broken",
                ssh_local_port=None,
                dir=None,
            ),
            disks=inventory.disks,
        )
        with self.assertRaisesRegex(ConfigError, "unsupported state 'Broken'"):
            lima._validate_lima_preflight(
                LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima"),
                inventory,
            )

    def test_deploy_existing_disks_error_gives_recovery_and_cleanup_paths(self) -> None:
        admin_disk = lima._disk_name("kern-test", "admin")
        agent_disk = lima._disk_name("kern-test", "agent")
        inventory = self._inventory(instance=False, roles={"admin", "agent"})
        with self.assertRaises(ConfigError) as raised:
            lima._validate_lima_preflight(
                LifecycleCommand(mode="deploy", agent_name="kern-test", provider="lima"),
                inventory,
            )
        message = str(raised.exception)
        self.assertIn("run recover", message)
        self.assertIn(f"limactl disk delete {admin_disk}", message)
        self.assertIn(f"limactl disk delete {agent_disk}", message)
        self.assertIn("retry deploy", message)

    def test_recover_rejects_stale_attachment_to_absent_instance(self) -> None:
        instance_name = lima._instance_name("kern-test")
        inventory = lima.LimaInventory(
            instance=None,
            disks={
                role: lima.LimaDisk(
                    name=lima._disk_name("kern-test", role),
                    instance=instance_name,
                )
                for role in ("admin", "agent")
            },
        )
        with self.assertRaisesRegex(ConfigError, "still report attachment to an absent instance"):
            lima._validate_lima_preflight(
                LifecycleCommand(mode="recover", agent_name="kern-test", provider="lima"),
                inventory,
            )


class LimaLifecycleFlowTests(unittest.TestCase):
    def _run_lifecycle(
        self,
        command: LifecycleCommand,
        fake: FakeLimactl,
        home: str,
        *,
        provision_side_effect: object = None,
        archive_side_effect: BaseException | None = None,
    ) -> tuple[int, str, list[tuple[str, ...]]]:
        stdout = io.StringIO()
        with patch.object(lima, "_limactl", fake), \
                patch.object(lima.shutil, "which", return_value="/usr/bin/limactl"), \
                patch.object(lima, "_generate_deploy_key", side_effect=_fake_deploy_key), \
                patch.object(lima, "_deliver_stage_one") as deliver, \
                patch.object(
                    lima, "_write_runtime_code_archive", side_effect=archive_side_effect
                ), \
                patch.object(
                    lima, "_provision_over_ssh", side_effect=provision_side_effect
                ) as provision, \
                patch.object(lima.Path, "home", return_value=Path(home)), \
                patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", stdout):
            exit_code = lima.main_for_lifecycle(command)
        self.deliver = deliver
        self.provision = provision
        return exit_code, stdout.getvalue(), fake.calls

    def test_deploy_happy_path(self) -> None:
        fake = FakeLimactl()
        command = LifecycleCommand(
            mode="deploy",
            agent_name="kern-test",
            admin_password_sha256=SAMPLE_ADMIN_PASSWORD_SHA256,
            operator_ssh_public_key=SAMPLE_SSH_PUBLIC_KEY,
            provider="lima",
        )
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
            self.assertEqual(exit_code, 0)

            instance_name = lima._instance_name("kern-test")
            admin_disk = lima._disk_name("kern-test", "admin")
            agent_disk = lima._disk_name("kern-test", "agent")
            # Both durable disks are created with their fixed sizes, then the
            # instance is created and started; nothing is ever disk-deleted.
            self.assertIn(("disk", "create", admin_disk, "--size=16GiB"), calls)
            self.assertIn(("disk", "create", agent_disk, "--size=16GiB"), calls)
            self.assertTrue(any(call[0] == "create" for call in calls))
            self.assertIn(("start", "--tty=false", instance_name), calls)
            self.assertFalse(any(call[:2] == ("disk", "delete") for call in calls))
            self.assertFalse(any(call[0] == "delete" for call in calls))

            # Stage one is delivered over Lima's SSH; shared provisioning then
            # runs against the loopback endpoint on the allocated port.
            self.deliver.assert_called_once()
            stage_one = self.deliver.call_args.args[2]
            self.assertIn("useradd --create-home --shell /bin/bash kern-operator", stage_one)
            self.assertIn("ssh-ed25519 AAAADEPLOY kern-deploy", stage_one)
            embedded = next(line for line in stage_one.splitlines() if line.startswith("{"))
            payload = json.loads(embedded)
            self.assertEqual(payload["storage"]["resolver"], "lima")
            self.assertEqual(
                payload["storage"]["resolver_input"]["admin"], {"disk_name": admin_disk}
            )
            self.assertEqual(
                payload["storage"]["resolver_input"]["agent"], {"disk_name": agent_disk}
            )
            self.provision.assert_called_once_with(
                "127.0.0.1",
                self.provision.call_args.args[1],
                self.provision.call_args.args[2],
                port=60022,
            )

            result = json.loads(stdout)
            self.assertEqual(result["provider"], "lima")
            self.assertEqual(result["host"], {"id": instance_name, "state": "running"})
            self.assertEqual(
                result["ssh"], {"host": "127.0.0.1", "port": 60022, "user": "kern-operator"}
            )
            self.assertEqual(result["storage"]["admin"], {"id": admin_disk})
            self.assertNotIn("tunnel_token", stdout)

    def test_launch_failure_deletes_instance_and_preserves_disks(self) -> None:
        fake = FakeLimactl()
        fake.fail_on["start"] = subprocess.CalledProcessError(
            1, ["limactl", "start"], stderr="qemu exploded"
        )
        command = LifecycleCommand(
            mode="deploy",
            agent_name="kern-test",
            admin_password_sha256=SAMPLE_ADMIN_PASSWORD_SHA256,
            operator_ssh_public_key=SAMPLE_SSH_PUBLIC_KEY,
            provider="lima",
        )
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        # The half-provisioned instance is deleted; both durable disks
        # survive and are never passed to a disk-delete operation.
        self.assertTrue(any(call[0] == "delete" for call in calls))
        self.assertFalse(any(call[:2] == ("disk", "delete") for call in calls))
        self.assertEqual(len(fake.disks), 2)
        self.assertIsNone(fake.instance)

    def test_create_failure_after_instance_record_cleans_up_exact_instance(self) -> None:
        fake = FakeLimactl()
        fake.fail_after_create = subprocess.TimeoutExpired(
            ["limactl", "create"], timeout=lima._CREATE_TIMEOUT_SECONDS
        )
        command = LifecycleCommand(
            mode="deploy",
            agent_name="kern-test",
            admin_password_sha256=SAMPLE_ADMIN_PASSWORD_SHA256,
            operator_ssh_public_key=SAMPLE_SSH_PUBLIC_KEY,
            provider="lima",
        )
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(any(call[0] == "delete" for call in calls))
        self.assertFalse(any(call[:2] == ("disk", "delete") for call in calls))
        self.assertEqual(len(fake.disks), 2)
        self.assertIsNone(fake.instance)

    def test_archive_build_failure_precedes_vm_deletion(self) -> None:
        # The runtime archive is built with every other local artifact before
        # replacement deletes the working VM; a local build failure must leave
        # the host running.
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance=_existing_instance_record(),
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": lima._disk_name("kern-test", "agent"), "instance": instance_name},
            ],
        )
        command = LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima")
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(
                command, fake, home, archive_side_effect=OSError("no space left on device")
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(any(call[0] in {"stop", "delete"} for call in calls))
        self.assertIsNotNone(fake.instance)

    def test_launch_rejects_unexpected_stored_definition(self) -> None:
        # Even though direct mutation is unsupported, Kern never boots a
        # definition that differs from the isolated one it generated.
        fake = FakeLimactl()
        fake.created_definition_override = "plain: false\n"
        command = LifecycleCommand(
            mode="deploy",
            agent_name="kern-test",
            admin_password_sha256=SAMPLE_ADMIN_PASSWORD_SHA256,
            operator_ssh_public_key=SAMPLE_SSH_PUBLIC_KEY,
            provider="lima",
        )
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(any(call[0] == "start" for call in calls))
        self.assertTrue(any(call[0] == "delete" for call in calls))
        self.deliver.assert_not_called()
        self.assertIsNone(fake.instance)

    def test_shared_provisioning_failure_deletes_instance_and_preserves_disks(self) -> None:
        fake = FakeLimactl()
        command = LifecycleCommand(
            mode="deploy",
            agent_name="kern-test",
            admin_password_sha256=SAMPLE_ADMIN_PASSWORD_SHA256,
            operator_ssh_public_key=SAMPLE_SSH_PUBLIC_KEY,
            provider="lima",
        )
        failure = subprocess.CalledProcessError(
            1, ["ssh", "bootstrap"], stderr="bootstrap failed"
        )
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(
                command, fake, home, provision_side_effect=failure
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(any(call[0] == "delete" for call in calls))
        self.assertFalse(any(call[:2] == ("disk", "delete") for call in calls))
        self.assertEqual(len(fake.disks), 2)
        self.assertIsNone(fake.instance)

    def test_ambiguous_delete_still_waits_for_disk_detachment(self) -> None:
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance=_existing_instance_record(),
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": lima._disk_name("kern-test", "agent"), "instance": instance_name},
            ],
        )
        fake.fail_after_delete = subprocess.TimeoutExpired(
            ["limactl", "delete"], timeout=lima._DELETE_TIMEOUT_SECONDS
        )
        with patch.object(lima, "_limactl", fake), patch.object(
            lima, "_wait_for_detached_disks"
        ) as wait_for_detached:
            lima._delete_instance(_sample_config(), instance_name)
        wait_for_detached.assert_called_once_with(_sample_config())
        self.assertIsNone(fake.instance)

    def test_upgrade_replaces_instance_and_reuses_disks(self) -> None:
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance=_existing_instance_record(),
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": lima._disk_name("kern-test", "agent"), "instance": instance_name},
            ],
        )
        command = LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima")
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
        self.assertEqual(exit_code, 0)
        # The complete replacement is validated before the old instance is
        # deleted; the preserved disks are reused, not recreated.
        self.assertIn(("delete", "--force", "--tty=false", instance_name), calls)
        self.assertFalse(any(call[:2] == ("disk", "create") for call in calls))
        self.assertFalse(any(call[:2] == ("disk", "delete") for call in calls))
        validate_index = next(i for i, call in enumerate(calls) if call[0] == "validate")
        delete_index = calls.index(("delete", "--force", "--tty=false", instance_name))
        create_index = next(i for i, call in enumerate(calls) if call[0] == "create")
        self.assertLess(validate_index, delete_index)
        self.assertLess(delete_index, create_index)

    def test_upgrade_version_hint_rejects_before_deleting_instance(self) -> None:
        instance_name = lima._instance_name("kern-test")
        for stored_version in (repo_version(), "999.0.0"):
            with self.subTest(stored_version=stored_version):
                fake = FakeLimactl(
                    instance=_existing_instance_record(stored_version=stored_version),
                    disks=[
                        {"name": lima._disk_name("kern-test", role), "instance": instance_name}
                        for role in ("admin", "agent")
                    ],
                )
                command = LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima")
                with tempfile.TemporaryDirectory() as home:
                    exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout, "")
                self.assertFalse(any(call[0] in {"stop", "delete", "create"} for call in calls))
                self.assertIsNotNone(fake.instance)

    def test_replacement_validation_failure_preserves_working_instance(self) -> None:
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance=_existing_instance_record(),
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": lima._disk_name("kern-test", "agent"), "instance": instance_name},
            ],
        )
        fake.fail_on["validate"] = subprocess.CalledProcessError(
            1, ["limactl", "validate"], stderr="requested VM shape is unavailable"
        )
        command = LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima")
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIsNotNone(fake.instance)
        self.assertEqual(fake.instance["status"], "Running")
        self.assertTrue(any(call[0] == "validate" for call in calls))
        self.assertFalse(any(call[0] in {"stop", "delete", "create"} for call in calls))

    def test_upgrade_refuses_to_recreate_disk_lost_after_preflight(self) -> None:
        # If Lima no longer reports a preserved disk after deleting disposable
        # compute, replacement must not silently create empty storage.
        instance_name = lima._instance_name("kern-test")
        agent_disk = lima._disk_name("kern-test", "agent")
        fake = FakeLimactl(
            instance=_existing_instance_record(),
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": agent_disk, "instance": instance_name},
            ],
        )
        fake.drop_disk_after_delete = agent_disk
        command = LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima")
        with tempfile.TemporaryDirectory() as home:
            exit_code, stdout, calls = self._run_lifecycle(command, fake, home)
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(any(call[:2] == ("disk", "create") for call in calls))
        self.assertFalse(any(call[0] == "create" for call in calls))

    def test_missing_ssh_transport_fails_before_deletion(self) -> None:
        # ssh and scp are first invoked only after the working VM would have
        # been deleted; preflight must verify them so the host stays online.
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(
            instance=_existing_instance_record(),
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": instance_name},
                {"name": lima._disk_name("kern-test", "agent"), "instance": instance_name},
            ],
        )

        def which(tool: str) -> str | None:
            return None if tool == "scp" else f"/usr/bin/{tool}"

        command = LifecycleCommand(mode="upgrade", agent_name="kern-test", provider="lima")
        with tempfile.TemporaryDirectory() as home:
            with patch.object(lima, "_limactl", fake), \
                    patch.object(lima.shutil, "which", side_effect=which), \
                    patch.object(lima.Path, "home", return_value=Path(home)), \
                    patch("sys.stderr", io.StringIO()) as stderr, \
                    patch("sys.stdout", io.StringIO()) as stdout:
                exit_code = lima.main_for_lifecycle(command)
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("scp was not found on PATH", stderr.getvalue())
        self.assertFalse(any(call[0] in {"stop", "delete", "create"} for call in fake.calls))
        self.assertIsNotNone(fake.instance)

    def test_github_delivery_is_rejected(self) -> None:
        command = LifecycleCommand(
            mode="deploy",
            agent_name="kern-test",
            admin_password_sha256=SAMPLE_ADMIN_PASSWORD_SHA256,
            operator_ssh_public_key=SAMPLE_SSH_PUBLIC_KEY,
            github_commit_sha="a" * 40,
            provider="lima",
        )
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(lima.main_for_lifecycle(command), 2)
        self.assertIn("--bootstrap-from-github is not supported", stderr.getvalue())


class LimaPowerTests(unittest.TestCase):
    def _run_power(
        self, mode: str, fake: FakeLimactl
    ) -> tuple[int, str, list[tuple[str, ...]]]:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as home:
            with patch.object(lima, "_limactl", fake), \
                    patch.object(lima.shutil, "which", return_value="/usr/bin/limactl"), \
                    patch.object(lima.Path, "home", return_value=Path(home)), \
                    patch("sys.stderr", io.StringIO()), \
                    patch("sys.stdout", stdout):
                exit_code = lima.main_for_power(mode, "kern-test")
        return exit_code, stdout.getvalue(), fake.calls

    def _fake_with_instance(
        self, status: str, definition: str | None = "generated"
    ) -> FakeLimactl:
        instance_name = lima._instance_name("kern-test")
        instance_dir = tempfile.TemporaryDirectory()
        self.addCleanup(instance_dir.cleanup)
        if definition is not None:
            if definition == "generated":
                definition = lima._render_vm_definition(_sample_config())
            (Path(instance_dir.name) / "lima.yaml").write_text(definition)
        return FakeLimactl(
            instance={
                "name": instance_name,
                "status": status,
                "sshLocalPort": 60022 if status == "Running" else 0,
                "dir": instance_dir.name,
            },
            disks=[
                {"name": lima._disk_name("kern-test", "admin"), "instance": ""},
                {"name": lima._disk_name("kern-test", "agent"), "instance": ""},
            ],
        )

    def test_start_from_stopped(self) -> None:
        exit_code, stdout, calls = self._run_power("start", self._fake_with_instance("Stopped"))
        self.assertEqual(exit_code, 0)
        instance_name = lima._instance_name("kern-test")
        self.assertIn(("start", "--tty=false", instance_name), calls)
        result = json.loads(stdout)
        self.assertEqual(result["host"]["state"], "running")
        self.assertEqual(result["initial_state"], "stopped")
        self.assertEqual(result["ssh"]["port"], 60022)
        # Power operations install nothing, so no version is reported.
        self.assertNotIn("version", result)

    def test_stop_from_running(self) -> None:
        exit_code, stdout, calls = self._run_power("stop", self._fake_with_instance("Running"))
        self.assertEqual(exit_code, 0)
        self.assertIn(("stop", lima._instance_name("kern-test")), calls)
        result = json.loads(stdout)
        self.assertEqual(result["host"]["state"], "stopped")
        self.assertIsNone(result["ssh"]["port"])

    def test_power_is_idempotent_for_terminal_states(self) -> None:
        exit_code, _stdout, calls = self._run_power("start", self._fake_with_instance("Running"))
        self.assertEqual(exit_code, 0)
        self.assertFalse(any(call[0] == "start" for call in calls))
        exit_code, _stdout, calls = self._run_power("stop", self._fake_with_instance("Stopped"))
        self.assertEqual(exit_code, 0)
        self.assertFalse(any(call[0] == "stop" for call in calls))

    def test_power_timeout_returns_controlled_failure(self) -> None:
        fake = self._fake_with_instance("Stopped")
        fake.fail_on["start"] = subprocess.TimeoutExpired(
            ["limactl", "start"], timeout=lima._START_TIMEOUT_SECONDS
        )
        exit_code, stdout, calls = self._run_power("start", fake)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(("start", "--tty=false", lima._instance_name("kern-test")), calls)

    def test_power_requires_instance_and_disks(self) -> None:
        exit_code, stdout, _calls = self._run_power("start", FakeLimactl())
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        instance_name = lima._instance_name("kern-test")
        fake = FakeLimactl(instance={"name": instance_name, "status": "Stopped", "sshLocalPort": 0})
        exit_code, _stdout, _calls = self._run_power("start", fake)
        self.assertEqual(exit_code, 2)

    def test_power_rejects_unknown_state(self) -> None:
        exit_code, _stdout, calls = self._run_power("start", self._fake_with_instance("Broken"))
        self.assertEqual(exit_code, 2)
        self.assertFalse(any(call[0] in {"start", "stop"} for call in calls))

    def test_start_rejects_tampered_stored_definition(self) -> None:
        # An out-of-band edit that mounts the host home into the VM must fail
        # closed before limactl start can boot it.
        tampered = lima._render_vm_definition(_sample_config()).replace(
            "mounts: []", 'mounts:\n- location: "~"'
        )
        exit_code, stdout, calls = self._run_power(
            "start", self._fake_with_instance("Stopped", definition=tampered)
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(any(call[0] == "start" for call in calls))

    def test_start_rejects_commented_out_invariants(self) -> None:
        # Keeping the generated lines as comments while adding effective
        # overrides must not fool the check: the comparison is exact, not a
        # marker search.
        tricked = lima._render_vm_definition(_sample_config()).replace(
            "plain: true", "# plain: true\nplain: false"
        )
        exit_code, _stdout, calls = self._run_power(
            "start", self._fake_with_instance("Stopped", definition=tricked)
        )
        self.assertEqual(exit_code, 2)
        self.assertFalse(any(call[0] == "start" for call in calls))

    def test_start_rejects_non_pinned_vm_shape(self) -> None:
        # The VM shape is pinned like the EC2 instance type; a definition
        # resized out of band is not one Kern generates.
        resized = lima._render_vm_definition(_sample_config()).replace(
            "cpus: 2", "cpus: 8"
        )
        exit_code, _stdout, calls = self._run_power(
            "start", self._fake_with_instance("Stopped", definition=resized)
        )
        self.assertEqual(exit_code, 2)
        self.assertFalse(any(call[0] == "start" for call in calls))

    def test_start_rejects_missing_stored_definition(self) -> None:
        exit_code, stdout, calls = self._run_power(
            "start", self._fake_with_instance("Stopped", definition=None)
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(any(call[0] == "start" for call in calls))

    def test_start_rejects_undecodable_stored_definition(self) -> None:
        fake = self._fake_with_instance("Stopped", definition=None)
        assert fake.instance is not None
        Path(str(fake.instance["dir"]), "lima.yaml").write_bytes(b"\xff\xfe not utf-8")
        exit_code, stdout, calls = self._run_power("start", fake)
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(any(call[0] == "start" for call in calls))

    def test_stop_does_not_require_stored_definition(self) -> None:
        # Stopping a tampered or definition-less instance must stay possible;
        # only booting the stored definition is gated.
        exit_code, _stdout, calls = self._run_power(
            "stop", self._fake_with_instance("Running", definition=None)
        )
        self.assertEqual(exit_code, 0)
        self.assertIn(("stop", lima._instance_name("kern-test")), calls)

    def test_power_os_error_returns_controlled_failure(self) -> None:
        # A process-launch or filesystem error exits through the diagnostic
        # contract, not a traceback.
        fake = self._fake_with_instance("Stopped")
        fake.fail_on["start --tty=false"] = FileNotFoundError("limactl vanished")
        exit_code, stdout, _calls = self._run_power("start", fake)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")

    def test_power_never_renders_bootstrap(self) -> None:
        with patch.object(lima, "_bootstrap_payload", side_effect=AssertionError("no bootstrap in power ops")), \
                patch.object(lima, "_provision_over_ssh", side_effect=AssertionError("no SSH provisioning in power ops")):
            exit_code, _stdout, _calls = self._run_power("start", self._fake_with_instance("Stopped"))
        self.assertEqual(exit_code, 0)


class LimaStageOneDeliveryTests(unittest.TestCase):
    def test_stage_one_streams_script_over_lima_ssh_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh_config = Path(tmp) / "ssh.config"
            ssh_config.write_text("Host lima-x\n")
            instance = lima.LimaInstance(
                name=lima._instance_name("kern-test"),
                status="Running",
                ssh_local_port=60022,
                dir=tmp,
            )
            with patch.object(lima.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
                lima._deliver_stage_one(_sample_config(), instance, "echo stage-one")
            command = run.call_args.args[0]
            self.assertEqual(command[0], "ssh")
            self.assertIn("-F", command)
            self.assertIn(str(ssh_config), command)
            self.assertIn(f"lima-{instance.name}", command)
            self.assertEqual(command[-3:], ["sudo", "bash", "-s"])
            # The script rides stdin, never an argument.
            self.assertEqual(run.call_args.kwargs["input"], "echo stage-one")
            self.assertNotIn("echo stage-one", command)

    def test_stage_one_fails_after_bounded_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh_config = Path(tmp) / "ssh.config"
            ssh_config.write_text("Host lima-x\n")
            instance = lima.LimaInstance(
                name=lima._instance_name("kern-test"),
                status="Running",
                ssh_local_port=60022,
                dir=tmp,
            )
            with patch.object(lima.subprocess, "run") as run, patch.object(
                lima.time, "sleep"
            ):
                run.return_value = subprocess.CompletedProcess([], 255, stdout="", stderr="refused")
                with self.assertRaisesRegex(ConfigError, "could not stage provisioning"):
                    lima._deliver_stage_one(_sample_config(), instance, "echo stage-one")
            self.assertEqual(run.call_count, 5)


class LimaCliWiringTests(unittest.TestCase):
    def test_parse_args_carries_provider(self) -> None:
        command = deploy._parse_args(
            "deploy",
            [
                "--agent-name",
                "kern-test",
                "--admin-password-sha256",
                SAMPLE_ADMIN_PASSWORD_SHA256,
                "--operator-ssh-public-key",
                SAMPLE_SSH_PUBLIC_KEY,
                "--provider",
                "lima",
            ],
        )
        self.assertEqual(command.provider, "lima")

    def test_vm_shape_flags_no_longer_exist(self) -> None:
        # The Lima VM shape is pinned like the EC2 instance type; the flags
        # were removed rather than left as silent no-ops.
        with patch("sys.stderr", io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                deploy._parse_args(
                    "upgrade",
                    ["--agent-name", "kern-test", "--provider", "lima", "--lima-cpus", "8"],
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_provider_defaults_to_aws(self) -> None:
        command = deploy._parse_args(
            "upgrade",
            ["--agent-name", "kern-test"],
        )
        self.assertEqual(command.provider, "aws")

    def test_parse_args_rejects_github_delivery_with_lima(self) -> None:
        with patch("sys.stderr", io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                deploy._parse_args(
                    "deploy",
                    [
                        "--agent-name",
                        "kern-test",
                        "--admin-password-sha256",
                        SAMPLE_ADMIN_PASSWORD_SHA256,
                        "--provider",
                        "lima",
                        "--bootstrap-from-github",
                        "a" * 40,
                    ],
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--bootstrap-from-github is not supported", stderr.getvalue())

    def test_lifecycle_dispatches_to_lima_without_aws_environment(self) -> None:
        # No AWS_REGION or credentials are required for the local provider.
        with patch.dict(os.environ, {}, clear=True), \
                patch("host.cli.lifecycle_lima.main_for_lifecycle", return_value=0) as dispatch:
            exit_code = deploy.main_for_mode(
                "deploy",
                [
                    "--agent-name",
                    "kern-test",
                    "--admin-password-sha256",
                    SAMPLE_ADMIN_PASSWORD_SHA256,
                    "--operator-ssh-public-key",
                    SAMPLE_SSH_PUBLIC_KEY,
                    "--provider",
                    "lima",
                ],
            )
        self.assertEqual(exit_code, 0)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0].provider, "lima")

    def test_power_dispatches_to_lima(self) -> None:
        with patch.dict(os.environ, {}, clear=True), \
                patch("host.cli.lifecycle_lima.main_for_power", return_value=0) as dispatch:
            exit_code = power.main_for_power_mode(
                "stop", ["--agent-name", "kern-test", "--provider", "lima"]
            )
        self.assertEqual(exit_code, 0)
        dispatch.assert_called_once_with("stop", "kern-test")


class LimaBootstrapHandoffTests(unittest.TestCase):
    def test_rendered_bootstrap_consumes_provider_neutral_storage_contract(self) -> None:
        bootstrap = render._render_bootstrap()
        self.assertIn("python3 -m host.bootstrap.storage_resolver", bootstrap)
        self.assertNotIn("resolve_ebs_device", bootstrap)
        self.assertNotIn("resolve_lima_device", bootstrap)
        self.assertNotIn("/run/kern-provider/lima-disks.json", bootstrap)
        self.assertNotIn('payload_value storage.resolver', bootstrap)
        self.assertNotIn("aws)", bootstrap)
        self.assertNotIn("lima)", bootstrap)
        # The two roles may never share one block device, and a mismatched
        # existing filesystem label fails closed.
        self.assertIn("storage resolver did not return two distinct role devices", bootstrap)
        self.assertIn("refusing to mount", bootstrap)

    def test_guest_storage_spec_shape(self) -> None:
        spec = lima._guest_storage_spec({"admin": "a-disk", "agent": "b-disk"})
        self.assertEqual(
            spec,
            {
                "resolver": "lima",
                "resolver_input": {
                    "admin": {"disk_name": "a-disk"},
                    "agent": {"disk_name": "b-disk"},
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
