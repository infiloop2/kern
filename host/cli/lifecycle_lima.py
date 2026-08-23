"""Lima local-VM operations for Kern host lifecycle commands.

This module is the ``lima`` counterpart of ``host.cli.lifecycle_aws``: it owns
compute (one dedicated Lima VM), durable block storage (two standalone Lima
data disks), power, discovery, and the loopback SSH endpoint. Everything after
the guest's data devices are identified is the same shared bootstrap and Kern
runtime that AWS hosts run; the design and its invariants are documented in
``docs/architecture/host-provider-design.md``.

Contracts this module maintains:

- Resources use deterministic exact names derived from the agent name, so
  discovery needs no registry file and never selects by prefix or substring.
- Discovery fails closed on malformed ``limactl`` JSON, unknown states, and
  disks attached to a foreign instance.
- The generated VM definition runs Lima in plain mode with no host filesystem
  mounts and no forwarded port other than Lima's own loopback SSH. It contains
  no lifecycle secret; the provisioning payload travels over SSH stdin.
- Compute is disposable and durable disks are not: no code path here invokes
  ``limactl disk delete``, and failed provisioning deletes only the instance
  it created while preserving both data disks.
- All subprocesses use argument arrays with bounded timeouts; resource names
  are re-validated before every mutating call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

from host.bootstrap.render import (
    _bootstrap_payload,
    _render_ssh_user_data,
    _write_runtime_code_archive,
)
from host.config import (
    AGENT_NAME_RE,
    ConfigError,
    RuntimeOperatorConnection,
    build_operator_connections,
    public_operator_connections,
)
from host.constants import ADMIN_API_PORT, OPERATOR_TUNNEL_TOKEN_ENV_NAME
from host.cli.lifecycle_bootstrap import (
    CODE_ARCHIVE_NAME,
    _generate_deploy_key,
    _provision_over_ssh,
)
from host.cli.lifecycle_constants import (
    ADMIN_VOLUME_SIZE_GB,
    AGENT_VOLUME_SIZE_GB,
    ROOT_VOLUME_SIZE_GB,
    SSH_USER,
)
from host.cli.lifecycle_logging import _log
from host.cli.lifecycle_types import LifecycleCommand
from host.cli.operation_lock import OperationLock
from host.version import compare_versions, repo_version

# Tested limactl range: additional named disks, plain mode, and the JSON list
# schemas this module consumes are stable across it. Outside the range,
# preflight fails with an upgrade instruction instead of parsing unknown
# output.
LIMA_MIN_VERSION = (1, 0, 0)
LIMA_MAX_VERSION_EXCLUSIVE = (3, 0, 0)
# The local shape is pinned like the AWS adapter's instance type. Keeping it
# here makes resource sizing an adapter concern rather than a shared-CLI knob.
LIMA_VM_CPUS = 2
LIMA_VM_MEMORY_GIB = 2

# The stable channel of the same Ubuntu release the AWS provider resolves
# through SSM ("current" AMI): both providers track Canonical's latest 22.04
# image rather than a byte-pinned build.
_UBUNTU_IMAGES = (
    ("https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64.img", "x86_64"),
    ("https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-arm64.img", "aarch64"),
)

# Deterministic, collision-resistant resource names (see the design doc):
# normalization keeps them Lima-safe, the digest keeps names that normalize to
# the same spelling distinct.
_LOCAL_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{24}$")
_INSTANCE_NAME_RE = re.compile(r"^kern-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{24}$")
_DISK_NAME_RE = re.compile(
    r"^kern-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{24}-(admin|agent)$"
)

_VERSION_OUTPUT_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_STORED_VERSION_RE = re.compile(r"^# kern-version: ([^\n]+)$", re.MULTILINE)

_LIST_TIMEOUT_SECONDS = 60
_VALIDATE_TIMEOUT_SECONDS = 60
_DISK_CREATE_TIMEOUT_SECONDS = 300
# Instance creation downloads the Ubuntu image on first use.
_CREATE_TIMEOUT_SECONDS = 3600
_START_TIMEOUT_SECONDS = 1800
_STOP_TIMEOUT_SECONDS = 600
_DELETE_TIMEOUT_SECONDS = 600
_STAGE_ONE_TIMEOUT_SECONDS = 300
_DETACH_WAIT_SECONDS = 120
_LIVE_PROGRESS_INTERVAL_SECONDS = 10


@dataclass(frozen=True)
class LimaConfig:
    agent_name: str


@dataclass(frozen=True)
class LimaInstance:
    name: str
    status: str
    ssh_local_port: int | None
    dir: str | None


@dataclass(frozen=True)
class LimaDisk:
    name: str
    # Exact name of the instance using the disk; empty when detached.
    instance: str


@dataclass(frozen=True)
class LimaInventory:
    instance: LimaInstance | None
    # role -> disk, for the roles whose deterministic disk name exists.
    disks: dict[str, LimaDisk]


def build_lima_config(command: LifecycleCommand) -> LimaConfig:
    agent_name = command.agent_name.strip()
    if not AGENT_NAME_RE.fullmatch(agent_name):
        raise ConfigError("agent name must be 1-50 characters of letters, numbers, '-' or '_'")
    return LimaConfig(agent_name=agent_name)


def _local_key(agent_name: str) -> str:
    normalized = re.sub(r"[-_]+", "-", agent_name.lower()).strip("-") or "agent"
    digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()[:24]
    prefix = normalized[:16].rstrip("-") or "agent"
    key = f"{prefix}-{digest}"
    if not _LOCAL_KEY_RE.fullmatch(key):
        raise ConfigError(f"could not derive a Lima-safe local key from agent name {agent_name!r}")
    return key


def _instance_name(agent_name: str) -> str:
    name = f"kern-{_local_key(agent_name)}"
    if not _INSTANCE_NAME_RE.fullmatch(name):
        raise ConfigError(f"derived Lima instance name {name!r} is not a safe exact name")
    return name


def _disk_name(agent_name: str, role: str) -> str:
    if role not in {"admin", "agent"}:
        raise ConfigError(f"unknown storage role {role!r}")
    name = f"{_instance_name(agent_name)}-{role}"
    if not _DISK_NAME_RE.fullmatch(name):
        raise ConfigError(f"derived Lima disk name {name!r} is not a safe exact name")
    return name


def _limactl(
    config: LimaConfig,
    *args: str,
    timeout: int,
    input_text: str | None = None,
    progress_message: str | None = None,
) -> str:
    # The parent environment is inherited unchanged, so an ambient LIMA_HOME
    # reaches limactl exactly as it would any direct limactl invocation.
    del config
    finished = threading.Event()
    progress_thread: threading.Thread | None = None
    if progress_message is not None:
        started = time.monotonic()

        def report_progress() -> None:
            while not finished.wait(_LIVE_PROGRESS_INTERVAL_SECONDS):
                elapsed = max(1, round(time.monotonic() - started))
                _log(f"{progress_message} ({elapsed}s elapsed)")

        progress_thread = threading.Thread(target=report_progress, daemon=True)
        progress_thread.start()
    try:
        proc = subprocess.run(
            ["limactl", *args],
            check=True,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    finally:
        finished.set()
        if progress_thread is not None:
            progress_thread.join()
    return proc.stdout


def _parse_json_lines(output: str, *, source: str) -> list[dict[str, Any]]:
    """``limactl ... --json`` emits one JSON object per line."""
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"could not parse {source} output as JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise ConfigError(f"{source} returned a non-object JSON record")
        records.append(record)
    return records


def _record_field(record: dict[str, Any], *names: str) -> Any:
    """Read one logical field that limactl has spelled differently across
    releases (e.g. ``sshLocalPort`` / ``SSHLocalPort``)."""
    for name in names:
        if name in record:
            return record[name]
    return None


def _preflight(config: LimaConfig) -> None:
    """Validate the limactl installation and JSON schemas without changing
    any resource."""
    if shutil.which("limactl") is None:
        raise ConfigError(
            "limactl was not found on PATH; install Lima >= "
            f"{'.'.join(str(part) for part in LIMA_MIN_VERSION)} "
            "(https://lima-vm.io) to deploy a local Kern host"
        )
    output = _limactl(config, "--version", timeout=_LIST_TIMEOUT_SECONDS)
    match = _VERSION_OUTPUT_RE.search(output)
    if match is None:
        raise ConfigError(f"could not parse limactl version from {output.strip()!r}")
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if not (LIMA_MIN_VERSION <= version < LIMA_MAX_VERSION_EXCLUSIVE):
        minimum = ".".join(str(part) for part in LIMA_MIN_VERSION)
        maximum = ".".join(str(part) for part in LIMA_MAX_VERSION_EXCLUSIVE)
        raise ConfigError(
            f"limactl {'.'.join(str(part) for part in version)} is outside Kern's tested range "
            f">= {minimum}, < {maximum}; install a supported Lima release"
        )
    # Exercise both list schemas read-only so an incompatible installation
    # fails here rather than mid-operation.
    _inspect(config)


def _inspect(config: LimaConfig) -> LimaInventory:
    """Return the managed instance and disks selected by exact deterministic
    name, failing closed on anything this module cannot validate."""
    instance_name = _instance_name(config.agent_name)
    instances = _parse_json_lines(
        _limactl(config, "list", "--format=json", timeout=_LIST_TIMEOUT_SECONDS),
        source="limactl list",
    )
    instance: LimaInstance | None = None
    for record in instances:
        name = _record_field(record, "name", "Name")
        if not isinstance(name, str):
            raise ConfigError("limactl list returned an instance without a name")
        if name != instance_name:
            continue
        if instance is not None:
            raise ConfigError(f"limactl list returned duplicate records for instance {instance_name}")
        status = _record_field(record, "status", "Status")
        if not isinstance(status, str) or not status:
            raise ConfigError(f"limactl list returned no status for instance {instance_name}")
        port_value = _record_field(record, "sshLocalPort", "SSHLocalPort")
        ssh_local_port: int | None = None
        if port_value is not None:
            if not isinstance(port_value, int) or isinstance(port_value, bool) or port_value < 0:
                raise ConfigError(f"limactl list returned an invalid SSH port for {instance_name}")
            ssh_local_port = port_value or None
        dir_value = _record_field(record, "dir", "Dir")
        instance_dir = dir_value if isinstance(dir_value, str) and dir_value else None
        instance = LimaInstance(
            name=name, status=status, ssh_local_port=ssh_local_port, dir=instance_dir
        )
    disks: dict[str, LimaDisk] = {}
    disk_records = _parse_json_lines(
        _limactl(config, "disk", "list", "--json", timeout=_LIST_TIMEOUT_SECONDS),
        source="limactl disk list",
    )
    for role in ("admin", "agent"):
        wanted = _disk_name(config.agent_name, role)
        matches = []
        for record in disk_records:
            name = _record_field(record, "name", "Name")
            if not isinstance(name, str):
                raise ConfigError("limactl disk list returned a disk without a name")
            if name == wanted:
                matches.append(record)
        if not matches:
            continue
        if len(matches) > 1:
            raise ConfigError(f"limactl disk list returned duplicate records for disk {wanted}")
        instance_value = _record_field(matches[0], "instance", "Instance")
        if instance_value is None:
            instance_value = ""
        if not isinstance(instance_value, str):
            raise ConfigError(f"limactl disk list returned an invalid instance field for {wanted}")
        if instance_value and instance_value != instance_name:
            raise ConfigError(
                f"Kern {role} disk {wanted} is attached to unexpected Lima instance "
                f"{instance_value}; detach it before running lifecycle commands"
            )
        disks[role] = LimaDisk(name=wanted, instance=instance_value)
    return LimaInventory(instance=instance, disks=disks)


def _validate_lima_preflight(command: LifecycleCommand, inventory: LimaInventory) -> None:
    """The shared operation matrix over Lima inventory; messages mirror the
    AWS preflight."""
    agent_name = command.agent_name
    if inventory.instance is None:
        stale_attachments = sorted(
            disk.name for disk in inventory.disks.values() if disk.instance
        )
        if stale_attachments:
            raise ConfigError(
                "Kern Lima data disk(s) still report attachment to an absent instance: "
                f"{', '.join(stale_attachments)}; wait for detachment or detach them before "
                f"running {command.mode}"
            )
    if inventory.instance is not None and inventory.instance.status not in {
        "Running",
        "Stopped",
    }:
        raise ConfigError(
            f"Kern Lima instance {inventory.instance.name} has unsupported state "
            f"{inventory.instance.status!r}; resolve it with limactl before running "
            "lifecycle commands"
        )
    roles = set(inventory.disks)
    if command.mode == "deploy":
        if inventory.instance is not None:
            raise ConfigError(
                f"deploy requires no existing Kern Lima instance for {agent_name}; "
                "use upgrade or recover for preserved hosts"
            )
        if roles:
            disk_names = sorted(_disk_name(agent_name, role) for role in roles)
            delete_commands = "; ".join(
                f"limactl disk delete {disk_name}" for disk_name in disk_names
            )
            raise ConfigError(
                f"deploy requires no existing Kern Lima data disks for {agent_name}; "
                f"found {', '.join(disk_names)}. To preserve an existing host, run recover "
                "once both admin and agent disks are present. If these are unused disks from a "
                f"failed first deploy, delete exactly them with: {delete_commands}; then retry "
                "deploy."
            )
        return
    expected_roles = {"admin", "agent"}
    if roles != expected_roles:
        missing = ", ".join(sorted(expected_roles - roles)) or "none"
        found = ", ".join(sorted(roles)) or "none"
        raise ConfigError(
            f"{command.mode} requires existing admin and agent data disks for {agent_name}; "
            f"found {found}, missing {missing}"
        )
    if command.mode in {"upgrade", "reconfigure"} and inventory.instance is None:
        raise ConfigError(
            f"{command.mode} requires an existing Kern Lima instance for {agent_name}; "
            "use recover to recreate a missing or broken host"
        )
    if command.mode == "recover" and inventory.instance is not None:
        raise ConfigError(
            f"recover requires no existing Kern Lima instance for {agent_name}; "
            "use upgrade for a normal release upgrade, or reconfigure to change admin password "
            "or operator access"
        )


# The system provision script is the only provider-specific code inside the
# guest, and it runs before the durable disks are mounted: it copies Lima's
# non-secret additional-disk name/device metadata to a root-only file that
# the shared bootstrap's storage resolution consumes. It never relies on
# attachment order such as "admin is always /dev/vdb".
_DISK_METADATA_PROVISION_SCRIPT = """\
#!/bin/bash
set -eu
umask 077
# Lima exports LIMA_CIDATA_DISK_<n>_NAME/_DEVICE to provisioning scripts;
# source the cidata environment file as a fallback for releases that do not.
# set -a exports every sourced assignment so the python3 child sees them.
if [ -z "${LIMA_CIDATA_DISKS:-}" ] && [ -f /mnt/lima-cidata/lima.env ]; then
  set +u
  set -a
  . /mnt/lima-cidata/lima.env
  set +a
  set -u
fi
mkdir -p /run/kern-provider
python3 - <<'EOF'
import json
import os
import re

disks = []
for index in range(0, 32):
    name = os.environ.get(f"LIMA_CIDATA_DISK_{index}_NAME")
    device = os.environ.get(f"LIMA_CIDATA_DISK_{index}_DEVICE")
    if name and device:
        # Lima's cidata contract records the virtio device basename (for
        # example "vdb"), not an absolute guest path. Accept only that exact
        # shape before placing it under /dev; never turn arbitrary metadata
        # into a filesystem path.
        if re.fullmatch(r"vd[a-z]", device) is None:
            raise SystemExit(f"invalid Lima additional-disk device {device!r}")
        disks.append({"name": name, "device": f"/dev/{device}"})
with open("/run/kern-provider/lima-disks.json", "w") as handle:
    json.dump({"disks": disks}, handle, indent=2, sort_keys=True)
EOF
chmod 600 /run/kern-provider/lima-disks.json
"""


def _render_vm_definition(config: LimaConfig) -> str:
    """Generate the entire Lima definition from trusted, validated fields.

    Plain mode disables Lima's host filesystem mounts (including the default
    host-home mount), dynamic port forwarding, containerd, and the guest
    agent while retaining SSH and system provisioning; ``mounts: []`` and the
    containerd stanza state the same guarantees explicitly. The definition
    carries no payload, token, key, or password: secrets travel only over SSH
    stdin after the VM is running."""
    admin_disk = _disk_name(config.agent_name, "admin")
    agent_disk = _disk_name(config.agent_name, "agent")
    provision_script = "\n".join(
        f"      {line}" if line else "" for line in _DISK_METADATA_PROVISION_SCRIPT.splitlines()
    )
    image_lines = "\n".join(
        f'- location: "{location}"\n  arch: "{arch}"' for location, arch in _UBUNTU_IMAGES
    )
    return f"""\
# Generated by Kern; do not edit. Lifecycle commands replace this instance.
# kern-version: {repo_version()}
minimumLimaVersion: "1.0.0"
plain: true
images:
{image_lines}
cpus: {LIMA_VM_CPUS}
memory: "{LIMA_VM_MEMORY_GIB}GiB"
disk: "{ROOT_VOLUME_SIZE_GB}GiB"
mounts: []
containerd:
  system: false
  user: false
additionalDisks:
- name: "{admin_disk}"
  format: false
- name: "{agent_disk}"
  format: false
provision:
- mode: system
  script: |
{provision_script}
"""


def _write_vm_definition(config: LimaConfig, workdir: Path) -> Path:
    definition_path = workdir / "kern-lima.yaml"
    definition_path.touch(mode=0o600)
    definition_path.chmod(0o600)
    definition_path.write_text(_render_vm_definition(config))
    return definition_path


def _validate_vm_definition(config: LimaConfig, definition_path: Path) -> None:
    """Ask Lima to resolve current-host defaults and validate the complete
    candidate definition without creating or changing an instance."""
    _limactl(
        config,
        "validate",
        "--fill",
        str(definition_path),
        timeout=_VALIDATE_TIMEOUT_SECONDS,
    )


def _ensure_storage_disks(
    config: LimaConfig,
    inventory: LimaInventory,
    *,
    create_missing: bool,
) -> dict[str, str]:
    """Create only missing disks; preflight already rejected partial pairs for
    every mode but deploy. Replacement modes fail closed if a preserved disk
    disappears after preflight. Returns role -> exact disk name."""
    disks: dict[str, str] = {}
    for role, size_gib in (("admin", ADMIN_VOLUME_SIZE_GB), ("agent", AGENT_VOLUME_SIZE_GB)):
        name = _disk_name(config.agent_name, role)
        if role in inventory.disks:
            _log(f"reusing {role} storage disk {name}")
        else:
            if not create_missing:
                raise ConfigError(
                    f"preserved {role} Lima data disk {name} disappeared during replacement; "
                    "refusing to create empty replacement storage"
                )
            _log(f"creating {role} storage disk {name} ({size_gib} GiB)")
            _limactl(
                config,
                "disk",
                "create",
                name,
                f"--size={size_gib}GiB",
                timeout=_DISK_CREATE_TIMEOUT_SECONDS,
                progress_message=f"still creating {role} storage disk {name}",
            )
        disks[role] = name
    return disks


def _wait_for_detached_disks(config: LimaConfig) -> None:
    deadline = time.monotonic() + _DETACH_WAIT_SECONDS
    while True:
        inventory = _inspect(config)
        attached = sorted(
            disk.name for disk in inventory.disks.values() if disk.instance
        )
        if not attached:
            return
        if time.monotonic() >= deadline:
            raise ConfigError(
                f"Kern data disks {', '.join(attached)} did not detach within "
                f"{_DETACH_WAIT_SECONDS} seconds"
            )
        time.sleep(2)


def _delete_instance(config: LimaConfig, instance_name: str) -> None:
    """Destroy only the disposable instance; standalone data disks survive
    instance deletion by Lima's contract. Idempotent when already absent.

    Kern owns the exact deterministic name while holding its operation lock.
    Mutating Kern-managed resources directly with ``limactl`` is unsupported.
    """
    if not _INSTANCE_NAME_RE.fullmatch(instance_name):
        raise ConfigError(f"refusing to delete unvalidated Lima instance name {instance_name!r}")
    if _inspect(config).instance is None:
        return
    _log(f"deleting Lima instance {instance_name}")
    try:
        _limactl(config, "stop", instance_name, timeout=_STOP_TIMEOUT_SECONDS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Already stopped or never started; delete --force settles it.
        pass
    if _inspect(config).instance is None:
        return
    try:
        _limactl(config, "delete", "--force", "--tty=false", instance_name, timeout=_DELETE_TIMEOUT_SECONDS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if _inspect(config).instance is not None:
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
            raise ConfigError(
                f"could not delete Lima instance {instance_name}: {stderr or exc}"
            ) from exc
    _wait_for_detached_disks(config)


def _cleanup_failed_instance(
    config: LimaConfig,
    instance_name: str,
) -> None:
    """Best-effort deletion of a failed launch, preserving both data disks."""
    try:
        _log(
            f"provisioning failed; deleting Lima instance {instance_name} and preserving "
            "both data disks"
        )
        _delete_instance(config, instance_name)
    except Exception as cleanup_exc:  # noqa: BLE001 — best-effort cleanup
        _log(f"warning: could not delete {instance_name}: {cleanup_exc}")


def _launched_instance(config: LimaConfig, instance_name: str) -> LimaInstance:
    inventory = _inspect(config)
    instance = inventory.instance
    if instance is None:
        raise ConfigError(f"Lima instance {instance_name} is missing after start")
    if instance.status != "Running":
        raise ConfigError(
            f"Lima instance {instance_name} is {instance.status!r} after start; expected Running"
        )
    if not instance.ssh_local_port:
        raise ConfigError(f"Lima instance {instance_name} reports no loopback SSH port")
    return instance


def _ssh_config_path(config: LimaConfig, instance: LimaInstance) -> Path:
    if instance.dir:
        return Path(instance.dir) / "ssh.config"
    ambient_lima_home = os.environ.get("LIMA_HOME")
    base = Path(ambient_lima_home) if ambient_lima_home else Path.home() / ".lima"
    return base / instance.name / "ssh.config"


def _deliver_stage_one(
    config: LimaConfig,
    instance: LimaInstance,
    stage_one_script: str,
) -> None:
    """Stream the shared stage-one script (kern-operator account, single-use
    deploy key, root-only payload) to root over Lima's generated management
    SSH configuration. The script rides stdin, never an argument or a file in
    the VM definition."""
    last_error = ""
    for attempt in range(5):
        if attempt:
            time.sleep(5)
        ssh_config = _ssh_config_path(config, instance)
        if not ssh_config.is_file():
            raise ConfigError(f"Lima SSH configuration {ssh_config} does not exist")
        command = [
            "ssh",
            "-F",
            str(ssh_config),
            "-o",
            "BatchMode=yes",
            f"lima-{instance.name}",
            "sudo",
            "bash",
            "-s",
        ]
        proc = subprocess.run(
            command,
            input=stage_one_script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_STAGE_ONE_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            return
        last_error = proc.stderr.strip()
    raise ConfigError(
        f"could not stage provisioning on Lima instance {instance.name}: {last_error}"
    )


def _launch(
    config: LimaConfig,
    definition_path: Path,
    stage_one_script: str,
) -> LimaInstance:
    """Create, start, and stage one disposable instance.

    On failure, delete the deterministic instance name and preserve the two
    separately managed data disks.
    """
    instance_name = _instance_name(config.agent_name)
    try:
        _log(
            f"creating Lima instance {instance_name} "
            "(first use downloads and verifies Ubuntu 22.04; this can take a few minutes)"
        )
        _limactl(
            config,
            "create",
            f"--name={instance_name}",
            "--tty=false",
            str(definition_path),
            timeout=_CREATE_TIMEOUT_SECONDS,
            progress_message=(
                f"still creating Lima instance {instance_name}; "
                "Lima is downloading or verifying the image and preparing the VM"
            ),
        )
        created = _inspect(config).instance
        if created is None:
            raise ConfigError(f"Lima instance {instance_name} is missing after create")
        _check_stored_definition(config, created)
        _log(f"instance created; starting {instance_name} and attaching its data disks")
        _limactl(
            config,
            "start",
            "--tty=false",
            instance_name,
            timeout=_START_TIMEOUT_SECONDS,
            progress_message=(
                f"still starting Lima instance {instance_name}; "
                "waiting for the guest to boot and initialize"
            ),
        )
        instance = _launched_instance(config, instance_name)
        _log(f"instance running with loopback SSH port {instance.ssh_local_port}")
        _log("installing Kern's secure bootstrap access in the guest")
        _deliver_stage_one(config, instance, stage_one_script)
        return instance
    except BaseException:
        # limactl can create the deterministic instance record before a
        # timeout or non-zero exit, so cleanup always re-inspects by exact
        # name before deleting the disposable VM.
        _cleanup_failed_instance(config, instance_name)
        raise


def _guest_storage_spec(disks: dict[str, str]) -> dict[str, Any]:
    return {
        "resolver": "lima",
        "resolver_input": {
            "admin": {"disk_name": disks["admin"]},
            "agent": {"disk_name": disks["agent"]},
        },
    }


def _result(
    config: LimaConfig,
    *,
    instance: LimaInstance,
    disks: dict[str, str],
    target_version: str,
    state: str = "running",
) -> dict[str, Any]:
    return {
        "agent_name": config.agent_name,
        "provider": "lima",
        "host": {"id": instance.name, "state": state},
        "storage": {
            "admin": {"id": disks["admin"]},
            "agent": {"id": disks["agent"]},
        },
        "ssh": {
            "host": "127.0.0.1",
            # Lima retains its allocated sshLocalPort in list output while an
            # instance is stopped, but no endpoint is listening then. Expose
            # only reachable operator endpoints in lifecycle results.
            "port": instance.ssh_local_port if state == "running" else None,
            "user": SSH_USER,
        },
        "admin_ui_local_url": f"http://127.0.0.1:{ADMIN_API_PORT}",
        "version": target_version,
    }


def main_for_lifecycle(command: LifecycleCommand) -> int:
    """Run deploy/upgrade/recover/reconfigure against the local Lima
    provider. The shared payload, bootstrap, and verification are identical to
    AWS hosts; only compute, disks, and the SSH endpoint differ."""
    try:
        if command.github_commit_sha is not None:
            raise ConfigError(
                "--bootstrap-from-github is not supported with --provider lima; local hosts "
                "provision from the local checkout over SSH"
            )
        config = build_lima_config(command)
        target_version = repo_version()
        replacement_operator_connections: tuple[RuntimeOperatorConnection, ...] | None = None
        if command.mode in {"deploy", "reconfigure"}:
            replacement_operator_connections = build_operator_connections(
                command.operator_ssh_public_key,
                command.operator_cloudflare_hostname,
                os.environ.get(OPERATOR_TUNNEL_TOKEN_ENV_NAME)
                if command.operator_cloudflare_hostname is not None
                else None,
            )
        _log(
            f"local Lima provider; preparing {command.mode} for "
            f"'{config.agent_name}' at Kern {target_version}"
        )
        _preflight(config)
        # Provisioning later runs ssh, scp, and ssh-keygen; verify all three
        # up front so a missing transport tool cannot surface only after a
        # working VM has been deleted.
        for tool in ("ssh", "scp", "ssh-keygen"):
            if shutil.which(tool) is None:
                raise ConfigError(
                    f"{tool} was not found on PATH; install the OpenSSH client tools "
                    "before running Kern lifecycle commands"
                )
        with OperationLock("lima", config.agent_name):
            inventory = _inspect(config)
            _validate_lima_preflight(command, inventory)
            with tempfile.TemporaryDirectory() as workdir_name:
                workdir = Path(workdir_name)
                # Disk names are deterministic, so the complete candidate can
                # be rendered and validated before creating disks or deleting
                # an existing working VM. Lima's validator fills current-host
                # defaults (including the VM driver) without mutating state.
                candidate_disks = {
                    role: _disk_name(config.agent_name, role)
                    for role in ("admin", "agent")
                }
                payload = _bootstrap_payload(
                    config.agent_name,
                    command.admin_password_sha256,
                    replacement_operator_connections,
                    _guest_storage_spec(candidate_disks),
                    mode=command.mode,
                    target_version=target_version,
                    allow_upgrade=command.allow_upgrade,
                    reset_admin_passkeys=command.reset_admin_passkeys,
                )
                deploy_key = _generate_deploy_key(workdir)
                stage_one_script = _render_ssh_user_data(
                    payload, deploy_key.with_suffix(".pub").read_text().strip()
                )
                definition_path = _write_vm_definition(config, workdir)
                _validate_vm_definition(config, definition_path)
                # The runtime code archive is the largest local artifact;
                # build it now so a local failure (disk space, unreadable
                # checkout file) cannot follow the deletion of a working VM.
                try:
                    _write_runtime_code_archive(workdir / CODE_ARCHIVE_NAME)
                except OSError as exc:
                    raise ConfigError(
                        f"could not build the runtime code archive locally: {exc}"
                    ) from exc

                if inventory.instance is not None:
                    _check_stored_version_hint(command, inventory.instance, target_version)
                    _delete_instance(config, inventory.instance.name)
                    inventory = _inspect(config)
                disks = _ensure_storage_disks(
                    config,
                    inventory,
                    create_missing=command.mode == "deploy",
                )
                instance = _launch(
                    config,
                    definition_path,
                    stage_one_script,
                )
                assert instance.ssh_local_port is not None
                try:
                    _provision_over_ssh(
                        "127.0.0.1",
                        deploy_key,
                        workdir,
                        port=instance.ssh_local_port,
                    )
                except BaseException:
                    _cleanup_failed_instance(config, instance.name)
                    raise
                _log("provisioning complete")
            result = _result(
                config, instance=instance, disks=disks, target_version=target_version
            )
            if replacement_operator_connections is not None:
                result["operator_connections"] = public_operator_connections(
                    [connection.to_json() for connection in replacement_operator_connections]
                )
            # stdout carries only this result JSON; all progress went to stderr.
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"{command.mode} command failed: {stderr or exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # e.g. a transport executable that disappeared after preflight; the
        # inner handlers have already run their cleanup.
        print(f"{command.mode} command failed: {exc}", file=sys.stderr)
        return 1


def _read_stored_definition(instance: LimaInstance) -> tuple[Path, str]:
    if not instance.dir:
        raise ConfigError(
            f"limactl list returned no directory for instance {instance.name}; "
            "cannot verify its stored definition"
        )
    definition_path = Path(instance.dir) / "lima.yaml"
    try:
        text = definition_path.read_text()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(
            f"could not read the stored Lima definition {definition_path}: {exc}"
        ) from exc
    return definition_path, text


def _check_stored_version_hint(
    command: LifecycleCommand,
    instance: LimaInstance,
    target_version: str,
) -> None:
    """Reject known-incompatible replacement before deleting working compute.

    The mounted admin disk remains authoritative during bootstrap. This
    deterministic definition hint is the same advisory preflight role as the
    AWS instance version tag and needs no separate local registry."""
    definition_path, text = _read_stored_definition(instance)
    matches = _STORED_VERSION_RE.findall(text)
    if len(matches) != 1:
        raise ConfigError(
            f"stored Lima definition {definition_path} for {instance.name} must contain "
            "exactly one Kern version hint; refusing to delete the instance"
        )
    version = matches[0]
    try:
        comparison = compare_versions(version, target_version)
    except ValueError as exc:
        raise ConfigError(
            f"stored Lima definition {definition_path} for {instance.name} has invalid "
            f"Kern version hint {version!r}; refusing to delete the instance"
        ) from exc
    _log(
        f"existing Lima instance version hint is {version}; "
        "admin disk version is authoritative"
    )
    if command.mode == "upgrade" and comparison >= 0:
        raise ConfigError(
            f"existing Kern Lima instance {instance.name} records version {version}; "
            f"upgrade requires preserved state older than target VERSION {target_version}; "
            "run recover for same-version repair, or target a newer Kern version for newer state"
        )
    if command.mode == "reconfigure" and comparison != 0:
        raise ConfigError(
            f"existing Kern Lima instance {instance.name} records version {version}; "
            f"reconfigure requires preserved state to match target VERSION {target_version}; "
            "run upgrade first, or target the version matching the preserved state"
        )


def _check_stored_definition(config: LimaConfig, instance: LimaInstance) -> None:
    """Fail closed unless the instance's stored definition is byte-identical
    to the one this module generates. ``limactl start`` boots whatever
    ``lima.yaml`` sits in the instance directory, so an out-of-band edit could
    otherwise start the VM with host mounts, forwarded ports, or without the
    durable disks. The definition is fully pinned, so exact comparison leaves
    no room for comment or duplicate-key tricks."""
    definition_path, text = _read_stored_definition(instance)
    rejection = ConfigError(
        f"stored Lima definition {definition_path} for {instance.name} is not one "
        "this Kern version generates; refusing to start it — replace the instance "
        "with upgrade or reconfigure, which regenerates and validates a fresh "
        "definition"
    )
    if text != _render_vm_definition(config):
        raise rejection


def main_for_power(mode: str, agent_name: str) -> int:
    """Dispatch power-only work to the focused Lima power module."""
    from host.cli.power_lima import main_for_power as run_power

    return run_power(mode, agent_name)
