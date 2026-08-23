"""Power operations for the local Lima provider."""

from __future__ import annotations

import json
import subprocess
import sys

from host.cli import lifecycle_lima as lima
from host.cli.lifecycle_types import LifecycleCommand
from host.cli.operation_lock import OperationLock
from host.config import ConfigError
from host.version import repo_version


def main_for_power(mode: str, agent_name: str) -> int:
    """Run start/stop against the local Lima provider: provider power only,
    no bootstrap, durable disks untouched."""
    command = LifecycleCommand(mode=mode, agent_name=agent_name, provider="lima")
    try:
        config = lima.build_lima_config(command)
        lima._preflight(config)
        with OperationLock("lima", config.agent_name):
            inventory = lima._inspect(config)
            if inventory.instance is None:
                raise ConfigError(
                    "power operation requires exactly one existing Kern Lima instance; found none"
                )
            roles = set(inventory.disks)
            if roles != {"admin", "agent"}:
                missing = ", ".join(sorted({"admin", "agent"} - roles)) or "none"
                found = ", ".join(sorted(roles)) or "none"
                raise ConfigError(
                    f"power operation requires existing admin and agent data disks for {agent_name}; "
                    f"found {found}, missing {missing}"
                )
            initial_state = inventory.instance.status
            instance_name = inventory.instance.name

            if mode == "start":
                if initial_state not in {"Running", "Stopped"}:
                    raise ConfigError(
                        f"cannot start Kern Lima instance {instance_name} from state {initial_state}"
                    )
                lima._check_stored_definition(config, inventory.instance)
                if initial_state == "Stopped":
                    lima._limactl(config, "start", "--tty=false", instance_name, timeout=lima._START_TIMEOUT_SECONDS)
                instance = lima._launched_instance(config, instance_name)
                final_state = "running"
            else:
                if initial_state not in {"Running", "Stopped"}:
                    raise ConfigError(
                        f"cannot stop Kern Lima instance {instance_name} from state {initial_state}"
                    )
                if initial_state == "Running":
                    lima._limactl(config, "stop", instance_name, timeout=lima._STOP_TIMEOUT_SECONDS)
                refreshed = lima._inspect(config).instance
                if refreshed is None or refreshed.status != "Stopped":
                    raise ConfigError(
                        f"Kern Lima instance {instance_name} did not reach stopped state"
                    )
                instance = refreshed
                final_state = "stopped"
            disks = {role: disk.name for role, disk in inventory.disks.items()}
            result = lima._result(
                config,
                instance=instance,
                disks=disks,
                target_version=repo_version(),
                state=final_state,
            )
            # Power operations do not install a version; matching the AWS
            # power result, the field is omitted.
            del result["version"]
            result["operation"] = mode
            result["initial_state"] = initial_state.lower()
            # stdout carries only this result JSON.
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"{mode} command failed: {stderr or exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # e.g. an unwritable lock directory or limactl disappearing after
        # preflight; power operations mutate nothing that needs cleanup.
        print(f"{mode} command failed: {exc}", file=sys.stderr)
        return 1
