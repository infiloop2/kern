"""Shared client for fixed root helpers that exchange one JSON document.

Root helpers are an operating-system boundary used by several runtime and
integration modules.  Keeping the subprocess protocol here prevents lower
layers from importing an admin API service merely to reuse its private
runner.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


class HelperError(Exception):
    """A fixed root helper could not run or returned an error response."""


class HelperTimedOut(Exception):
    """A root helper exceeded its deadline.

    ``could_not_terminate`` records the sudo case where the unprivileged
    caller could not signal the root-owned child.
    """

    def __init__(self, could_not_terminate: bool) -> None:
        super().__init__("root helper timed out")
        self.could_not_terminate = could_not_terminate


def run_root_helper(
    argv: list[str], timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, PermissionError) as exc:
        raise HelperTimedOut(isinstance(exc, PermissionError)) from exc


def run_helper_json(
    command: list[str],
    payload: dict[str, Any],
    *,
    timeout: int,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(f"{command[-1]} failed: {exc}") from exc
    try:
        value = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise HelperError(f"{command[-1]} returned invalid JSON") from exc
    if proc.returncode != 0:
        error = value.get("error", {}) if isinstance(value, dict) else {}
        message = error.get("message") if isinstance(error, dict) else None
        raise HelperError(message or proc.stderr.strip() or f"{command[-1]} failed")
    if not isinstance(value, dict):
        raise HelperError(f"{command[-1]} returned invalid JSON")
    return value
