"""Ingest structured Kern host-diagnostic journal records into PostgreSQL."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
from typing import Any

from host.runtime.core import state
from host.runtime.core.host_errors import (
    JOURNAL_FIELD,
    JOURNAL_FIELD_VALUE,
    MAX_COMPONENT_BYTES,
    MAX_CONTEXT_BYTES,
    MAX_EXCEPTION_TYPE_BYTES,
    MAX_SUMMARY_BYTES,
    MAX_TRACEBACK_BYTES,
)


RETRY_SECONDS = 3
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
SEVERITIES = frozenset({"error", "warning"})
KINDS = frozenset({
    "unexpected_exception",
    "service_exit",
    "invariant_failure",
    "provider_failure",
    "unexpected_behavior",
})


def allowed_units() -> frozenset[str]:
    units = {
        "kern-postgres.service",
        "kern-network-proxy.service",
        "kern-tools.service",
        "kern-agent-network.service",
        "kern-admin-api.service",
        "kern-workspace.service",
        "kern-host-errors.service",
        "kern-cloudflared.service",
    }
    return frozenset(units)


def journal_command(units: frozenset[str]) -> list[str]:
    return [
        "/usr/bin/journalctl",
        f"{JOURNAL_FIELD}={JOURNAL_FIELD_VALUE}",
        *(f"_SYSTEMD_UNIT={unit}" for unit in sorted(units)),
        "--follow",
        "--lines=0",
        "--output=json",
        "--no-pager",
        "--all",
    ]


def _bounded_string(value: Any, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError("required string field is missing")
        return ""
    value = value.replace("\x00", r"\0")
    encoded = value.encode("utf-8", "replace")
    if required and not encoded:
        raise ValueError("required string field is empty")
    if len(encoded) > limit:
        raise ValueError("string field exceeds its storage bound")
    return value


def _context(value: Any) -> dict[str, str | int | float | bool | None]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, str | int | float | bool | None] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64 or "\x00" in key:
            continue
        if item is None or isinstance(item, (int, bool)):
            safe[key] = item
        elif isinstance(item, str):
            safe[key] = item.replace("\x00", r"\0")
        elif isinstance(item, float) and math.isfinite(item):
            safe[key] = item
    if len(json.dumps(safe, sort_keys=True).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("context exceeds its storage bound")
    return safe


def journal_record(raw: str) -> tuple[dict[str, Any], int]:
    journal = json.loads(raw)
    if not isinstance(journal, dict):
        raise ValueError("journal record must be an object")
    realtime_value = journal.get("__REALTIME_TIMESTAMP")
    if isinstance(realtime_value, bool) or not isinstance(realtime_value, (str, int)):
        raise ValueError("journal timestamp is missing")
    realtime = int(realtime_value)
    if realtime < 0:
        raise ValueError("journal timestamp must be non-negative")
    return journal, realtime


def parse_journal_record(
    raw: str,
    *,
    units: frozenset[str] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    journal, realtime = journal_record(raw)
    unit = journal.get("_SYSTEMD_UNIT")
    if not isinstance(unit, str) or unit not in (allowed_units() if units is None else units):
        return realtime, None
    message = journal.get("MESSAGE")
    if not isinstance(message, str):
        return realtime, None
    payload = json.loads(message)
    if not isinstance(payload, dict):
        raise ValueError("host diagnostic payload must be an object")
    kind = _bounded_string(payload.get("kind"), 64, required=True)
    if kind not in KINDS:
        raise ValueError("unsupported host diagnostic kind")
    severity = _bounded_string(payload.get("severity"), 16, required=True)
    if severity not in SEVERITIES:
        raise ValueError("unsupported host diagnostic severity")
    fingerprint = _bounded_string(payload.get("fingerprint"), 64, required=True)
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("invalid host diagnostic fingerprint")
    pid_value = journal.get("_PID")
    pid = int(pid_value) if isinstance(pid_value, (str, int)) and str(pid_value).isdigit() else None
    boot_id = _bounded_string(journal.get("_BOOT_ID"), 64)
    return realtime, {
        "service": unit.removesuffix(".service"),
        "component": _bounded_string(payload.get("component"), MAX_COMPONENT_BYTES, required=True),
        "severity": severity,
        "kind": kind,
        "exception_type": _bounded_string(payload.get("exception_type"), MAX_EXCEPTION_TYPE_BYTES),
        "summary": _bounded_string(payload.get("summary"), MAX_SUMMARY_BYTES, required=True),
        "traceback": _bounded_string(payload.get("traceback"), MAX_TRACEBACK_BYTES),
        "context": _context(payload.get("context")),
        "fingerprint": fingerprint,
        "host_version": _bounded_string(payload.get("host_version"), 64),
        "boot_id": boot_id,
        "pid": pid,
    }


def _stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _consume(process: subprocess.Popen[str], units: frozenset[str]) -> bool:
    assert process.stdout is not None
    for line in process.stdout:
        try:
            realtime, event = parse_journal_record(line, units=units)
        except Exception as exc:
            print(f"host diagnostic collector skipped invalid payload: {exc}", file=sys.stderr, flush=True)
            continue
        try:
            if event is not None:
                state.ingest_host_diagnostic(realtime, event)
        except Exception as exc:
            print(f"host diagnostic collector paused: {exc}", file=sys.stderr, flush=True)
            _stop(process)
            return False
    return process.wait() == 0


def run_forever() -> None:
    units = allowed_units()
    while True:
        try:
            process = subprocess.Popen(
                journal_command(units),
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                text=True,
                bufsize=1,
            )
            clean = _consume(process, units)
            if clean:
                print("host diagnostic journal stream ended; restarting", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"host diagnostic collector unavailable: {exc}", file=sys.stderr, flush=True)
        time.sleep(RETRY_SECONDS)


def main() -> int:
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
