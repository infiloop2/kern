"""Structured host-diagnostic emission for journald ingestion.

Services call :func:`report_unexpected` only for service-level exceptions and
:func:`report_warning` for contained but suspicious behavior. The reporter
does not inspect locals, request headers, the environment, or arbitrary object
representations; callers explicitly select any operator-facing context.
``logger --journald`` gives the collector a dedicated field to follow while
systemd attaches the trusted originating unit, PID, timestamp, and boot id.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import traceback as traceback_module
from typing import Any


JOURNAL_FIELD = "KERN_HOST_DIAGNOSTIC"
JOURNAL_FIELD_VALUE = "1"
JOURNAL_TAG = "kern-host-diagnostic"
MAX_COMPONENT_BYTES = 256
MAX_EXCEPTION_TYPE_BYTES = 256
MAX_SUMMARY_BYTES = 2048
MAX_TRACEBACK_BYTES = 32768
MAX_CONTEXT_BYTES = 4096
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VERSION_FILE = _REPO_ROOT / "VERSION"


def _clip(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", "ignore")


def _relative_filename(filename: str) -> str:
    try:
        return str(Path(filename).resolve().relative_to(_REPO_ROOT))
    except (OSError, ValueError):
        return Path(filename).name


def _safe_context(context: dict[str, Any] | None) -> dict[str, str | int | float | bool | None]:
    if context is None:
        return {}
    safe: dict[str, str | int | float | bool | None] = {}
    for key, value in context.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            continue
        if value is None or isinstance(value, (str, int, bool)):
            safe[key] = _clip(value, 512) if isinstance(value, str) else value
        elif isinstance(value, float) and math.isfinite(value):
            safe[key] = value
    while safe and len(json.dumps(safe, sort_keys=True).encode()) > MAX_CONTEXT_BYTES:
        safe.pop(next(reversed(safe)))
    return safe


def _exception_record(
    component: str,
    exc: BaseException,
    context: dict[str, Any] | None,
    *,
    severity: str = "error",
    kind: str = "unexpected_exception",
) -> dict[str, Any]:
    frames = traceback_module.extract_tb(exc.__traceback__)
    frame_lines: list[str] = []
    fingerprint_frames: list[str] = []
    for frame in frames:
        filename = _relative_filename(frame.filename)
        fingerprint_frames.append(f"{filename}:{frame.lineno}:{frame.name}")
        source = f"\n  {frame.line.strip()}" if frame.line else ""
        frame_lines.append(f'File "{filename}", line {frame.lineno}, in {frame.name}{source}')
    exception_type = type(exc).__name__
    summary = _clip(str(exc).strip() or exception_type, MAX_SUMMARY_BYTES)
    safe_context = _safe_context(context)
    fingerprint_parts = [severity, kind, component, exception_type, *fingerprint_frames]
    if severity == "warning":
        fingerprint_parts.extend((
            summary,
            json.dumps(safe_context, sort_keys=True, separators=(",", ":")),
        ))
    fingerprint_input = "\n".join(fingerprint_parts)
    try:
        host_version = _VERSION_FILE.read_text().strip()
    except OSError:
        host_version = ""
    return {
        "component": _clip(component, MAX_COMPONENT_BYTES),
        "severity": severity,
        "kind": kind,
        "exception_type": _clip(exception_type, MAX_EXCEPTION_TYPE_BYTES),
        "summary": summary,
        "traceback": _clip("\n".join(frame_lines), MAX_TRACEBACK_BYTES),
        "context": safe_context,
        "fingerprint": hashlib.sha256(fingerprint_input.encode()).hexdigest(),
        "host_version": _clip(host_version, 64),
    }


def emit_record(record: dict[str, Any]) -> None:
    """Emit one bounded record to journald, falling back to protected stderr.

    Reporting must never replace or mask the failure being reported.
    """
    message = json.dumps(record, sort_keys=True, separators=(",", ":"))
    priority = "4" if record.get("severity") == "warning" else "3"
    fields = (
        f"MESSAGE={message}\n"
        f"PRIORITY={priority}\n"
        f"SYSLOG_IDENTIFIER={JOURNAL_TAG}\n"
        f"{JOURNAL_FIELD}={JOURNAL_FIELD_VALUE}\n\n"
    )
    try:
        subprocess.run(
            ["/usr/bin/logger", "--journald"],
            input=fields,
            text=True,
            check=True,
            timeout=2,
        )
    except Exception:
        print(f"{JOURNAL_TAG}: {message}", file=sys.stderr, flush=True)


def report_unexpected(
    component: str,
    exc: BaseException,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    try:
        emit_record(_exception_record(component, exc, context))
    except Exception:
        print(f"{JOURNAL_TAG}: unexpected error reporter failure", file=sys.stderr, flush=True)


def report_warning(
    component: str,
    issue: BaseException | str,
    *,
    context: dict[str, Any] | None = None,
    kind: str = "unexpected_behavior",
) -> None:
    """Record a contained anomaly for operator debugging.

    String warnings deliberately have no traceback. Exception warnings retain
    their bounded traceback, but remain warnings because the service handled
    the failure and continued operating.
    """
    try:
        if isinstance(issue, BaseException):
            record = _exception_record(
                component,
                issue,
                context,
                severity="warning",
                kind=kind,
            )
        else:
            summary = str(issue).strip() or "Unexpected behavior"
            safe_context = _safe_context(context)
            fingerprint_input = json.dumps(
                [kind, component, summary, safe_context],
                sort_keys=True,
                separators=(",", ":"),
            )
            try:
                host_version = _VERSION_FILE.read_text().strip()
            except OSError:
                host_version = ""
            record = {
                "component": _clip(component, MAX_COMPONENT_BYTES),
                "severity": "warning",
                "kind": kind,
                "exception_type": "",
                "summary": _clip(summary, MAX_SUMMARY_BYTES),
                "traceback": "",
                "context": safe_context,
                "fingerprint": hashlib.sha256(fingerprint_input.encode()).hexdigest(),
                "host_version": _clip(host_version, 64),
            }
        emit_record(record)
    except Exception:
        print(f"{JOURNAL_TAG}: warning reporter failure", file=sys.stderr, flush=True)


def report_service_exit(service: str, result: str, exit_code: str, exit_status: str) -> None:
    """Emit an abnormal systemd service exit from an ``ExecStopPost`` hook."""
    if not result or result == "success":
        return
    summary = f"{service} stopped: result={result}, code={exit_code or 'unknown'}, status={exit_status or 'unknown'}"
    fingerprint_input = "\n".join(("service_exit", service, result, exit_code, exit_status))
    try:
        host_version = _VERSION_FILE.read_text().strip()
    except OSError:
        host_version = ""
    emit_record({
        "component": "systemd",
        "severity": "error",
        "kind": "service_exit",
        "exception_type": "",
        "summary": _clip(summary, MAX_SUMMARY_BYTES),
        "traceback": "",
        "context": {
            "service": _clip(service, 128),
            "service_result": _clip(result, 64),
            "exit_code": _clip(exit_code, 64),
            "exit_status": _clip(exit_status, 64),
        },
        "fingerprint": hashlib.sha256(fingerprint_input.encode()).hexdigest(),
        "host_version": _clip(host_version, 64),
    })
