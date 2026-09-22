"""Fixed TypeSafe judgment contract for one current tool approval."""

from __future__ import annotations

import math
from typing import Any


RISK_SCORE_IDS = (
    "commits_money_or_obligation",
    "sensitive_data",
    "summary_mismatch",
)

APPROVAL_QUESTIONS: dict[str, dict[str, Any]] = {
    "commits_money_or_obligation": {
        "type": "noul",
        "instructions": "Would this action commit money, accept terms, place an order, create a subscription, or create another external obligation?",
    },
    "sensitive_data": {
        "type": "noul",
        "instructions": "Does the action payload contain or transmit personal, financial, secret, or credential-like information?",
    },
    "summary_mismatch": {
        "type": "noul",
        "instructions": "Does the human-readable summary fail to faithfully describe the material effect or contents of the action payload?",
    },
}


def approval_state(
    record: dict[str, Any],
    *,
    action_description: str,
    data_policy: str,
) -> dict[str, Any]:
    """Build the exact current approval state without parsing or clipping it."""
    return {
        "tool_id": record["tool_id"],
        "action_id": record["action_id"],
        "action_description": action_description,
        "data_policy": data_policy,
        "summary": record["summary"],
        "payload": record["payload"],
        "account_label": record.get("account_label", ""),
    }


def assessment_from_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not isinstance(result.get("model"), str):
        return None
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(APPROVAL_QUESTIONS):
        return None
    scores: dict[str, float] = {}
    for score_id in RISK_SCORE_IDS:
        answer = answers.get(score_id)
        probability = answer.get("noul") if isinstance(answer, dict) else None
        if (
            not isinstance(probability, (int, float))
            or isinstance(probability, bool)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            return None
        scores[score_id] = float(probability)
    return {
        "model": result["model"],
        "scores": scores,
    }
