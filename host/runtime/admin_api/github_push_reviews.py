"""Immediate admin-side review of held pushes; independent of tool quiet hours."""
import time
from typing import Any, Callable

from host.network_integrations.github.push_gate import pending
from host.runtime.admin_api import auto_approval_prompt, github_push_prompt
from host.runtime.core import host_errors, state
from host.runtime.host_inference import client, json_contract


def review(record: dict[str, Any]) -> dict[str, Any]:
    evidence = pending.review_context(record)
    result = client.openai_text_completion(
        auto_approval_prompt.prompt(github_push_prompt.POLICY, evidence),
        auto_approval_prompt.SCHEMA, "github_push_review", model=auto_approval_prompt.MODEL,
        instructions=auto_approval_prompt.INSTRUCTIONS, reasoning_effort="medium",
        max_output_tokens=4096, timeout_seconds=auto_approval_prompt.TIMEOUT_SECONDS,
    )
    if not json_contract.matches(result, auto_approval_prompt.SCHEMA) or not result["reason"].strip():
        raise ValueError("The reviewer returned an invalid decision")
    return result


def run_once(resolve: Callable[[str, str], Any]) -> None:
    for record in state.pending_push_auto_approvals():
        push_id = record["id"]
        current = state.get_pending_push(push_id)
        if not current or current["status"] != "pending":
            continue
        policy = next((p for p in state.auto_approval_policies()
                       if (p["tool_id"], p["action_id"]) == (github_push_prompt.TOOL_ID, github_push_prompt.ACTION_ID)), None)
        if policy is None:
            state.save_push_auto_approval_review(push_id, int(time.time()), "", "no_policy", "No policy found for GitHub pushes.")
            continue
        provider = state.host_inference_provider_metadata("openai")
        github = (state.network_policy_record() or {}).get("controls", {}).get("network_integrations", {}).get("github", {})
        reason = ""
        if not github.get("enabled") or not github.get("require_dot_github_approval"):
            reason = "The GitHub .github/ push approval gate is disabled."
        elif not provider["enabled"] or not provider["configured"]:
            reason = "OpenAI Host AI inference is disabled or not configured."
        if reason:
            state.save_push_auto_approval_review(push_id, int(time.time()), github_push_prompt.POLICY, "failed", reason)
            continue
        try:
            result = review(current)
        except Exception as exc:
            host_errors.report_warning("admin_api.github_push_review", exc, context={"push_id": push_id})
            reason = str(exc) if isinstance(exc, (client.HostInferenceError, pending.HelperError, ValueError)) else "Push review failed."
            if isinstance(exc, client.HostInferenceError) and exc.reason == "input_too_large":
                reason = "The complete .github/ changes are too large for automatic review."
            state.save_push_auto_approval_review(push_id, int(time.time()), github_push_prompt.POLICY, "failed", reason, auto_approval_prompt.MODEL)
            continue
        # Like tool reviews, an audit consumes this check. Never release before saving it.
        state.save_push_auto_approval_review(
            push_id, int(time.time()), github_push_prompt.POLICY,
            "approved" if result["approve"] else "left_pending", result["reason"], auto_approval_prompt.MODEL,
        )
        if result["approve"]:
            try:
                resolve(push_id, "approve")
            except Exception as exc:
                host_errors.report_warning("admin_api.github_push_release", exc, context={"push_id": push_id})
                state.save_push_auto_approval_error(push_id, str(exc)[:500])


def run(resolve: Callable[[str, str], Any]) -> None:
    # The durable queue is shared with the proxy. No proxy inference, extra RPC,
    # or dependence on operator UI traffic; restart also finds unchecked pushes.
    while True:
        try:
            run_once(resolve)
        except Exception as exc:
            host_errors.report_warning("admin_api.github_push_reviews", exc)
        time.sleep(1)
