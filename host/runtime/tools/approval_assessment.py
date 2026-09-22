"""One best-effort TypeSafe risk annotation for a new tool approval."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any

from host.runtime.core import host_errors, state
from host.runtime.host_inference import approval_risk, client


_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="approval-risk")
_QUEUE_SLOTS = threading.BoundedSemaphore(16)
Judge = Callable[..., dict[str, Any]]


def assess(
    record: dict[str, Any],
    *,
    action_description: str,
    data_policy: str,
    judge: Judge = client.typesafe_jev_judgment,
) -> bool:
    """Call Jev once and save its annotation if the approval is still pending."""
    try:
        current = state.tool_approval(record["approval_id"], record.get("tool_id"))
        if current is None or current.get("status") != "pending":
            return False
        request_state = approval_risk.approval_state(
            current,
            action_description=action_description,
            data_policy=data_policy,
        )
        # Recheck the decision at the last practical point before egress; no
        # lock or retry is needed for this best-effort annotation.
        current = state.tool_approval(record["approval_id"], record.get("tool_id"))
        if current is None or current.get("status") != "pending":
            return False
        result = judge(
            request_state,
            approval_risk.APPROVAL_QUESTIONS,
            timeout_seconds=2.0,
        )
        assessment = approval_risk.assessment_from_result(result)
        if assessment is None:
            raise ValueError("TypeSafe returned an unusable approval assessment")
        return state.save_tool_approval_risk_assessment(
            current["approval_id"], assessed_at=int(time.time()), **assessment
        )
    except client.HostInferenceError:
        # The socket client or provider service already recorded this failure.
        return False
    except Exception as exc:
        host_errors.report_warning(
            "tools.approval_assessment",
            exc,
            context={
                "tool_id": record.get("tool_id", ""),
                "action_id": record.get("action_id", ""),
            },
            kind="approval_assessment_failed",
        )
        return False


def schedule(
    record: dict[str, Any],
    *,
    action_description: str,
    data_policy: str,
    judge: Judge = client.typesafe_jev_judgment,
) -> bool:
    """Queue one bounded attempt without delaying the approval response."""
    try:
        if not state.host_inference_provider_is_enabled("typesafe"):
            return False
    except Exception as exc:
        host_errors.report_warning(
            "tools.approval_assessment",
            exc,
            context={"phase": "provider_state"},
            kind="approval_assessment_failed",
        )
        return False
    if not _QUEUE_SLOTS.acquire(blocking=False):
        host_errors.report_warning(
            "tools.approval_assessment",
            RuntimeError("approval assessment queue is full"),
            context={"phase": "queue"},
            kind="approval_assessment_failed",
        )
        return False

    def run() -> None:
        try:
            assess(
                record,
                action_description=action_description,
                data_policy=data_policy,
                judge=judge,
            )
        finally:
            _QUEUE_SLOTS.release()

    try:
        _EXECUTOR.submit(run)
    except RuntimeError as exc:
        _QUEUE_SLOTS.release()
        host_errors.report_warning(
            "tools.approval_assessment",
            exc,
            context={"phase": "queue"},
            kind="approval_assessment_failed",
        )
        return False
    return True
