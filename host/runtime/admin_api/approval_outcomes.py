"""Deliver an approval outcome with its typed transcript notice."""

from typing import Any

from host.agent_messages import approval_message, approval_notice
from host.runtime.admin_api import workspace_proxy
from host.runtime.core import host_errors

TERMINAL_STATUSES = frozenset({"executed", "failed", "denied"})


def notify(record: dict[str, Any]) -> None:
    thread_id = record.get("origin_thread_id")
    if not thread_id or record.get("status") not in TERMINAL_STATUSES:
        return
    try:
        workspace_proxy.send_message(thread_id, approval_message(record), notice=approval_notice(record))
    except Exception as exc:
        # Notification failure must not obscure the completed approval or
        # invite re-execution. Same one-shot semantics as agent messages.
        host_errors.report_warning("admin_api.approval_outcome", exc,
                                   context={"thread_id": thread_id})


def notify_push(push: dict[str, Any]) -> None:
    if not push.get("origin_thread_id"):
        return
    status = {"approved": "executed", "rejected": "denied", "failed": "failed"}.get(push["status"])
    if status is None:
        return
    notify({
        "origin_thread_id": push.get("origin_thread_id"),
        "approval_id": f"push-{push['id']}",
        "summary": "Git push",
        "status": status,
        "result": push.get("detail") or ("The push failed." if status == "failed" else ""),
    })
