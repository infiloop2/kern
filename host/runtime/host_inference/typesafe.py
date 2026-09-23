"""Bounded TypeSafe Jev adapter for host-owned typed judgments."""

from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
from typing import Any

from host.runtime.host_inference import provider_http, redaction


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_TIMEOUT_SECONDS = 2.0
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
RESPONSE_MODEL_RE = re.compile(r"^jev-(?:latest|[1-9][0-9]*\.[0-9]+\.[0-9]+)$")
QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
Transport = Callable[..., bytes]
UsageRecorder = Callable[[str, Any | None], None]


class JudgmentResponseError(ValueError):
    pass


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _request_bytes(method: str, url: str, **kwargs: Any) -> bytes:
    timeout = float(kwargs.pop("timeout"))
    max_bytes = int(kwargs.pop("max_bytes"))
    headers = kwargs.pop("headers", {})
    data = kwargs.pop("data", None)
    kwargs.pop("failure_message", None)
    if method != "POST" or url != ENDPOINT or kwargs:
        raise ValueError("TypeSafe transport request is invalid")
    return provider_http.post(
        ENDPOINT,
        headers=headers,
        data=data,
        timeout=timeout,
        max_bytes=max_bytes,
        label="TypeSafe judgment",
    )


def _valid_probability(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def _validate_questions(questions: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 64:
        raise ValueError("judgment questions must be a map of 1 to 64 entries")
    validated: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        if not isinstance(question_id, str) or QUESTION_ID_RE.fullmatch(question_id) is None:
            raise ValueError("judgment question id is invalid")
        if not isinstance(question, dict):
            raise ValueError("judgment question must be an object")
        if (
            set(question) != {"type", "instructions"}
            or question["type"] != "noul"
            or not isinstance(question["instructions"], str)
            or not question["instructions"]
        ):
            raise ValueError("judgment question must contain noul instructions")
        validated[question_id] = question
    return validated


def _valid_answer(answer: Any) -> bool:
    return (
        isinstance(answer, dict)
        and set(answer) == {"type", "noul"}
        and answer["type"] == "noul"
        and _valid_probability(answer["noul"])
    )


def judge(
    *,
    api_key: str,
    model: str,
    state: Any,
    questions: dict[str, dict[str, Any]],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Transport = _request_bytes,
    usage_recorder: UsageRecorder | None = None,
) -> dict[str, Any]:
    """Return a validated Jev result or raise a bounded adapter error."""
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("TypeSafe API key is missing")
    if model != "jev-latest":
        raise ValueError("TypeSafe model must be jev-latest")
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= DEFAULT_TIMEOUT_SECONDS:
        raise ValueError("judgment timeout is invalid")
    questions = _validate_questions(questions)
    safe_questions = {
        question_id: {
            "type": "noul",
            "instructions": redaction.redact_text(question["instructions"]),
        }
        for question_id, question in questions.items()
    }
    body = {
        "model": model,
        "state": redaction.redact_content(state),
        "questions": safe_questions,
    }
    encoded = _json_bytes(body)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("TypeSafe judgment request is too large")
    raw = transport(
        "POST",
        ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=encoded,
        failure_message="TypeSafe judgment failed.",
        timeout=timeout_seconds,
        max_bytes=MAX_RESPONSE_BYTES,
    )
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if usage_recorder is not None:
            usage_recorder(model, None)
        raise JudgmentResponseError("TypeSafe returned an invalid judgment response") from exc
    if usage_recorder is not None:
        usage_recorder(model, response)
    try:
        response_model = response["model"]
        answers = response["answers"]
    except (KeyError, TypeError) as exc:
        raise JudgmentResponseError("TypeSafe returned an invalid judgment response") from exc
    if (
        not isinstance(response_model, str)
        or RESPONSE_MODEL_RE.fullmatch(response_model) is None
        or not isinstance(answers, dict)
        or set(answers) != set(safe_questions)
        or any(not _valid_answer(answer) for answer in answers.values())
    ):
        raise JudgmentResponseError("TypeSafe response did not match the requested questions")
    return {"model": response_model, "answers": answers}
