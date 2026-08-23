"""Shared types for Kern host lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LifecycleCommand:
    mode: str
    agent_name: str
    # SHA-256 hex digest of the admin password; the CLI never handles the
    # password itself. Required for deploy and reconfigure.
    admin_password_sha256: str | None = None
    allow_upgrade: bool = False
    # GitHub delivery: a full 40-hex public-repo commit to pin, or "" to pin
    # the latest main commit. None selects the SSH delivery of the local
    # checkout.
    github_commit_sha: str | None = None
    # Operator endpoints for deploy and reconfigure; at least one is required.
    operator_ssh_public_key: str | None = None
    operator_cloudflare_hostname: str | None = None
    # Root recovery switch. Only reconfigure may remove durable passkeys.
    reset_admin_passkeys: bool = False
    # Infrastructure provider: "aws" (default, EC2/EBS) or "lima" (a local
    # VM on the operator's machine). Only the provider selection point and
    # provider modules branch on this.
    provider: str = "aws"


class LifecycleProvider(Protocol):
    """Infrastructure provider surface used by deploy/upgrade/recovery."""

    def main_for_lifecycle(self, command: LifecycleCommand) -> int: ...


class PowerProvider(Protocol):
    """Infrastructure provider surface used by start/stop."""

    def main_for_power(self, mode: str, agent_name: str) -> int: ...
