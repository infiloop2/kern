"""Bounded OpenAI Decisions adapter for text relevance predicates."""

from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
from typing import Any

from host.runtime.host_inference import redaction


ENDPOINT = "https://api.openai.com/v1/decisions"
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_INPUT_BYTES = 48 * 1024
QUESTION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
RESPONSE_MODEL_RE = re.compile(r"^gpt-6-luna(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?$")
Transport = Callable[..., bytes]
UsageRecorder = Callable[[str, Any | None], None]


class DecisionResponseError(ValueError):
    pass


def validate_questions(questions: Any) -> list[dict[str, Any]]:
    if not isinstance(questions, list) or not 1 <= len(questions) <= 64:
        raise ValueError("decision questions must be a list of 1 to 64 predicates")
    names: set[str] = set()
    for question in questions:
        if (not isinstance(question, dict)
                or set(question) != {"type", "name", "instructions"}
                or question["type"] != "predicate"
                or not isinstance(question["name"], str)
                or QUESTION_NAME_RE.fullmatch(question["name"]) is None
                or question["name"] in names
                or not isinstance(question["instructions"], str)
                or not question["instructions"]):
            raise ValueError("decision question must contain unique named predicate instructions")
        names.add(question["name"])
    return questions


def evaluate(
    *, api_key: str, model: str, input: str, questions: list[dict[str, Any]],
    transport: Transport, usage_recorder: UsageRecorder | None = None,
) -> dict[str, Any]:
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("OpenAI API key is missing")
    if model != "gpt-6-luna":
        raise ValueError("decision model must be gpt-6-luna")
    if not isinstance(input, str) or not input or len(input.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("decision input is empty or too large")
    questions = validate_questions(questions)
    body = {
        "model": model,
        "input": redaction.redact_text(input),
        "questions": [{**q, "instructions": redaction.redact_text(q["instructions"])} for q in questions],
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("decision request is too large")
    raw = transport(
        ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=encoded, label="OpenAI Decisions", max_bytes=MAX_RESPONSE_BYTES,
    )
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if usage_recorder is not None:
            usage_recorder(model, None)
        raise DecisionResponseError("OpenAI returned an invalid decision response") from exc
    if usage_recorder is not None:
        usage_recorder(model, response)
    if (not isinstance(response, dict)
            or not isinstance(response.get("model"), str)
            or RESPONSE_MODEL_RE.fullmatch(response["model"]) is None
            or not isinstance(response.get("answers"), list)
            or len(response["answers"]) != len(questions)):
        raise DecisionResponseError("OpenAI response did not match the requested decisions")
    for answer, question in zip(response["answers"], questions):
        if not isinstance(answer, dict) or answer.get("name") != question["name"]:
            raise DecisionResponseError("OpenAI response did not match the requested decisions")
        if answer.get("type") == "refusal" and set(answer) == {"type", "name"}:
            continue
        value = answer.get("probability")
        if (set(answer) != {"type", "name", "probability"} or answer["type"] != "predicate"
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 1):
            raise DecisionResponseError("OpenAI response did not match the requested decisions")
    return {"model": response["model"], "answers": response["answers"]}
