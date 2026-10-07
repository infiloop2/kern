"""Daily Codex size, archive and unreferenced-root reconciliation."""

from __future__ import annotations

from pathlib import PurePosixPath
import time
from typing import Any

from host.runtime.agent_runtime import codex_app_server, orchestrator
from host.runtime.core import host_errors, state

ORPHAN_GRACE_SECONDS = 7 * 24 * 3600


def _eligible(thread: dict[str, Any], runtime: str, before: float) -> bool:
    # Originator is recorded by Codex at creation, independent of this listing
    # client's originator. Do not touch standalone CLI or third-party sessions.
    if (thread.get("originator") != "kern-host"
        or thread.get("cwd") != codex_app_server.AGENT_CWD
        or thread.get("parentThreadId") is not None):
        return False
    updated = thread.get("updatedAt")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not 0 < updated <= before:
        return False
    status = thread.get("status")
    if not isinstance(status, dict) or status.get("type") not in {"notLoaded", "idle", "systemError"}:
        return False
    if thread.get("ephemeral") is not False:
        return False
    path = thread.get("path")
    if not isinstance(path, str):
        return False
    rollout = PurePosixPath(path)
    home = PurePosixPath(codex_app_server.AGENT_CWD) / f".{runtime}"
    return (
        ".." not in rollout.parts
        and any(rollout.is_relative_to(home / directory) for directory in ("sessions", "archived_sessions"))
        and rollout.name.endswith(f"-{thread['id']}.jsonl")
    )


def reconcile_codex_sessions() -> int:
    """Apply size, archive and unreferenced-root cleanup in one daily pass.

    A seven-day inactivity grace protects sessions being created or handed off.
    Running/FINISHING turns are skipped; archive state is rechecked at detach.
    Provider logs are deleted by UUID, never by unlinking rollouts or databases.
    """
    deleted = 0
    before = time.time() - ORPHAN_GRACE_SECONDS
    for runtime in codex_app_server.CODEX_RUNTIME_TYPES:
        server = codex_app_server.CodexAppServer(runtime_type=runtime)
        try:
            server.start(init_timeout=5)
            for thread_id, session_id, archived in state.codex_cleanup_candidates(runtime):
                if thread_id in orchestrator.live_thread_ids():
                    continue
                if not archived:
                    try:
                        size = codex_app_server.session_rollout_size(server, session_id)
                    except codex_app_server.CodexAppServerError as exc:
                        if str(exc) != f"no rollout found for thread id {session_id}":
                            raise
                        # Normal resume recovery owns missing mapped sessions.
                        continue
                    if size < codex_app_server.SESSION_ROLLOUT_MAX_BYTES:
                        continue
                with state.mutation() as cur:
                    # Admission publishes its live fence under this same lock.
                    # Recheck after measurement, including FINISHING turns.
                    if thread_id in orchestrator.live_thread_ids():
                        continue
                    detached = state.detach_idle_codex_session(
                        cur, thread_id, runtime, session_id, archived=archived,
                    )
                if detached:
                    # Commit detach before deletion so the next send starts fresh.
                    codex_app_server.delete_session(server, session_id)
                    deleted += 1
            sessions = codex_app_server.stored_sessions(server)
            referenced = state.referenced_provider_session_ids(runtime) | orchestrator.live_provider_session_ids(runtime)
            for thread in sessions:
                session_id = thread["id"]
                if session_id in referenced or not _eligible(thread, runtime, before):
                    continue
                try:
                    fresh = server.call("thread/read", {
                        "threadId": session_id, "includeTurns": False,
                    }, timeout=5)["thread"]
                except codex_app_server.CodexAppServerError as exc:
                    if str(exc) != f"no rollout found for thread id {session_id}":
                        raise
                    # A concurrent successful deletion is harmless. Keep using
                    # the supported deletion path to reconcile native metadata.
                    fresh = thread
                if not isinstance(fresh, dict) or fresh.get("id") != session_id:
                    raise codex_app_server.CodexAppServerError("Codex returned mismatched orphan metadata")
                if not _eligible(fresh, runtime, before):
                    continue
                # Refresh host ownership immediately before deletion; snapshots
                # must not override a restore/send or an in-flight session id.
                if session_id in (state.referenced_provider_session_ids(runtime)
                                  | orchestrator.live_provider_session_ids(runtime)):
                    continue
                codex_app_server.delete_session(server, session_id)
                deleted += 1
        except Exception as exc:
            # Stop this account after any provider failure; the next daily pass
            # rediscovers remaining logs, including a failed post-detach delete.
            host_errors.report_unexpected("admin_api.codex_session_sweep", exc, context={"runtime": runtime})
        finally:
            try:
                server.close()
            except Exception as exc:
                host_errors.report_unexpected("admin_api.codex_session_sweep.close", exc, context={"runtime": runtime})
    return deleted
