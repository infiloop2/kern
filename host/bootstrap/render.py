"""Rendering for host provisioning artifacts.

One bootstrap implementation serves both provisioning deliveries: the SSH
delivery renders these artifacts on the operator machine and pushes them to
the instance, and the GitHub delivery renders them on the instance itself from
a fetched checkout (``host.bootstrap.self_provision``). Everything here runs
with the standard library only, because on the GitHub path it executes on a
stock Ubuntu image before any packages are installed.
"""

from __future__ import annotations

import json
from pathlib import Path
import tarfile
from typing import Any

from host.config import InputConfig, RuntimeOperatorConnection
from host.constants import (
    ADMIN_API_PORT,
    AGENT_PREVIEW_PORT_BASE,
    AGENT_PREVIEW_PORT_COUNT,
    WORKSPACE_ADMIN_GROUP_GID,
    WORKSPACE_PORT,
    PROXY_PORT,
    PUBLIC_GITHUB_REPOSITORY,
    SERVICE_ACCOUNTS,
)

TEMPLATE_DIR = Path(__file__).resolve().parent


def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def _bootstrap_payload(
    config: InputConfig,
    admin_password_sha256: str | None,
    replacement_operator_connections: tuple[RuntimeOperatorConnection, ...] | None = None,
    storage_volumes: dict[str, str] | None = None,
    *,
    mode: str,
    target_version: str,
    allow_upgrade: bool = False,
    reset_admin_passkeys: bool = False,
) -> dict[str, Any]:
    runtime_config: dict[str, Any] = {
        "agent_name": config.agent_name,
    }
    if replacement_operator_connections is not None:
        runtime_config["operator_connections"] = [
            connection.to_json() for connection in replacement_operator_connections
        ]
    if admin_password_sha256 is not None:
        runtime_config["admin_password_sha256"] = admin_password_sha256
    operation: dict[str, Any] = {
        "mode": mode,
        "target_version": target_version,
        "allow_upgrade": allow_upgrade,
    }
    if reset_admin_passkeys:
        operation["reset_admin_passkeys"] = True
    return {
        "operation": operation,
        "runtime_config": runtime_config,
        "storage_volumes": storage_volumes or {},
    }


def _write_runtime_code_archive(code_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]

    def runtime_only(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if "__pycache__" in tarinfo.name:
            return None
        if tarinfo.name == "host/cli" or tarinfo.name.startswith("host/cli/"):
            return None  # lifecycle CLIs never run on the host
        return tarinfo

    with tarfile.open(code_path, "w:gz") as tar:
        # host/ includes the bundled tool framework and packages under
        # host/tools; the admin service imports them at startup. VERSION rides
        # along so self_provision can enforce the version gate on the
        # delivered tree, the same as on a fetched checkout.
        tar.add(root / "host", arcname="host", filter=runtime_only)
        tar.add(root / "VERSION", arcname="VERSION")


def _render_ssh_user_data(payload: dict[str, Any], deploy_public_key: str) -> str:
    # json.dumps never emits raw newlines, so the compact payload is one line
    # and can never collide with the heredoc delimiter.
    return (
        SSH_USER_DATA_TEMPLATE
        .replace("@PAYLOAD_JSON@", json.dumps(payload, sort_keys=True))
        .replace("@DEPLOY_PUBLIC_KEY@", deploy_public_key)
    )


def _render_github_user_data(payload: dict[str, Any], commit_sha: str) -> str:
    # json.dumps never emits raw newlines, so the compact payload is one line
    # and can never collide with the heredoc delimiter.
    return (
        GITHUB_USER_DATA_TEMPLATE
        .replace("@PAYLOAD_JSON@", json.dumps(payload, sort_keys=True))
        .replace("@GITHUB_REPOSITORY@", PUBLIC_GITHUB_REPOSITORY)
        .replace("@COMMIT_SHA@", commit_sha)
    )


def _service_account_constants() -> str:
    """Shell uid/gid variables for the pinned core service accounts, rendered
    from the same host.constants table verify_deploy checks on the host."""
    lines = []
    for name, uid in SERVICE_ACCOUNTS.items():
        prefix = name.upper().replace("-", "_")
        lines.append(f"{prefix}_UID={uid}")
        lines.append(f"{prefix}_GID={uid}")
    lines.append(f"KERN_WORKSPACE_API_GID={WORKSPACE_ADMIN_GROUP_GID}")
    return "\n".join(lines)


def _agent_preview_nftables_rules() -> str:
    """Default-deny nftables policy for the agent preview port range on loopback.

    The agent may bind any loopback port (the input hook accepts all loopback),
    so a preview server can *exist* under many uids; what this policy governs is
    who may exchange packets with the range on the output hook. It is written as
    an allowlist followed by a two-directional default drop, so the reachable
    set is closed by construction — a new service account, a new local user, or
    a future carve-out cannot silently gain access the way a fall-through to the
    broad ``oif lo accept`` below would grant it.

    Exactly three flows are allowed, then everything else in the range is
    dropped in both directions:

    - ``dport`` in range, ``kern-agent``: the agent *originates* to its own
      servers (curl / headless-browser checks, integration tests).
    - established ``sport`` in range, ``kern-agent``: the agent's own listeners
      *reply*. Scoped to established so the agent cannot originate from a
      preview source port (a NEW originated flow is dropped by the sport default
      drop below).
    - ``dport`` in range, ``kern-operator``: the operator's SSH local forward,
      opened by their ``kern-operator`` session, may reach a preview server so
      the operator can view it. (``kern-operator`` is created by stage-1 user
      data on every deploy path, so it always resolves at ruleset-load time.)

    Then:

    - ``dport`` default drop: no other principal — service account, root, or any
      future local user — may *dial* the range. This is what stops a compromised
      egress-capable service (``kern-tools``, ``kern-proxy``, ``cloudflared``)
      from connecting to an agent preview server and exfiltrating its content,
      and stops the admin API from being an SSRF confused-deputy into it.
    - ``sport`` default drop: no non-agent principal may *answer* on the range
      (the agent's own replies are already accepted above). This stops a
      compromised service that bound a preview port from completing a handshake
      the agent originated — which would let a prompt-injected agent POST
      workspace data straight to that service, bypassing the policy proxy. It
      also subsumes the workspace source-port drop.

    Net: only agent<->agent traffic and the operator's forward touch the range;
    every other principal is denied both directions before the broad accept."""
    first = AGENT_PREVIEW_PORT_BASE
    last = AGENT_PREVIEW_PORT_BASE + AGENT_PREVIEW_PORT_COUNT - 1
    return "\n".join([
        "    # Agent preview ports: default-deny with a small allowlist. The agent",
        "    # may originate to and serve on this range; the operator's SSH forward",
        "    # may reach a preview server. Every other principal is dropped in both",
        "    # directions (dport: nobody else may dial the range; sport: nobody but",
        "    # the agent may answer on it) before the broad loopback accept, so no",
        "    # service account or future user can reach an agent preview",
        "    # server or receive a connection the agent originated.",
        f'    oif lo tcp dport {first}-{last} meta skuid "kern-agent" accept',
        f'    oif lo ct state established,related tcp sport {first}-{last} meta skuid "kern-agent" accept',
        f'    oif lo tcp dport {first}-{last} meta skuid "kern-operator" accept',
        f"    oif lo tcp dport {first}-{last} drop",
        f"    oif lo tcp sport {first}-{last} drop",
    ])


def _render_bootstrap() -> str:
    return (
        BOOTSTRAP_TEMPLATE
        .replace("@ADMIN_PORT@", str(ADMIN_API_PORT))
        .replace("@WORKSPACE_PORT@", str(WORKSPACE_PORT))
        .replace("@PROXY_PORT@", str(PROXY_PORT))
        .replace("@AGENT_PREVIEW_NFTABLES_RULES@", _agent_preview_nftables_rules())
        .replace("@SERVICE_ACCOUNT_CONSTANTS@", _service_account_constants())
    )


# SSH delivery, stage 1 EC2 user data: base-account hardening, the single-use
# deploy key (in authorized_keys2 so stage 2 can delete that whole file to
# revoke it), and the embedded provisioning payload. Both deliveries stage the
# payload through user data; only code delivery differs.
SSH_USER_DATA_TEMPLATE = _load_template("user_data_ssh.sh")


# GitHub delivery, single-stage EC2 user data: hardens the base accounts,
# stages the payload, fetches the pinned public commit, and hands off to
# host.bootstrap.self_provision on the instance itself.
GITHUB_USER_DATA_TEMPLATE = _load_template("user_data_github.sh")


# The one bootstrap script both deliveries run as root on the instance,
# rendered on the instance itself by host.bootstrap.self_provision from the
# delivered code: the scp'd archive on the SSH delivery, the fetched checkout
# on the GitHub delivery. It consumes the payload staged by user data.
BOOTSTRAP_TEMPLATE = _load_template("bootstrap.sh")
