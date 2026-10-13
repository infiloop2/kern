"""Hourly Codex size, archive and unreferenced-root reconciliation."""

from __future__ import annotations

from collections import Counter
import math
from pathlib import PurePosixPath
import time
from typing import Any

from host.runtime.agent_runtime import codex_app_server, orchestrator
from host.runtime.core import host_errors, state

ORPHAN_GRACE_SECONDS = 7 * 24 * 3600
CLEANUP_INIT_TIMEOUT_SECONDS = 45


def _ineligible_reason(thread: dict[str, Any], runtime: str, before: float) -> str | None:
    if thread.get("originator") != "kern-host":
        return "foreign_origin"
    if thread.get("cwd") != codex_app_server.AGENT_CWD:
        return "wrong_cwd"
    if thread.get("parentThreadId") is not None:
        return "native_child"
    updated = thread.get("updatedAt")
    if (isinstance(updated, bool) or not isinstance(updated, (int, float))
        or not math.isfinite(updated) or updated <= 0):
        return "invalid_updated_at"
    if updated > before:
        return "inactivity_grace"
    status = thread.get("status")
    if not isinstance(status, dict) or status.get("type") not in {"notLoaded", "idle", "systemError"}:
        return "provider_active_or_unknown"
    if thread.get("ephemeral") is not False:
        return "ephemeral_or_unknown"
    path = thread.get("path")
    if not isinstance(path, str):
        return "missing_path"
    rollout = PurePosixPath(path)
    home = PurePosixPath(codex_app_server.AGENT_CWD) / f".{runtime}"
    if (
        ".." in rollout.parts
        or not any(rollout.is_relative_to(home / directory) for directory in ("sessions", "archived_sessions"))
        or not rollout.name.endswith(f"-{thread['id']}.jsonl")
    ):
        return "unsafe_path"
    return None


def reconcile_codex_sessions() -> int:
    """Retire eligible roots and record bounded per-account progress/outcomes.

    Running/FINISHING turns and seven-day archive/orphan grace stay protected.
    Native deletion owns provider metadata; no rollout or database is unlinked.
    """
    deleted = 0
    before = time.time() - ORPHAN_GRACE_SECONDS
    for runtime in codex_app_server.CODEX_RUNTIME_TYPES:
        server = codex_app_server.CodexAppServer(runtime_type=runtime)
        started = time.monotonic()
        counts: Counter[str] = Counter({
            "mapped_candidates": 0, "mapped_checked": 0, "mapped_measured": 0,
            "mapped_measured_bytes": 0, "detached": 0, "stored_roots": 0,
            "orphan_checked": 0, "retired_oversized": 0, "retired_archived": 0,
            "retired_orphan": 0, "measured_retired_bytes": 0, "measurement_failed": 0,
        })
        retired: set[str] = set()
        phase = "initialize"
        thread_id: str | None = None
        session_id: str | None = None
        outcome = "completed"
        host_errors.report_info(
            "admin_api.codex_session_sweep",
            f"Codex cleanup started for {runtime}",
            context={
                "runtime": runtime, "phase": phase,
                "limit_bytes": codex_app_server.SESSION_ROLLOUT_MAX_BYTES,
                "orphan_grace_seconds": ORPHAN_GRACE_SECONDS,
                "init_timeout_seconds": CLEANUP_INIT_TIMEOUT_SECONDS,
            },
        )
        try:
            server.start(init_timeout=CLEANUP_INIT_TIMEOUT_SECONDS)
            phase = "mapped_candidates"
            candidates = state.codex_cleanup_candidates(runtime)
            counts["mapped_candidates"] = len(candidates)
            for thread_id, session_id, archived in candidates:
                counts["mapped_checked"] += 1
                phase = "mapped_live_check"
                if thread_id in orchestrator.live_thread_ids():
                    counts["skip_live_thread"] += 1
                    continue
                size = 0
                if not archived:
                    phase = "measure"
                    try:
                        size = codex_app_server.session_rollout_size(server, session_id)
                    except codex_app_server.CodexRolloutMeasurementError as exc:
                        counts["measurement_failed"] += 1
                        host_errors.report_warning(
                            "admin_api.codex_session_sweep.measure", exc,
                            context={"runtime": runtime, "thread_id": thread_id, "session_id": session_id},
                        )
                        continue
                    except codex_app_server.CodexAppServerError as exc:
                        if str(exc) != f"no rollout found for thread id {session_id}":
                            raise
                        counts["skip_missing_rollout"] += 1
                        continue
                    counts["mapped_measured"] += 1
                    counts["mapped_measured_bytes"] += size
                    if size < codex_app_server.SESSION_ROLLOUT_MAX_BYTES:
                        counts["skip_zero_or_unmaterialized" if size == 0 else "skip_below_limit"] += 1
                        continue
                phase = "detach"
                with state.mutation() as cur:
                    if thread_id in orchestrator.live_thread_ids():
                        counts["skip_live_thread"] += 1
                        continue
                    detached = state.detach_idle_codex_session(
                        cur, thread_id, runtime, session_id, archived=archived,
                    )
                if not detached:
                    counts["skip_changed_mapping"] += 1
                    continue
                counts["detached"] += 1
                phase = "delete_mapped"
                codex_app_server.delete_session(server, session_id)
                retired.add(session_id)
                counts["retired_archived" if archived else "retired_oversized"] += 1
                counts["measured_retired_bytes"] += size
                deleted += 1
            thread_id = None
            session_id = None
            phase = "list_roots"
            sessions = codex_app_server.stored_sessions(server)
            counts["stored_roots"] = len(sessions)
            phase = "references"
            referenced = state.referenced_provider_session_ids(runtime)
            live = orchestrator.live_provider_session_ids(runtime)
            counts["referenced_roots"] = len(referenced)
            counts["live_roots"] = len(live)
            for thread in sessions:
                phase = "orphan_check"
                session_id = thread["id"]
                if not isinstance(session_id, str):
                    raise codex_app_server.CodexAppServerError("Codex returned an invalid orphan id")
                counts["orphan_checked"] += 1
                if session_id in retired:
                    counts["skip_already_retired"] += 1
                    continue
                if session_id in live:
                    counts["skip_live_session"] += 1
                    continue
                if session_id in referenced:
                    counts["skip_referenced"] += 1
                    continue
                reason = _ineligible_reason(thread, runtime, before)
                if reason is not None:
                    counts[f"skip_{reason}"] += 1
                    continue
                phase = "read_orphan"
                try:
                    fresh = server.call("thread/read", {
                        "threadId": session_id, "includeTurns": False,
                    }, timeout=5)["thread"]
                except codex_app_server.CodexAppServerError as exc:
                    if str(exc) != f"no rollout found for thread id {session_id}":
                        raise
                    fresh = thread
                if not isinstance(fresh, dict) or fresh.get("id") != session_id:
                    raise codex_app_server.CodexAppServerError("Codex returned mismatched orphan metadata")
                reason = _ineligible_reason(fresh, runtime, before)
                if reason is not None:
                    counts[f"skip_rechecked_{reason}"] += 1
                    continue
                phase = "orphan_references"
                if session_id in (state.referenced_provider_session_ids(runtime)
                                  | orchestrator.live_provider_session_ids(runtime)):
                    counts["skip_new_reference"] += 1
                    continue
                phase = "delete_orphan"
                codex_app_server.delete_session(server, session_id)
                counts["retired_orphan"] += 1
                deleted += 1
        except Exception as exc:
            outcome = "failed"
            host_errors.report_unexpected(
                "admin_api.codex_session_sweep", exc,
                context={
                    "runtime": runtime, "phase": phase, "thread_id": thread_id,
                    "session_id": session_id, **counts,
                },
            )
        finally:
            try:
                server.close()
            except Exception as exc:
                outcome = "failed"
                host_errors.report_unexpected(
                    "admin_api.codex_session_sweep.close", exc, context={"runtime": runtime},
                )
            retired_count = counts["retired_oversized"] + counts["retired_archived"] + counts["retired_orphan"]
            if outcome == "completed" and counts["measurement_failed"]:
                outcome = "partial"
            host_errors.report_info(
                "admin_api.codex_session_sweep",
                f"Codex cleanup {outcome} for {runtime}: "
                f"checked {counts['mapped_checked']}/{counts['mapped_candidates']} mapped sessions, "
                f"listed {counts['stored_roots']} roots, retired {retired_count}, "
                f"measurement failures {counts['measurement_failed']}",
                context={
                    "runtime": runtime, "outcome": outcome, "last_phase": phase,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "limit_bytes": codex_app_server.SESSION_ROLLOUT_MAX_BYTES,
                    "retired_roots": retired_count, **counts,
                },
            )
    return deleted
