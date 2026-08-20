"""Lifecycle command-line parsing, local serialization, and provider dispatch."""

from __future__ import annotations

import argparse
import re
from host.constants import OPERATOR_TUNNEL_TOKEN_ENV_NAME, PUBLIC_GITHUB_REPOSITORY
from host.cli.lifecycle_types import LifecycleCommand


_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_EMPTY_PASSWORD_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _parse_args(mode: str, argv: list[str] | None) -> LifecycleCommand:
    if mode not in {"deploy", "upgrade", "recover", "reconfigure"}:
        raise ValueError(f"unsupported lifecycle mode: {mode}")
    descriptions = {
        "deploy": "Create a new Kern host with no existing instance or data volumes",
        "upgrade": "Upgrade preserved Kern state without changing admin password or operator access",
        "recover": "Create a replacement host from preserved data volumes and existing operator access",
        "reconfigure": "Replace operator access and refresh the admin password for preserved Kern state",
    }
    parser = argparse.ArgumentParser(
        prog=f"python3 -m host.cli.{mode}",
        description=descriptions[mode],
    )
    parser.add_argument(
        "--agent-name",
        required=True,
        help="Stable host name: 1-50 characters of letters, numbers, '-' or '_'.",
    )
    parser.add_argument(
        "--provider",
        choices=("aws", "lima"),
        default="aws",
        help=(
            "Infrastructure provider: 'aws' (default) provisions EC2/EBS; 'lima' provisions "
            "a dedicated local VM with durable Lima data disks on this machine. See "
            "README.md for local host setup."
        ),
    )
    parser.add_argument(
        "--bootstrap-from-github",
        nargs="?",
        const="",
        metavar="COMMIT_SHA",
        help=(
            f"Provision the instance from a pinned {PUBLIC_GITHUB_REPOSITORY} commit via "
            "EC2 user data instead of pushing the local checkout over SSH; without a value, "
            "the latest main commit is pinned. The CLI reads the commit's VERSION from GitHub "
            "first and asks for confirmation. The command returns once the instance is "
            "launched with its volumes attached; bootstrap completes on the host."
        ),
    )
    if mode in {"deploy", "reconfigure"}:
        parser.add_argument(
            "--operator-ssh-public-key",
            metavar="OPENSSH_PUBLIC_KEY",
            help=(
                "Operator SSH endpoint: the ssh-ed25519 or ssh-rsa public key content to "
                "install. At least one operator endpoint is required."
            ),
        )
        parser.add_argument(
            "--operator-cloudflare-hostname",
            metavar="HOSTNAME",
            help=(
                "Operator Cloudflare Tunnel endpoint: the exact public hostname. The tunnel "
                f"token is read from {OPERATOR_TUNNEL_TOKEN_ENV_NAME}. At least one operator "
                "endpoint is required."
            ),
        )
        parser.add_argument(
            "--admin-password-sha256",
            required=True,
            metavar="HEX_DIGEST",
            help=(
                "SHA-256 hex digest of the admin password to install. The host stores only "
                "this hash and the CLI never sees the password itself. Compute it locally, "
                "for example: printf %%s 'your-password' | sha256sum"
            ),
        )
    if mode == "recover":
        parser.add_argument(
            "--allow-upgrade",
            action="store_true",
            help="Allow recovery to also advance preserved state to the target VERSION.",
        )
    if mode == "reconfigure":
        parser.add_argument(
            "--reset-admin-passkeys",
            action="store_true",
            help=(
                "Delete every enrolled admin passkey during reconfigure. Use only "
                "for recovery after losing passkey access or changing the public hostname."
            ),
        )
    args = parser.parse_args(argv)
    admin_password_sha256: str | None = None
    if mode in {"deploy", "reconfigure"}:
        digest = str(args.admin_password_sha256).strip().lower()
        if not _SHA256_HEX_RE.fullmatch(digest):
            parser.error("--admin-password-sha256 must be a 64-character hex SHA-256 digest")
        if digest == _EMPTY_PASSWORD_SHA256:
            parser.error(
                "--admin-password-sha256 is the SHA-256 of an empty password; set a non-empty "
                "admin password (a caller hashing an unset environment variable lands here)"
            )
        admin_password_sha256 = digest
    github_commit_sha = args.bootstrap_from_github
    if github_commit_sha is not None:
        github_commit_sha = github_commit_sha.strip()
        if github_commit_sha and not _COMMIT_SHA_RE.fullmatch(github_commit_sha):
            parser.error("--bootstrap-from-github must be a full 40-character lowercase hex commit sha")
    if args.provider == "lima" and github_commit_sha is not None:
        parser.error("--bootstrap-from-github is not supported with --provider lima")
    return LifecycleCommand(
        mode=mode,
        agent_name=args.agent_name,
        admin_password_sha256=admin_password_sha256,
        allow_upgrade=bool(getattr(args, "allow_upgrade", False)),
        github_commit_sha=github_commit_sha,
        operator_ssh_public_key=getattr(args, "operator_ssh_public_key", None),
        operator_cloudflare_hostname=getattr(args, "operator_cloudflare_hostname", None),
        reset_admin_passkeys=bool(getattr(args, "reset_admin_passkeys", False)),
        provider=args.provider,
    )


def main_for_mode(mode: str, argv: list[str] | None = None) -> int:
    command = _parse_args(mode, argv)
    if command.provider == "aws":
        from host.cli import lifecycle_aws

        return lifecycle_aws.main_for_lifecycle(command)
    from host.cli import lifecycle_lima

    return lifecycle_lima.main_for_lifecycle(command)
