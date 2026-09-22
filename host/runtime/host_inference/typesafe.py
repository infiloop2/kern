"""Bounded TypeSafe Jev adapter for host-owned typed judgments."""

from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
from typing import Any

from host.runtime.host_inference import provider_http


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
        kind = question.get("type")
        instructions = question.get("instructions")
        if kind not in {"noul", "choice", "score"} or not isinstance(instructions, str) or not instructions:
            raise ValueError("judgment question type or instructions are invalid")
        allowed = {"type", "instructions", "criteria"}
        if set(question) - allowed:
            raise ValueError("judgment question contains unsupported fields")
        criteria = question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
                raise ValueError("choice criteria must contain 2 to 255 options")
            if any(not isinstance(key, str) or not key or value is not None and not isinstance(value, str)
                   for key, value in criteria.items()):
                raise ValueError("choice criteria are invalid")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or any(
                not isinstance(level, str) or not level for level in criteria
            ):
                raise ValueError("score criteria must contain 2 to 10 text levels")
        elif criteria is not None:
            if not isinstance(criteria, dict) or set(criteria) != {"true", "false"} or any(
                not isinstance(criteria[key], str) or not criteria[key] for key in ("true", "false")
            ):
                raise ValueError("noul criteria must describe true and false")
        validated[question_id] = question
    return validated


def _valid_distribution(value: Any, expected: set[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == expected
        and all(_valid_probability(item) for item in value.values())
        and math.isclose(sum(float(item) for item in value.values()), 1.0, abs_tol=0.02)
    )


def _validate_answer(answer: Any, question: dict[str, Any]) -> bool:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        return False
    if question["type"] == "noul":
        return set(answer) == {"type", "noul"} and _valid_probability(answer.get("noul"))
    confidence = answer.get("confidence")
    if not _valid_probability(confidence):
        return False
    if question["type"] == "choice":
        options = set(question["criteria"])
        return (
            set(answer) == {"type", "choice", "probabilities", "confidence"}
            and answer.get("choice") in options
            and _valid_distribution(answer.get("probabilities"), options)
        )
    level_keys = {str(index) for index in range(len(question["criteria"]))}
    legend = answer.get("legend")
    score = answer.get("score")
    return (
        set(answer) == {"type", "score", "score", "legend", "probabilities", "confidence"}
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(score)
        and 0 <= score <= len(level_keys) - 1
        and isinstance(legend, dict)
        and legend == {str(index): level for index, level in enumerate(question["criteria"])}
        and _valid_distribution(answer.get("probabilities"), level_keys)
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
    body = {"model": model, "state": state, "questions": questions}
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
        or set(answers) != set(questions)
        or any(not _validate_answer(answers[key], question) for key, question in questions.items())
    ):
        raise JudgmentResponseError("TypeSafe response did not match the requested questions")
    return {"model": response_model, "answers": answers}
