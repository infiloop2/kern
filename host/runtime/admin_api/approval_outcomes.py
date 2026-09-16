"""Send one ordinary thread message after an operator decides an approval."""

from typing import Any

from host.agent_scripts import AUTOMATED_TRIGGER_PREFIX
from host.runtime.admin_api import workspace_proxy
from host.runtime.core import host_errors

TERMINAL_STATUSES = frozenset({"executed", "failed", "denied"})


def outcome_message(record: dict[str, Any]) -> str:
    outcome = {
        "executed": "was approved and executed successfully.",
        "failed": "was approved, but execution failed.",
        "denied": "was denied.",
    }[record["status"]]
    message = AUTOMATED_TRIGGER_PREFIX + f"Approval ID: {record['approval_id']} {outcome}"
    if record["status"] in {"executed", "failed"} and record["result"]:
        label = "Error" if record["status"] == "failed" else "Result"
        message += f"\n\n{label}: {record['result'][:4000]}"
    return message


def notify(record: dict[str, Any]) -> None:
    thread_id = record.get("origin_thread_id")
    if not thread_id or record.get("status") not in TERMINAL_STATUSES:
        return
    try:
        workspace_proxy.send_message(thread_id, outcome_message(record))
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
        "status": status,
        "result": push.get("detail") or ("The push failed." if status == "failed" else ""),
    })
