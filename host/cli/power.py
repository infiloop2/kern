"""Power command-line parsing, local serialization, and provider dispatch."""

from __future__ import annotations

import argparse


def main_for_power_mode(mode: str, argv: list[str] | None = None) -> int:
    if mode not in {"start", "stop"}:
        raise ValueError(f"unsupported power mode: {mode}")
    parser = argparse.ArgumentParser(
        prog=f"python3 -m host.cli.{mode}",
        description=f"{mode.capitalize()} an existing Kern host.",
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
            "Infrastructure provider: 'aws' (default) operates the EC2 instance; 'lima' "
            "operates the local Lima VM. See README.md for local host setup."
        ),
    )
    args = parser.parse_args(argv)
    if args.provider == "aws":
        from host.cli import power_aws

        return power_aws.main_for_power(mode, args.agent_name)
    from host.cli import lifecycle_lima

    return lifecycle_lima.main_for_power(mode, args.agent_name)
