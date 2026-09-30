"""Scheduled, operator-authorized review of pending tool approvals."""
from http import HTTPStatus
import random
import re
import threading
import time
from typing import Any

from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api import tools_client, auto_approval_prompt as auto_approval
from host.runtime.core import host_errors, state
from host.runtime.host_inference import client, json_contract
from host.runtime.tools import tools_host

next_review_at: int | None = None
_settings_changed = threading.Event()


def sleep_remaining(now: int, settings: dict[str, int]) -> int:
    start = settings["sleep_start_minute"] * 60
    duration = (settings["sleep_end_minute"] * 60 - start) % 86400
    elapsed = (now - start) % 86400
    return max(0, duration - elapsed)


def next_check(now: int, settings: dict[str, int]) -> int:
    due = now + random.randint(25 * 60, 35 * 60)
    remaining = sleep_remaining(due, settings)
    if remaining:
        due += remaining + random.randint(0, 5 * 60)
    return due


def save_settings(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != {"sleep_start_minute", "sleep_end_minute"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Sleep and wake times are required")
    start, end = body["sleep_start_minute"], body["sleep_end_minute"]
    if any(type(value) is not int or not 0 <= value < 1440 for value in (start, end)):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Sleep and wake times must be whole UTC minutes from 0 to 1439")
    if (end - start) % 1440 < 360:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Sleep must last at least 6 hours; sleep and wake times must differ")
    state.set_auto_approval_settings(start, end)
    _settings_changed.set()
    return {"saved": True}


def catalog() -> list[dict[str, Any]]:
    return [{"tool_id": tool.manifest.tool_id, "name": tool.manifest.display_name,
             "actions": [{"id": action.id, "description": action.description}
                         for action in tool.manifest.actions if action.approval == "operator"]}
            for tool in sorted(tools_host.BUNDLED_TOOLS.values(), key=lambda tool: tool.manifest.display_name)
            if any(action.approval == "operator" for action in tool.manifest.actions)]


def page(query: dict[str, list[str]]) -> dict[str, Any]:
    pages, outcomes = query.get("page", ["1"]), query.get("outcome", [""])
    if len(pages) != 1 or not re.fullmatch(r"[1-9][0-9]{0,5}", pages[0]):
        raise ApiError(HTTPStatus.BAD_REQUEST, "page must be a positive integer")
    if len(outcomes) != 1 or outcomes[0] not in {"", "approved", "left_pending", "no_policy", "failed"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "unknown review outcome")
    provider = state.host_inference_provider_metadata("openai")
    settings = state.auto_approval_settings()
    return {"settings": settings, "policies": state.auto_approval_policies(), "catalog": catalog(),
            "available": provider["enabled"] and provider["configured"],
            "next_review_at": next_review_at, "quiet": bool(sleep_remaining(int(time.time()), settings)),
            "history": state.auto_approval_history(int(pages[0]), outcomes[0])}


def policy_input(body: Any) -> tuple[str, str, str]:
    if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in ("tool_id", "action_id", "instructions")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "tool, action and instructions are required")
    tool = tools_host.BUNDLED_TOOLS.get(body["tool_id"])
    action = tool.manifest.action(body["action_id"]) if tool else None
    if action is None or action.approval != "operator":
        raise ApiError(HTTPStatus.BAD_REQUEST, "Choose an approval-gated tool action")
    instructions = body["instructions"].strip()
    if not 1 <= len(instructions) <= 8000:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Instructions must contain 1 to 8,000 characters")
    return body["tool_id"], body["action_id"], instructions


def save_policy(body: Any) -> dict[str, Any]:
    tool, action, instructions = policy_input(body)
    state.set_auto_approval_policy(tool, action, instructions)
    return {"saved": True}


def delete_policy(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in ("tool_id", "action_id")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "tool and action are required")
    state.set_auto_approval_policy(body["tool_id"], body["action_id"], None)
    return {"deleted": True}


def review(record: dict[str, Any], instructions: str) -> dict[str, Any]:
    tool = tools_host.BUNDLED_TOOLS.get(record["tool_id"])
    action = tool.manifest.action(record["action_id"]) if tool else None
    if action is None or action.approval != "operator":
        raise ValueError("This action is no longer available for auto-approval")
    request = {key: record[key] for key in ("tool_id", "action_id", "summary", "payload", "connection_id", "account_id", "account_label")}
    request.update(action_description=action.description, data_policy=action.data_policy)
    result = client.openai_text_completion(
        auto_approval.prompt(instructions, request), auto_approval.SCHEMA, "auto_approval",
        model=auto_approval.MODEL, instructions=auto_approval.INSTRUCTIONS, reasoning_effort="medium",
        max_output_tokens=4096, timeout_seconds=auto_approval.TIMEOUT_SECONDS,
    )
    if not json_contract.matches(result, auto_approval.SCHEMA) or not result["reason"].strip():
        raise ValueError("The reviewer returned an invalid decision")
    return result


def run_once() -> None:
    for record in state.pending_auto_approvals():
        now = int(time.time())
        if sleep_remaining(now, state.auto_approval_settings()):
            break
        # Read the current text at the time of each check, including deletions.
        policies = state.auto_approval_policies()
        policy = next((p["instructions"] for p in policies if (p["tool_id"], p["action_id"]) == (record["tool_id"], record["action_id"])), "")
        current = state.tool_approval(record["approval_id"])
        if not current or current["status"] != "pending":
            continue
        if not policy:
            state.save_auto_approval_review(record["approval_id"], now, "", "no_policy", "No policy found for this action.")
            continue
        provider = state.host_inference_provider_metadata("openai")
        if not provider["enabled"] or not provider["configured"]:
            reason = "OpenAI Host AI inference is not configured." if not provider["configured"] else "OpenAI Host AI inference is disabled."
            state.save_auto_approval_review(record["approval_id"], now, policy, "failed", reason)
            continue
        try:
            result = review(current, policy)
        except Exception as exc:
            if isinstance(exc, client.HostInferenceError) and exc.reason == "provider_disabled":
                state.save_auto_approval_review(
                    record["approval_id"], int(time.time()), policy, "failed",
                    "OpenAI Host AI inference is disabled or not configured.",
                )
                continue
            host_errors.report_warning("admin_api.auto_approval", exc, context={"approval_id": record["approval_id"]})
            reason = str(exc) if isinstance(exc, (client.HostInferenceError, ValueError)) else "Review failed."
            if isinstance(exc, client.HostInferenceError) and exc.reason == "input_too_large":
                reason = "Tool payload is too large for auto-review with this policy."
            state.save_auto_approval_review(record["approval_id"], int(time.time()), policy, "failed", reason, auto_approval.MODEL)
            if isinstance(exc, client.HostInferenceError):
                break
            continue
        outcome = "approved" if result["approve"] else "left_pending"
        reason = result["reason"]
        # Save the decision first. Its presence prevents any later batch from
        # picking up this request, even if admin stops before calling tools.
        state.save_auto_approval_review(
            record["approval_id"], int(time.time()), policy, outcome, reason, auto_approval.MODEL,
        )
        if outcome == "approved":
            try:
                tools_client.decide_tool_approval(record["approval_id"], "approve", record["tool_id"])
            except Exception as exc:
                host_errors.report_warning("admin_api.auto_approval_execute", exc, context={"approval_id": record["approval_id"]})
                state.save_auto_approval_error(record["approval_id"], str(exc)[:500])


def run() -> None:
    global next_review_at
    while True:
        _settings_changed.clear()
        try:
            next_review_at = next_check(int(time.time()), state.auto_approval_settings())
            if _settings_changed.wait(max(0, next_review_at - time.time())):
                continue
            run_once()
        except Exception as exc:
            next_review_at = None
            host_errors.report_warning("admin_api.auto_approval_run", exc)
            _settings_changed.wait(60)
