"""Durable bounded recovery. Provider probes run outside lifecycle mutations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from host.runtime.agent_runtime import recovery_policy
from host.runtime.core import host_errors, state

DELAYS = recovery_policy.DELAYS


@dataclass(frozen=True)
class Retry:
    thread_id: str
    attempt: int
    error: str
    due_at: float
    id: str
    kind: str
    runtime: str
    model: str
    effort: str
    run_number: int
    target_runtime: str


def notice(cur: Any, thread_id: str, summary: str, details: dict[str, Any]) -> None:
    state.append_agent_event(cur, "thread.notice", thread_id, {"notice": {
        "kind": "recovery", "summary": summary[:100], "details": json.dumps(details),
    }})


def record_outcome(cur: Any, thread_id: str, runtime: str, model: str, effort: str,
                   run_number: int, error: str | None, *, restart: bool = False) -> Retry | None:
    """Save known deadlines; return a one-shot, in-memory limit check if needed."""
    attempts = state.thread_recovery(thread_id, cur)["attempts"]
    if error is None:
        state.save_thread_recovery(cur, thread_id, 0, None)
        return None
    kind = "restart" if restart and runtime != "script" else recovery_policy.classify(runtime, error)
    retry = None
    if kind is not None and attempts < len(DELAYS):
        retry = Retry(thread_id, attempts + 1, error[:16000],
                      time.time() + (DELAYS[attempts] if kind == "transient" else 0),
                      str(uuid4()), kind, runtime, model, effort, run_number, runtime)
    if retry and kind == "limit":
        state.save_thread_recovery(cur, thread_id, attempts, None)
        return retry
    state.save_thread_recovery(cur, thread_id, attempts, asdict(retry) if retry else None)
    if runtime != "script":
        notice(cur, thread_id,
               "Automatic recovery scheduled" if retry else "Automatic recovery stopped",
               {"classification": kind or "unknown", "attempt": attempts + 1 if retry else attempts,
                "at": _at(retry.due_at) if retry else None,
                "reason": "attempt limit reached" if attempts >= len(DELAYS) else
                          "unrecognized error" if kind is None else kind})
    return None


def start_limit_check(retry: Retry) -> None:
    # No saved checking state, polling or retries for this probe. If the host
    # exits before it produces a decision, there is no recovery to restore.
    try:
        threading.Thread(target=_check_limit, args=(retry,), name="recovery-usage", daemon=True).start()
    except Exception as exc:
        host_errors.report_warning("agent_runtime.recovery_usage", exc, context={"thread_id": retry.thread_id})
        with state.mutation() as cur:
            notice(cur, retry.thread_id, "Automatic recovery stopped", {
                "classification": "limit", "attempt": retry.attempt,
                "reason": "usage check could not start",
            })


def _check_limit(retry: Retry) -> None:
    try:
        resolve(retry)
    except Exception as exc:
        host_errors.report_warning("agent_runtime.recovery_usage", exc, context={"thread_id": retry.thread_id})


def _at(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def current(thread_id: str, cur: Any = None) -> Retry | None:
    value = state.thread_recovery(thread_id, cur)["pending"]
    return Retry(**value) if value else None


def cancel(thread_id: str, cur: Any = None) -> bool:
    if cur is not None:
        return state.cancel_thread_recovery(cur, thread_id)
    with state.mutation() as cur:
        return state.cancel_thread_recovery(cur, thread_id)


def pending(thread_id: str) -> dict[str, Any] | None:
    retry = current(thread_id)
    return {"attempt": retry.attempt, "at": _at(retry.due_at), "kind": retry.kind,
            "target_runtime": retry.target_runtime} if retry else None


def claim(cur: Any, retry: Retry) -> None:
    """Consume only inside the transaction admitting the new model turn."""
    if current(retry.thread_id, cur) != retry:
        raise ValueError("automatic recovery was cancelled or replaced")
    state.save_thread_recovery(cur, retry.thread_id, retry.attempt, None)


def stop(retry: Retry, reason: str) -> None:
    with state.mutation() as cur:
        if current(retry.thread_id, cur) != retry:
            return
        state.cancel_thread_recovery(cur, retry.thread_id)
        notice(cur, retry.thread_id, "Automatic recovery stopped",
               {"classification": retry.kind, "reason": reason, "attempt": retry.attempt})


def resolve(retry: Retry) -> Retry | None:
    try:
        decision = recovery_policy.limit_recovery(retry.runtime, retry.model, retry.effort)
    except Exception:
        decision = None
    with state.mutation() as cur:
        config = state.thread_session_config(retry.thread_id, cur)
        saved = state.thread_recovery(retry.thread_id, cur)
        # Best effort: discard a result if newer work or settings superseded it.
        if (config is None or config["status"] != "idle" or config["run_number"] != retry.run_number
            or (config["agent_runtime"], config["model"], config["effort"])
               != (retry.runtime, retry.model, retry.effort)
            or saved["attempts"] != retry.attempt - 1 or saved["pending"] is not None):
            return None
        if decision is None:
            notice(cur, retry.thread_id, "Automatic recovery stopped", {
                "classification": "limit", "attempt": retry.attempt,
                "reason": "usage check failed, was inconclusive, or supplied no usable reset time",
            })
            return None
        target, due_at = decision
        resolved = replace(retry, target_runtime=target, due_at=due_at)
        state.save_thread_recovery(cur, retry.thread_id, saved["attempts"], asdict(resolved))
        notice(cur, retry.thread_id, "Automatic recovery scheduled", {
            "classification": retry.kind, "attempt": retry.attempt, "at": _at(due_at),
            "from_runtime": retry.runtime, "target_runtime": target,
        })
    return resolved


def deliver_due(deliver: Callable[[Retry], None]) -> None:
    from host.runtime.agent_runtime import orchestrator
    for _, value in state.pending_thread_recoveries():
        retry = Retry(**value)
        if retry.due_at > time.time() or retry.thread_id in orchestrator.live_thread_ids():
            continue
        # Boot status starts at loading. Let the ordinary status poller finish
        # before consuming a restored retry.
        if orchestrator.runtime_status(retry.target_runtime) == "loading":
            continue
        try:
            deliver(retry)
        except Exception as exc:
            if retry is not None:
                stop(retry, "recovery check or delivery failed")
                host_errors.report_warning("agent_runtime.turn_retry", exc, context={"thread_id": retry.thread_id})


def run(deliver: Callable[[Retry], None]) -> None:
    while True:
        time.sleep(15)
        try:
            deliver_due(deliver)
        except Exception as exc:
            host_errors.report_warning("agent_runtime.turn_retries", exc)
