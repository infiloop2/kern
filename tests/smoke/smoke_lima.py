#!/usr/bin/env python3
"""Boot a real Lima host and exercise its provider lifecycle end to end.

The smoke owns a unique agent name, a temporary Lima home, an ephemeral SSH
key, and every resource it creates. It needs no cloud account or provider
credential. Run it on a Linux host with KVM, QEMU, Lima, OpenSSH, and outbound
network access::

    python3 tests/smoke/smoke_lima.py

It intentionally covers the integration seams that the fake-limactl unit
tests cannot: a real Ubuntu boot, real data-disk attachment and formatting,
SSH provisioning, the credential-free live-host contract also exercised by
the fresh AWS smoke, power operations, replacement with durable state, upgrade
from older version metadata, and recovery after the disposable VM record is
removed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookies
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from host.cli import lifecycle_lima as lima
from host.constants import ADMIN_API_PORT, PROXY_PORT
from host.version import parse_version, repo_version
from tests.smoke.smoke_aws import AwsSmoke


LIFECYCLE_TIMEOUT_SECONDS = 2_400
HEALTH_TIMEOUT_SECONDS = 300
SSH_TIMEOUT_SECONDS = 180
SENTINEL_PATH = "/mnt/kern-agent/agent-home/.kern-lima-smoke-sentinel"
SMOKE_WORKDIR_PREFIX = "kl-"


class LimaSmoke(AwsSmoke):
    """Lima lifecycle coverage plus the credential-free live-host contract.

    ``AwsSmoke`` owns the broader provider-neutral checks. This class replaces
    its EC2 transport and lifecycle with Lima equivalents, then runs those
    checks against the real guest before exercising Lima replacement paths.
    """

    def __init__(self, workdir: Path) -> None:
        # Initialize the complete shared host-check contract, then replace
        # only provider-specific transport and lifecycle state.
        super().__init__(workdir)
        self.lima_home = workdir / "lima-home"
        self.lima_home.mkdir(parents=True)
        run_suffix = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))[-10:]
        if not run_suffix:
            run_suffix = secrets.token_hex(4)
        self.agent_name = f"lima-smoke-{run_suffix}"
        self.instance_name = lima._instance_name(self.agent_name)
        self.disk_names = {
            role: lima._disk_name(self.agent_name, role)
            for role in ("admin", "agent")
        }
        self.private_key = workdir / "operator-key"
        self.ssh_key = str(self.private_key)
        self.replacement_key = workdir / "replacement-key"
        self.known_hosts = workdir / "known-hosts"
        self.admin_password = secrets.token_urlsafe(32)
        self.sentinel = secrets.token_hex(32)
        self.root_sentinel = secrets.token_hex(32)
        self.memory_page_id = f"lima-smoke-{run_suffix}"
        self.app_id: str | None = None
        self.disk_identities: dict[str, tuple[int, int, int, int]] | None = None
        self.result: dict[str, Any] = {}
        self.session_cookie: str | None = None
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(REPO_ROOT)
        self.env["LIMA_HOME"] = str(self.lima_home)

    def run(self) -> None:
        self._require_tools()
        self._generate_key()
        self._remove_owned_resources()

        self._step("fresh deploy")
        self.result = self._lifecycle(
            "deploy",
            "--admin-password-sha256",
            hashlib.sha256(self.admin_password.encode()).hexdigest(),
            "--operator-ssh-public-key",
            self._public_key(),
        )
        self._assert_result(self.result, expected_state="running")
        self._remember_disk_identities()
        first_definition = self._definition_signature()
        self._check_definition_contract()
        self._check_definition_tamper_power_contract()
        self._assert_admin_not_directly_forwarded()
        self._write_sentinel()
        self._write_root_sentinel()
        # A stop/start can make SSH ready before the admin services have
        # finished returning. Open the tunnel and wait for the same bounded
        # healthy-host contract before the first durable API mutation.
        self._wait_for_health()
        self.check_credential_free_host_surface()
        self._seed_workspace_state()
        self._configure_network_policy()
        self._check_live_host()
        self._check_network_enforcement()
        self._ok("real Ubuntu host provisioned; SSH, UI, API, storage, and isolation verified")

        self._step("power stop and start")
        self._close_tunnel()
        stopped = self._lifecycle("stop")
        self._assert_result(stopped, expected_state="stopped", expect_version=False)
        if stopped.get("initial_state") != "running":
            raise AssertionError(f"stop reported an unexpected initial state: {stopped}")
        stopped_again = self._lifecycle("stop")
        self._assert_result(stopped_again, expected_state="stopped", expect_version=False)
        if stopped_again.get("initial_state") != "stopped":
            raise AssertionError(f"second stop was not idempotent: {stopped_again}")
        started = self._lifecycle("start")
        self._assert_result(started, expected_state="running", expect_version=False)
        if started.get("initial_state") != "stopped":
            raise AssertionError(f"start reported an unexpected initial state: {started}")
        started_again = self._lifecycle("start")
        self._assert_result(started_again, expected_state="running", expect_version=False)
        if started_again.get("initial_state") != "running":
            raise AssertionError(f"second start was not idempotent: {started_again}")
        self.result = started_again
        if self._definition_signature() != first_definition:
            raise AssertionError("power operations replaced or rewrote the VM definition")
        self._check_live_host()
        self._assert_root_sentinel(present=True)
        self._ok("stop/start preserved the instance, disks, operator access, and data")

        self._step("upgrade from older version metadata")
        self._write_older_version_metadata()
        self._close_tunnel()
        self.result = self._lifecycle("upgrade")
        self._reset_operator_transport()
        self._assert_result(self.result, expected_state="running")
        # The VM definition is intentionally deterministic. Losing a marker
        # stored only on the disposable root proves replacement instead.
        self._assert_root_sentinel(present=False)
        self._check_live_host()
        self._write_root_sentinel()
        self._ok(
            "upgrade replaced compute and preserved both durable disks"
        )

        self._step("reconfigure credentials")
        self._generate_key(self.replacement_key)
        old_key = self.private_key
        old_password = self.admin_password
        new_password = secrets.token_urlsafe(32)
        self._close_tunnel()
        self.result = self._lifecycle(
            "reconfigure",
            "--admin-password-sha256",
            hashlib.sha256(new_password.encode()).hexdigest(),
            "--operator-ssh-public-key",
            self.replacement_key.with_suffix(".pub").read_text().strip(),
        )
        self._reset_operator_transport()
        self.private_key = self.replacement_key
        self.ssh_key = str(self.private_key)
        self.admin_password = new_password
        self._assert_result(self.result, expected_state="running")
        if self._probe_ssh_key(old_key):
            raise AssertionError("the replaced operator SSH key still authenticates")
        self._open_tunnel()
        if self._login_status(old_password)[0] != 401:
            raise AssertionError("the replaced admin password still authenticates")
        status, cookie = self._login_status(new_password)
        if status != 200 or not cookie:
            raise AssertionError(f"the replacement admin password returned HTTP {status}")
        self.session_cookie = cookie
        self._assert_root_sentinel(present=False)
        self._check_live_host()
        self._write_root_sentinel()
        self._ok("reconfigure rotated both operator credentials and preserved durable state")

        self._step("recover after direct VM loss")
        self._close_tunnel()
        self._delete_instance_directly()
        if self._instance_record() is not None:
            raise AssertionError("direct instance deletion left the Lima record present")
        self._assert_disks_exist(detached=True)
        self.result = self._lifecycle("recover")
        self._reset_operator_transport()
        self._assert_result(self.result, expected_state="running")
        self._assert_root_sentinel(present=False)
        self._check_live_host()
        self._check_network_enforcement()
        self._ok("recover recreated compute from the preserved admin and agent disks")

        print(f"\n{self.passed}/{self.total} Lima end-to-end checks passed", flush=True)

    @property
    def expected_agent_name(self) -> str:
        return self.agent_name

    def teardown(self) -> None:
        self._close_tunnel()
        self._remove_owned_resources()

    def diagnostics(self) -> None:
        print("\nLima diagnostics before teardown:", file=sys.stderr, flush=True)
        if shutil.which("limactl") is None:
            print("limactl is not installed; no Lima diagnostics are available", file=sys.stderr)
            return
        for args in (("list", "--json"), ("disk", "list", "--json")):
            proc = self._run(
                ["limactl", *args],
                check=False,
                timeout=60,
                capture=True,
            )
            print(f"$ limactl {' '.join(args)}", file=sys.stderr)
            print(proc.stdout or "<no stdout>", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr)
        for path in sorted(self.lima_home.rglob("*.log")):
            try:
                lines = path.read_text(errors="replace").splitlines()[-120:]
            except OSError:
                continue
            print(f"\n--- {path} (last {len(lines)} lines) ---", file=sys.stderr)
            print("\n".join(lines), file=sys.stderr)

    def _require_tools(self) -> None:
        missing = [
            tool
            for tool in ("limactl", "qemu-system-x86_64", "ssh", "ssh-keygen")
            if shutil.which(tool) is None
        ]
        if missing:
            raise RuntimeError(f"Lima smoke requires these tools on PATH: {', '.join(missing)}")
        if sys.platform == "linux":
            kvm = Path("/dev/kvm")
            if not kvm.exists() or not os.access(kvm, os.R_OK | os.W_OK):
                raise RuntimeError("Lima smoke requires readable and writable /dev/kvm")
        version = self._run(
            ["limactl", "--version"], timeout=60, capture=True
        ).stdout.strip()
        print(f"Using {version}; isolated LIMA_HOME={self.lima_home}", flush=True)

    def _generate_key(self, path: Path | None = None) -> None:
        path = path or self.private_key
        self._run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-q",
                "-C",
                self.agent_name,
                "-f",
                str(path),
            ],
            timeout=60,
        )

    def _public_key(self) -> str:
        return self.private_key.with_suffix(".pub").read_text().strip()

    def _lifecycle(self, module: str, *extra: str) -> dict[str, Any]:
        proc = self._run(
            [
                sys.executable,
                "-m",
                f"host.cli.{module}",
                "--provider",
                "lima",
                "--agent-name",
                self.agent_name,
                *extra,
            ],
            check=False,
            timeout=LIFECYCLE_TIMEOUT_SECONDS,
            capture=True,
        )
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode,
                proc.args,
                output=proc.stdout,
                stderr=proc.stderr,
            )
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"{module} did not return one JSON result: {proc.stdout!r}"
            ) from exc
        if not isinstance(result, dict):
            raise AssertionError(f"{module} returned a non-object result: {result!r}")
        return result

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: int,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=REPO_ROOT,
            env=self.env,
            check=check,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            timeout=timeout,
        )

    def _limactl_records(self, *args: str) -> list[dict[str, Any]]:
        output = self._run(
            ["limactl", *args, "--json"],
            timeout=60,
            capture=True,
        ).stdout
        records: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise AssertionError(f"limactl returned a non-object record: {value!r}")
            records.append(value)
        return records

    @staticmethod
    def _field(record: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in record:
                return record[name]
        return None

    def _instance_record(self) -> dict[str, Any] | None:
        matches = [
            record
            for record in self._limactl_records("list")
            if self._field(record, "name", "Name") == self.instance_name
        ]
        if len(matches) > 1:
            raise AssertionError(f"duplicate exact-name Lima instances: {matches}")
        return matches[0] if matches else None

    def _disk_records(self) -> dict[str, dict[str, Any]]:
        wanted = set(self.disk_names.values())
        records = {
            str(self._field(record, "name", "Name")): record
            for record in self._limactl_records("disk", "list")
            if self._field(record, "name", "Name") in wanted
        }
        return records

    def _assert_disks_exist(self, *, detached: bool = False) -> None:
        records = self._disk_records()
        if set(records) != set(self.disk_names.values()):
            raise AssertionError(f"expected both owned Lima disks, found {sorted(records)}")
        if detached:
            attached = {
                name: self._field(record, "instance", "Instance")
                for name, record in records.items()
                if self._field(record, "instance", "Instance")
            }
            if attached:
                raise AssertionError(f"owned disks remained attached after VM deletion: {attached}")
        if self.disk_identities is not None:
            observed = self._disk_identities(records)
            if observed != self.disk_identities:
                raise AssertionError(
                    "a durable Lima disk was replaced instead of preserved: "
                    f"expected {self.disk_identities}, observed {observed}"
                )

    def _disk_identities(
        self, records: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, tuple[int, int, int, int]]:
        records = records or self._disk_records()
        identities: dict[str, tuple[int, int, int, int]] = {}
        for name in self.disk_names.values():
            record = records.get(name)
            if record is None:
                raise AssertionError(f"cannot identify missing Lima disk {name}")
            directory = self._field(record, "dir", "Dir")
            if not isinstance(directory, str) or not directory:
                raise AssertionError(f"Lima disk has no directory in list output: {record}")
            directory_stat = Path(directory).stat()
            data_stat = (Path(directory) / "datadisk").stat()
            identities[name] = (
                directory_stat.st_dev,
                directory_stat.st_ino,
                data_stat.st_dev,
                data_stat.st_ino,
            )
        return identities

    def _remember_disk_identities(self) -> None:
        self.disk_identities = self._disk_identities()

    def _check_definition_tamper_power_contract(self) -> None:
        record = self._instance_record()
        if record is None:
            raise AssertionError("expected a running instance before the tamper check")
        directory = self._field(record, "dir", "Dir")
        if not isinstance(directory, str) or not directory:
            raise AssertionError(f"Lima instance has no directory in list output: {record}")
        path = Path(directory) / "lima.yaml"
        original = path.read_bytes()
        path.write_bytes(original + b"\n# smoke tamper\n")
        try:
            # Stopping must remain available as the safe response to a
            # tampered or otherwise untrusted definition. Starting is the
            # dangerous operation: Lima boots the current lima.yaml, so Kern
            # must refuse it until the exact generated definition is restored.
            stopped = self._lifecycle("stop")
            self._assert_result(stopped, expected_state="stopped", expect_version=False)
            proc = self._run(
                [
                    sys.executable,
                    "-m",
                    "host.cli.start",
                    "--provider",
                    "lima",
                    "--agent-name",
                    self.agent_name,
                ],
                check=False,
                timeout=120,
                capture=True,
            )
            if proc.returncode != 2 or proc.stdout:
                raise AssertionError(
                    "a tampered Lima definition did not fail closed before power start: "
                    f"exit={proc.returncode}, stdout={proc.stdout!r}, stderr={proc.stderr!r}"
                )
            current = self._instance_record()
            if current is None or self._field(current, "status", "Status") != "Stopped":
                raise AssertionError("definition tampering started or replaced the stopped VM")
        finally:
            path.write_bytes(original)
        restarted = self._lifecycle("start")
        self._assert_result(restarted, expected_state="running", expect_version=False)
        # Lima may allocate a different loopback SSH port after a stop/start;
        # every later probe must use the latest authoritative result.
        self.result = restarted

    def _definition_signature(self) -> str:
        record = self._instance_record()
        if record is None:
            raise AssertionError("expected the owned Lima instance to exist")
        directory = self._field(record, "dir", "Dir")
        if not isinstance(directory, str) or not directory:
            raise AssertionError(f"Lima instance has no directory in list output: {record}")
        definition = (Path(directory) / "lima.yaml").read_bytes()
        return hashlib.sha256(definition).hexdigest()

    def _definition_text(self) -> str:
        record = self._instance_record()
        if record is None:
            raise AssertionError("expected the owned Lima instance to exist")
        directory = self._field(record, "dir", "Dir")
        if not isinstance(directory, str) or not directory:
            raise AssertionError(f"Lima instance has no directory in list output: {record}")
        return (Path(directory) / "lima.yaml").read_text()

    def _check_definition_contract(self) -> None:
        definition = self._definition_text()
        required = (
            "plain: true",
            "mounts: []",
            'cpus: 2',
            'memory: "2GiB"',
            'disk: "16GiB"',
            "containerd:\n  system: false\n  user: false",
            f'- name: "{self.disk_names["admin"]}"',
            f'- name: "{self.disk_names["agent"]}"',
        )
        missing = [fragment for fragment in required if fragment not in definition]
        if missing:
            raise AssertionError(f"stored definition is missing security/shape fields: {missing}")
        forbidden = (
            "portForwards:",
            self.admin_password,
            hashlib.sha256(self.admin_password.encode()).hexdigest(),
            self._public_key(),
            str(REPO_ROOT),
        )
        present = [fragment for fragment in forbidden if fragment in definition]
        if present:
            raise AssertionError("stored definition contains a forward, secret, key, or host path")

    def _assert_result(
        self,
        result: dict[str, Any],
        *,
        expected_state: str,
        expect_version: bool = True,
    ) -> None:
        expected_storage = {
            role: {"id": disk_name}
            for role, disk_name in self.disk_names.items()
        }
        if result.get("provider") != "lima" or result.get("agent_name") != self.agent_name:
            raise AssertionError(f"wrong provider or agent in lifecycle result: {result}")
        if result.get("host") != {"id": self.instance_name, "state": expected_state}:
            raise AssertionError(f"wrong host result: {result.get('host')}")
        if result.get("storage") != expected_storage:
            raise AssertionError(f"wrong storage result: {result.get('storage')}")
        ssh = result.get("ssh")
        if not isinstance(ssh, dict) or ssh.get("host") != "127.0.0.1":
            raise AssertionError(f"wrong loopback SSH result: {ssh}")
        port = ssh.get("port")
        if expected_state == "stopped":
            if port is not None:
                raise AssertionError(f"stopped Lima host retained a live SSH port: {ssh}")
        elif not isinstance(port, int) or port <= 0:
            raise AssertionError(f"invalid Lima SSH port: {ssh}")
        if ssh.get("user") != "kern-operator":
            raise AssertionError(f"wrong Lima SSH user: {ssh}")
        if expect_version and result.get("version") != repo_version():
            raise AssertionError(f"wrong installed version: {result.get('version')!r}")
        if not expect_version and "version" in result:
            raise AssertionError(f"power result unexpectedly contains version: {result}")
        if result.get("admin_ui_local_url") != f"http://127.0.0.1:{ADMIN_API_PORT}":
            raise AssertionError(f"wrong admin tunnel URL: {result.get('admin_ui_local_url')!r}")
        serialized = json.dumps(result, sort_keys=True)
        for secret_value in (
            self.admin_password,
            hashlib.sha256(self.admin_password.encode()).hexdigest(),
            self._public_key(),
        ):
            if secret_value in serialized:
                raise AssertionError("lifecycle result exposed a password digest or SSH key")
        self._assert_disks_exist()

    def _reset_operator_transport(self) -> None:
        self._close_tunnel()
        self.known_hosts.unlink(missing_ok=True)
        self.session_cookie = None

    def _assert_admin_not_directly_forwarded(self) -> None:
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", ADMIN_API_PORT), timeout=1):
                    pass
            except OSError:
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"guest admin port {ADMIN_API_PORT} is reachable without the SSH tunnel"
                )
            time.sleep(1)

    def _ssh_args(self) -> list[str]:
        ssh = self.result.get("ssh")
        if not isinstance(ssh, dict) or not isinstance(ssh.get("port"), int):
            raise AssertionError(f"no usable SSH endpoint in result: {self.result}")
        return [
            "ssh",
            "-i",
            str(self.private_key),
            "-p",
            str(ssh["port"]),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "kern-operator@127.0.0.1",
        ]

    def _probe_ssh_key(self, key: Path) -> bool:
        ssh = self.result.get("ssh")
        if not isinstance(ssh, dict) or not isinstance(ssh.get("port"), int):
            raise AssertionError(f"no usable SSH endpoint in result: {self.result}")
        proc = self._run(
            [
                "ssh",
                "-i",
                str(key),
                "-p",
                str(ssh["port"]),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ConnectTimeout=15",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "kern-operator@127.0.0.1",
                "true",
            ],
            check=False,
            timeout=30,
            capture=True,
        )
        return proc.returncode == 0

    def _wait_for_ssh(self) -> None:
        deadline = time.monotonic() + SSH_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            proc = self._run(
                [*self._ssh_args(), "true"],
                check=False,
                timeout=30,
                capture=True,
            )
            if proc.returncode == 0:
                return
            last = proc.stderr.strip()
            time.sleep(5)
        raise AssertionError(f"operator SSH never became ready: {last}")

    def _ssh(self, command: str, *, timeout: int = 120) -> str:
        self._wait_for_ssh()
        return self._run(
            [*self._ssh_args(), command],
            timeout=timeout,
            capture=True,
        ).stdout

    def _open_tunnel(self) -> None:
        if self.tunnel_open:
            return
        self._wait_for_ssh()
        self._run(
            [
                *self._ssh_args()[:-1],
                "-fN",
                "-M",
                "-S",
                str(self.control_socket),
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=8",
                "-L",
                f"{ADMIN_API_PORT}:127.0.0.1:{ADMIN_API_PORT}",
                self._ssh_args()[-1],
            ],
            timeout=60,
        )
        self.tunnel_open = True

    def _close_tunnel(self) -> None:
        if not self.tunnel_open:
            return
        self._run(
            [
                "ssh",
                "-S",
                str(self.control_socket),
                "-O",
                "exit",
                "kern-operator@127.0.0.1",
            ],
            check=False,
            timeout=30,
            capture=True,
        )
        self.tunnel_open = False
        self.session_cookie = None
        self.control_socket.unlink(missing_ok=True)

    def _login_status(self, password: str) -> tuple[int, str | None]:
        payload = json.dumps({"password": password}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{ADMIN_API_PORT}/v1/login",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, None
        with response:
            response.read()
            cookie = http.cookies.SimpleCookie()
            for header in response.headers.get_all("Set-Cookie") or []:
                cookie.load(header)
            morsel = cookie.get("tc_admin_session")
            return response.status, morsel.value if morsel is not None else None

    def _login(self) -> str:
        status, cookie = self._login_status(self.admin_password)
        if status != 200 or not cookie:
            raise AssertionError(f"admin login returned HTTP {status} without a session cookie")
        return cookie

    def _admin_cookie(self) -> str:
        if self.session_cookie is None:
            self.session_cookie = self._login()
        return self.session_cookie

    def _reopen_tunnel(self) -> None:
        self._close_tunnel()
        self._open_tunnel()

    def _ssh_code(self, remote_command: str) -> str:
        return self._ssh(remote_command)

    def _api(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None

        def attempt() -> dict[str, Any]:
            request = urllib.request.Request(
                f"http://127.0.0.1:{ADMIN_API_PORT}{path}",
                data=data,
                method=method,
            )
            request.add_header(
                "Cookie", f"tc_admin_session={self._admin_cookie()}"
            )
            request.add_header("X-Kern-Csrf", "1")
            if body is not None:
                request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.loads(response.read())
            if not isinstance(value, dict):
                raise AssertionError(
                    f"admin API {path} returned a non-object: {value!r}"
                )
            return value

        try:
            return attempt()
        except (urllib.error.URLError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                payload = exc.read()
                try:
                    detail: object = json.loads(payload)
                except json.JSONDecodeError:
                    detail = payload.decode(errors="replace")
                raise AssertionError(
                    f"{method} {path} returned HTTP {exc.code}: {detail}"
                ) from exc
            print(
                f"  (admin API unreachable: {exc}; reopening tunnel and retrying)",
                flush=True,
            )
            self._reopen_tunnel()
            return attempt()

    def _api_status(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        cookie: str | None = "__default__",
    ) -> tuple[int, dict[str, Any]]:
        if cookie == "__default__":
            cookie = self._admin_cookie()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{ADMIN_API_PORT}{path}",
            data=data,
            method=method,
        )
        if cookie is not None:
            request.add_header("Cookie", f"tc_admin_session={cookie}")
            request.add_header("X-Kern-Csrf", "1")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"raw": payload.decode(errors="replace")}

    def _wait_for_health(self) -> dict[str, Any]:
        self._open_tunnel()
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        last: object = None
        while time.monotonic() < deadline:
            try:
                health = self._api("GET", "/v1/health")
                last = health
                if health.get("network_controls", {}).get("status") == "active":
                    return health
            except (
                OSError,
                AssertionError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                self.session_cookie = None
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(5)
        raise AssertionError(f"admin health never became active: {last}")

    def _check_live_host(self) -> None:
        health = self._wait_for_health()
        mounts = health.get("host_runtime", {}).get("filesystem", {}).get("mounts", {})
        for name in ("root", "admin", "agent"):
            record = mounts.get(name) if isinstance(mounts, dict) else None
            if not isinstance(record, dict) or record.get("total_bytes", 0) <= 0:
                raise AssertionError(f"health is missing filesystem mount {name}: {mounts}")
        request = urllib.request.Request(f"http://127.0.0.1:{ADMIN_API_PORT}/")
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode(errors="replace")
        if "Kern" not in page:
            raise AssertionError("admin UI root does not look like Kern")
        if self._api_status("GET", "/v1/agent-runtime/status", cookie=None)[0] != 401:
            raise AssertionError("authenticated admin API accepted a request without a cookie")
        if self._api_status(
            "GET", "/v1/agent-runtime/status", cookie="wrong-session"
        )[0] != 401:
            raise AssertionError("authenticated admin API accepted a wrong session cookie")
        self._api("GET", "/v1/agent-runtime/status")

        found = self._ssh(f"sudo cat {shlex.quote(SENTINEL_PATH)}").strip()
        if found != self.sentinel:
            raise AssertionError(f"agent-volume sentinel changed: {found!r}")
        identity = self._ssh(
            "sudo -u postgres psql -tA -d kern_admin "
            + shlex.quote("-c")
            + " "
            + shlex.quote("SELECT agent_name FROM public.config LIMIT 1")
        ).strip()
        if identity != self.agent_name:
            raise AssertionError(f"durable database identity is {identity!r}")

        services = self._ssh(
            "systemctl is-active "
            "kern-postgres.service kern-network-proxy.service kern-tools.service "
            "kern-agent-network.service kern-admin-api.service "
            "kern-host-errors.service kern-workspace.service"
        ).splitlines()
        if services != ["active"] * 7:
            raise AssertionError(f"one or more core systemd services are not active: {services}")
        cloudflared = self._ssh(
            "systemctl is-enabled kern-cloudflared.service 2>/dev/null || true"
        ).strip()
        if cloudflared == "enabled":
            raise AssertionError("SSH-only deploy unexpectedly enabled cloudflared")
        ruleset = self._ssh("sudo nft list ruleset")
        if "table inet kern" not in ruleset or f"tcp dport {PROXY_PORT}" not in ruleset:
            raise AssertionError("installed nftables rules do not contain Kern uid boundaries")

        labels = self._ssh(
            "for mountpoint in /mnt/kern-admin /mnt/kern-agent; do "
            "device=$(findmnt -n -o SOURCE \"$mountpoint\"); "
            "printf '%s=%s\\n' \"$mountpoint\" \"$(sudo blkid -s LABEL -o value \"$device\")\"; "
            "done"
        ).splitlines()
        if labels != ["/mnt/kern-admin=KERN_ADMIN", "/mnt/kern-agent=KERN_AGENT"]:
            raise AssertionError(f"durable filesystem labels are wrong: {labels}")

        mount_types = self._ssh("findmnt -rn -o FSTYPE,TARGET || true")
        forbidden = [
            line
            for line in mount_types.splitlines()
            if line.split(maxsplit=1)[0] in {"9p", "virtiofs", "fuse.lima"}
        ]
        if forbidden:
            raise AssertionError(f"host filesystem mounts are visible in the guest: {forbidden}")

        version_payload = json.loads(
            self._ssh("sudo cat /mnt/kern-admin/admin-state/version.json")
        )
        if version_payload.get("version") != repo_version():
            raise AssertionError(f"admin version metadata was not converged: {version_payload}")
        self._check_workspace_state()
        self._check_network_policy_state()

    def _seed_workspace_state(self) -> None:
        if self._api("GET", "/v1/workspace/memory").get("pages") != []:
            raise AssertionError("fresh Lima host did not start with empty swarm memory")
        page = self._api(
            "PUT",
            f"/v1/workspace/memory/pages/{quote(self.memory_page_id, safe='')}",
            {
                "description": "Lima smoke durable page",
                "content": self.sentinel,
                "expected_revision": 0,
            },
        ).get("page")
        if not isinstance(page, dict) or page.get("revision") != 1:
            raise AssertionError(f"could not create durable smoke memory: {page}")

        created = self._api("POST", "/v1/workspace/web-apps/apps").get("app")
        if not isinstance(created, dict) or not isinstance(created.get("app_id"), str):
            raise AssertionError(f"could not create durable smoke app: {created}")
        self.app_id = created["app_id"]
        renamed = self._api(
            "PUT",
            f"/v1/workspace/web-apps/apps/{quote(self.app_id, safe='')}/name",
            {"name": "Lima durable smoke app"},
        ).get("app")
        if not isinstance(renamed, dict) or renamed.get("name") != "Lima durable smoke app":
            raise AssertionError(f"could not rename durable smoke app: {renamed}")

    def _check_workspace_state(self) -> None:
        page = self._api(
            "GET", f"/v1/workspace/memory/pages/{quote(self.memory_page_id, safe='')}"
        ).get("page")
        if not isinstance(page, dict) or page.get("content") != self.sentinel:
            raise AssertionError(f"durable swarm-memory page was not preserved: {page}")
        if self.app_id is None:
            raise AssertionError("durable smoke app was never created")
        apps = self._api("GET", "/v1/workspace/web-apps/apps").get("apps")
        if not any(
            isinstance(app, dict)
            and app.get("app_id") == self.app_id
            and app.get("name") == "Lima durable smoke app"
            for app in (apps or [])
        ):
            raise AssertionError(f"durable smoke app was not preserved: {apps}")
        state = self._api(
            "GET", f"/v1/workspace/web-apps/apps/{quote(self.app_id, safe='')}/state"
        ).get("app")
        if not isinstance(state, dict) or state.get("revision") != 0 or state.get("data") != {}:
            raise AssertionError(f"durable smoke app state is invalid: {state}")

    def _configure_network_policy(self) -> None:
        self.network_policy = {
            "network_integrations": {
                "custom": {
                    "domains": {
                        "example.com": {
                            "allow_http_methods": ["GET"],
                            "path_guards": ["^/$"],
                        }
                    }
                }
            }
        }
        self._api(
            "PUT",
            "/v1/network/policy",
            self.network_policy,
        )
        self._check_network_policy_state()

    def _check_network_policy_state(self) -> None:
        policy = getattr(self, "network_policy", None)
        if policy is None:
            return
        stored = self._api("GET", "/v1/network/policy").get("network_controls")
        expected_integrations = policy["network_integrations"]
        if (
            not isinstance(stored, dict)
            or stored.get("network_integrations") != expected_integrations
        ):
            raise AssertionError(f"network policy did not survive lifecycle operation: {stored}")

    def _check_network_enforcement(self) -> None:
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        prefix = (
            "sudo -u kern-agent env -u HTTP_PROXY -u HTTPS_PROXY "
            "-u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy"
        )
        allowed = self._ssh(
            f"{prefix} HTTPS_PROXY={proxy} curl -s -o /dev/null "
            "-w '%{http_code}' --max-time 30 https://example.com/"
        ).strip()
        denied = self._ssh(
            f"{prefix} HTTPS_PROXY={proxy} curl -s -o /dev/null "
            "-w '%{http_code}' --max-time 30 https://example.com/denied || true"
        ).strip()
        direct = self._ssh(
            f"{prefix} curl -s -o /dev/null -w '%{{http_code}}' "
            "--connect-timeout 3 --max-time 12 https://example.com/ || true"
        ).strip()
        loopback_admin = self._ssh(
            f"{prefix} curl -s -o /dev/null -w '%{{http_code}}' "
            f"--connect-timeout 2 --max-time 5 http://127.0.0.1:{ADMIN_API_PORT}/v1/health || true"
        ).strip()
        if allowed != "200":
            raise AssertionError(f"allowed request through the Kern proxy returned {allowed!r}")
        if denied != "403":
            raise AssertionError(f"denied path through the Kern proxy returned {denied!r}")
        if direct == "200":
            raise AssertionError("kern-agent reached the internet without the proxy")
        if loopback_admin not in {"", "000"}:
            raise AssertionError(
                f"kern-agent reached the admin listener directly: {loopback_admin!r}"
            )
        events = self._api("GET", "/v1/network/events?limit=100").get("events")
        decisions = {
            event.get("decision")
            for event in (events or [])
            if isinstance(event, dict)
        }
        if not {"allowed", "denied"} <= decisions:
            raise AssertionError(f"network audit log lacks allowed/denied decisions: {events}")

    def _write_sentinel(self) -> None:
        command = (
            f"printf %s {shlex.quote(self.sentinel)} | "
            f"sudo tee {shlex.quote(SENTINEL_PATH)} >/dev/null && "
            f"sudo chown kern-agent:kern-agent {shlex.quote(SENTINEL_PATH)} && "
            f"sudo chmod 600 {shlex.quote(SENTINEL_PATH)}"
        )
        self._ssh(command)

    def _write_root_sentinel(self) -> None:
        self._ssh(
            f"printf %s {shlex.quote(self.root_sentinel)} | "
            "sudo tee /var/tmp/kern-lima-root-sentinel >/dev/null"
        )

    def _assert_root_sentinel(self, *, present: bool) -> None:
        output = self._ssh(
            "sudo cat /var/tmp/kern-lima-root-sentinel 2>/dev/null || true"
        ).strip()
        if present and output != self.root_sentinel:
            raise AssertionError(f"disposable-root sentinel disappeared: {output!r}")
        if not present and output:
            raise AssertionError("disposable-root sentinel survived a VM replacement")

    def _write_older_version_metadata(self) -> None:
        major, minor, patch = parse_version(repo_version())
        if patch > 0:
            older = f"{major}.{minor}.{patch - 1}"
        elif minor > 0:
            older = f"{major}.{minor - 1}.0"
        elif major > 0:
            older = f"{major - 1}.0.0"
        else:
            raise AssertionError("cannot derive an older version than 0.0.0")
        # Simulate a host created by that older release. The definition hint
        # is advisory preflight; the durable admin metadata remains the
        # authoritative bootstrap gate.
        record = self._instance_record()
        if record is None:
            raise AssertionError("cannot age version metadata without a Lima instance")
        directory = self._field(record, "dir", "Dir")
        if not isinstance(directory, str) or not directory:
            raise AssertionError(f"Lima instance has no directory in list output: {record}")
        definition_path = Path(directory) / "lima.yaml"
        definition = definition_path.read_text()
        current_hint = f"# kern-version: {repo_version()}"
        if definition.count(current_hint) != 1:
            raise AssertionError("stored Lima definition lacks the current version hint")
        definition_path.write_text(
            definition.replace(current_hint, f"# kern-version: {older}")
        )
        script = (
            "import json, pathlib; "
            "path = pathlib.Path('/mnt/kern-admin/admin-state/version.json'); "
            f"path.write_text(json.dumps({{'version': {older!r}}}) + '\\n')"
        )
        self._ssh(f"sudo python3 -c {shlex.quote(script)}")
        payload = json.loads(self._ssh("sudo cat /mnt/kern-admin/admin-state/version.json"))
        if payload != {"version": older}:
            raise AssertionError(f"failed to create older version metadata: {payload}")

    def _delete_instance_directly(self) -> None:
        self._run(
            ["limactl", "stop", self.instance_name],
            check=False,
            timeout=600,
            capture=True,
        )
        self._run(
            ["limactl", "delete", "--force", "--tty=false", self.instance_name],
            timeout=600,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._instance_record() is None:
                records = self._disk_records()
                if len(records) == 2 and all(
                    not self._field(record, "instance", "Instance")
                    for record in records.values()
                ):
                    return
            time.sleep(2)
        raise AssertionError("direct VM deletion did not detach both durable disks")

    def _remove_owned_resources(self) -> None:
        if shutil.which("limactl") is None:
            return
        try:
            record = self._instance_record()
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            record = None
        if record is not None:
            self._run(
                ["limactl", "stop", self.instance_name],
                check=False,
                timeout=600,
                capture=True,
            )
            self._run(
                ["limactl", "delete", "--force", "--tty=false", self.instance_name],
                check=False,
                timeout=600,
                capture=True,
            )
        for disk_name in self.disk_names.values():
            self._run(
                ["limactl", "disk", "delete", "--force", disk_name],
                check=False,
                timeout=300,
                capture=True,
            )
        if self._instance_record() is not None or self._disk_records():
            raise AssertionError("exact-name Lima smoke resources remain after teardown")

    def _step(self, label: str) -> None:
        self.total += 1
        print(f"\n[{self.total}] {label}", flush=True)

    def _ok(self, detail: str) -> None:
        self.passed += 1
        print(f"  PASS: {detail}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)
    # Lima embeds the absolute home in Unix socket paths, so keep this
    # disposable prefix short while retaining an isolated per-run directory.
    with tempfile.TemporaryDirectory(prefix=SMOKE_WORKDIR_PREFIX) as directory:
        smoke = LimaSmoke(Path(directory))
        failed = False
        try:
            smoke.run()
        except BaseException:
            failed = True
            smoke.diagnostics()
            raise
        finally:
            try:
                smoke.teardown()
            except BaseException as exc:
                if not failed:
                    raise
                print(f"warning: Lima smoke teardown also failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
