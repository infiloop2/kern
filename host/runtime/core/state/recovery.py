"""Durable recovery state. Writes participate in the thread lifecycle transaction."""
from __future__ import annotations

from typing import Any

from host.runtime.core.state._base import _read

_PENDING_COLUMNS = (
    "retry_id", "retry_kind", "error_message", "due_at", "source_runtime",
    "source_model", "source_effort", "source_run_number", "target_runtime",
)
_READ_COLUMNS = ("attempts, retry_id, retry_kind, error_message,"
                 " EXTRACT(EPOCH FROM due_at)::double precision, source_runtime,"
                 " source_model, source_effort, source_run_number, target_runtime")


def _recovery_row(thread_id: str, row: Any) -> dict[str, Any]:
    if row is None:
        return {"attempts": 0, "pending": None}
    attempts = int(row[0])
    pending = None if row[1] is None else {
        "thread_id": thread_id, "attempt": attempts + 1, "id": str(row[1]), "kind": str(row[2]),
        "error": str(row[3]), "due_at": float(row[4]), "runtime": str(row[5]),
        "model": str(row[6]), "effort": str(row[7]), "run_number": int(row[8]),
        "target_runtime": str(row[9]),
    }
    return {"attempts": attempts, "pending": pending}


def thread_recovery(thread_id: str, cur: Any = None) -> dict[str, Any]:
    with _read(cur) as cur:
        cur.execute(f"SELECT {_READ_COLUMNS} FROM thread_recovery WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
    return _recovery_row(thread_id, row)


def save_thread_recovery(cur: Any, thread_id: str, attempts: int, pending: dict[str, Any] | None) -> None:
    values = tuple(pending[key] for key in (
        "id", "kind", "error", "due_at", "runtime", "model", "effort", "run_number", "target_runtime"
    )) if pending else (None,) * len(_PENDING_COLUMNS)
    cur.execute(
        f"INSERT INTO thread_recovery (thread_id, attempts, {', '.join(_PENDING_COLUMNS)})"
        " VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)"
        " ON CONFLICT (thread_id) DO UPDATE SET attempts = EXCLUDED.attempts, "
        + ", ".join(f"{column} = EXCLUDED.{column}" for column in _PENDING_COLUMNS),
        (thread_id, attempts, *values),
    )


def pending_thread_recoveries() -> list[tuple[str, dict[str, Any]]]:
    with _read() as cur:
        cur.execute(f"SELECT thread_id, {_READ_COLUMNS} FROM thread_recovery WHERE retry_id IS NOT NULL ORDER BY due_at, thread_id")
        return [(str(row[0]), _recovery_row(str(row[0]), row[1:])["pending"]) for row in cur.fetchall()]


def cancel_thread_recovery(cur: Any, thread_id: str) -> bool:
    cur.execute(
        "UPDATE thread_recovery SET " + ", ".join(f"{column} = NULL" for column in _PENDING_COLUMNS)
        + " WHERE thread_id = %s AND retry_id IS NOT NULL RETURNING 1",
        (thread_id,),
    )
    return cur.fetchone() is not None


def product_session_settings(cur: Any, thread_id: str) -> tuple[str, str, str] | None:
    if thread_id.startswith("app-"):
        cur.execute("SELECT agent_runtime, agent_model, agent_effort FROM web_apps WHERE app_id = %s", (thread_id,))
    elif thread_id.startswith("schedule-"):
        cur.execute("SELECT agent_runtime, model, effort FROM schedules WHERE thread_id = %s", (thread_id,))
    else:
        return None
    row = cur.fetchone()
    return (str(row[0]), str(row[1]), str(row[2])) if row else None


def recovery_destination(cur: Any, thread_id: str, runtime: str, model: str, effort: str,
                         target_runtime: str, now: str) -> None:
    """Check eligibility and persist a subscription switch with admission.

    NOWAIT avoids a lock cycle with Workspace sends holding a destination row
    while awaiting Admin admission. A racing edit/send stops this recovery.
    """
    if thread_id.startswith("app-"):
        cur.execute(
            "SELECT agent_runtime, agent_model, agent_effort, archived, agent_updates_locked"
            " FROM web_apps WHERE app_id = %s FOR UPDATE NOWAIT", (thread_id,),
        )
        row = cur.fetchone()
        if row is None or row[3] or row[4] or tuple(row[:3]) != (runtime, model, effort):
            raise ValueError("App is archived, locked, or its settings changed")
        if target_runtime != runtime:
            cur.execute("UPDATE web_apps SET agent_runtime = %s, updated_at = %s WHERE app_id = %s",
                        (target_runtime, now, thread_id))
    elif thread_id.startswith("schedule-"):
        cur.execute(
            "SELECT agent_runtime, model, effort, deleted_at FROM schedules"
            " WHERE thread_id = %s FOR UPDATE NOWAIT", (thread_id,),
        )
        row = cur.fetchone()
        if row is None or row[3] is not None or tuple(row[:3]) != (runtime, model, effort):
            raise ValueError("Standing agent is deleted or its settings changed")
        if target_runtime != runtime:
            cur.execute(
                "UPDATE schedules SET agent_runtime = %s, revision = revision + 1, updated_at = %s"
                " WHERE thread_id = %s", (target_runtime, now, thread_id),
            )
            cur.execute(
                "INSERT INTO schedule_revisions (schedule_id, revision, name, triggers, agent_runtime,"
                " model, effort, deleted, actor, created_at, purpose)"
                " SELECT id, revision, name, triggers, agent_runtime, model, effort, FALSE, 'agent', %s, purpose"
                " FROM schedules WHERE thread_id = %s", (now, thread_id),
            )
            # Match the Standing editor's 100-revision retention.
            cur.execute(
                "DELETE FROM schedule_revisions WHERE id IN (SELECT history.id"
                " FROM schedule_revisions AS history JOIN schedules ON schedules.id = history.schedule_id"
                " WHERE schedules.thread_id = %s ORDER BY history.id DESC OFFSET 100)", (thread_id,),
            )
    else:
        cur.execute("SELECT archived FROM chat_threads WHERE thread_id = %s FOR SHARE NOWAIT", (thread_id,))
        row = cur.fetchone()
        if row is None or row[0]:
            raise ValueError("Chat is archived or missing")
