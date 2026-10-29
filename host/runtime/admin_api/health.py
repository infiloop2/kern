"""Operator health snapshots and degraded-status explanations.

Reads cached runtime state, host metrics, and version metadata.
The loopback proxy probe checks that the active policy is enforced.
Health reads never start provider processes or refresh credentials.
"""

from __future__ import annotations

import socket
from typing import Any

from host import session_options
from host.constants import LOOPBACK, PROXY_PORT
from host.runtime.admin_api import upgrade_check
from host.runtime.agent_runtime import orchestrator
from host.runtime.agent_runtime.orchestrator import agent_runtime_status
from host.runtime.core import network_policy, state
from host.runtime.core.host_metrics import host_metrics
from host.runtime.core.state import load_config
from host.version import version_status


def health() -> dict[str, Any]:
    runtime = agent_runtime_status()
    network_status = network_policy.network_status()
    version = version_status()
    host = host_metrics()
    network_error_detail = "The saved network-control policy could not be validated."
    if network_status == "active" and not proxy_alive():
        network_status = "error"  # policy says active but nothing is enforcing it
        network_error_detail = (
            "The network policy is active, but the enforcing proxy is not accepting connections."
        )
    issues = _health_issues(
        runtime,
        network_status,
        network_error_detail,
        version,
        host,
    )
    return {
        "status": "degraded" if issues else "ok",
        "issues": issues,
        "agent_name": load_config().get("agent_name"),
        "version": version,
        "upgrade": upgrade_check.status(),
        "agent_runtime": runtime,
        "network_controls": {"status": network_status},
        "host_runtime": host,
        "history": state.agent_history_counts(),
        "lifetime_tokens": state.lifetime_token_usage(),
    }



def _health_issues(
    runtime: dict[str, Any],
    network_status: str,
    network_error_detail: str,
    version: dict[str, Any],
    host: dict[str, Any],
) -> list[dict[str, str]]:
    """Explain every predicate that makes the host health status degraded."""
    issues: list[dict[str, str]] = []
    # The integration page the operator would open, which is named after the
    # provider rather than the runtime: every runtime sharing a provider is an
    # account card inside one page, so "Grok 2" would name a page that does
    # not exist.
    integration_labels = {
        "openai": "Codex", "claude": "Claude Code", "xai": "Grok", "bedrock": "AWS Bedrock",
    }
    for record in runtime["runtimes"]:
        if record.get("status") != "error":
            continue
        runtime_type = str(record.get("type", "agent runtime"))
        label = orchestrator.RUNTIME_LABELS.get(runtime_type, runtime_type)
        identity = session_options.RUNTIMES.get(runtime_type)
        provider = identity.provider if identity else None
        integration_label = integration_labels.get(provider or "", label)
        detail = record.get("error_message")
        if not isinstance(detail, str) or not detail:
            detail = "The runtime reported an error without additional details."
        issues.append(
            {
                "kind": "agent_runtime",
                "summary": f"{label} is unavailable",
                "detail": detail,
                "next_step": (
                    f"Open Home > Agent runtimes > {integration_label}, refresh its status, "
                    "and reconnect or revalidate the account if the error continues."
                ),
            }
        )
    if network_status == "error":
        issues.append(
            {
                "kind": "network_controls",
                "summary": "Network controls are unavailable",
                "detail": network_error_detail,
                "next_step": (
                    "Stop starting agents and use the operator plane to recover or upgrade the host."
                ),
            }
        )
    if version.get("status") == "mismatch":
        runtime_version = str(version.get("runtime", "unknown"))
        state_version = str(version.get("state", "unknown"))
        issues.append(
            {
                "kind": "version",
                "summary": "Host versions do not match",
                "detail": f"Runtime {runtime_version}; durable state {state_version}.",
                "next_step": "Run a Kern upgrade or recovery from the operator plane.",
            }
        )
    elif version.get("status") != "ok":
        unavailable: list[str] = []
        if version.get("runtime") is None:
            unavailable.append("running root version (/opt/kern-host/VERSION)")
        if version.get("state") is None:
            unavailable.append(
                "durable-state version (/mnt/kern-admin/admin-state/version.json)"
            )
        detail = (
            f"Unavailable or invalid: {', '.join(unavailable)}."
            if unavailable
            else "The host version status could not be read."
        )
        issues.append(
            {
                "kind": "version",
                "summary": "Host version information is unavailable",
                "detail": detail,
                "next_step": (
                    "Run a Kern recovery from the operator plane to restore the version metadata."
                ),
            }
        )
    root = host.get("filesystem", {}).get("mounts", {}).get("root", {})
    used_bytes = root.get("used_bytes", 0)
    total_bytes = root.get("total_bytes", 0)
    if (
        isinstance(used_bytes, int)
        and isinstance(total_bytes, int)
        and total_bytes > 0
        and used_bytes / total_bytes >= 0.9
    ):
        used_percent = used_bytes / total_bytes * 100
        issues.append(
            {
                "kind": "root_filesystem",
                "summary": "Root volume is nearly full",
                "detail": f"The root filesystem is {used_percent:.1f}% full.",
                "next_step": (
                    "Stop agent work, inspect /tmp and /var/tmp, and remove unneeded temporary "
                    "files; redeploy if free space does not recover."
                ),
            }
        )
    return issues



def proxy_alive() -> bool:
    """The proxy binds loopback, which nftables always allows, so a TCP
    connect is a meaningful liveness probe even while the network is locked
    down. If the proxy is down the agent has no network path at all."""
    try:
        with socket.create_connection((LOOPBACK, PROXY_PORT), timeout=1):
            return True
    except OSError:
        return False

