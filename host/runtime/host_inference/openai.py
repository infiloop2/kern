"""Bounded OpenAI adapter for host-owned text completion."""

from __future__ import annotations

from collections.abc import Callable
import json
import re
from typing import Any

from host.runtime.host_inference import json_contract, provider_http

ENDPOINT = "https://api.openai.com/v1/chat/completions"
TIMEOUT_SECONDS = 20
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 48 * 1024
MAX_SCHEMA_BYTES = 12 * 1024
MAX_OUTPUT_TOKENS = 400
SCHEMA_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
Transport = Callable[..., bytes]
UsageRecorder = Callable[[str, Any | None], None]


class InferenceResponseError(ValueError):
    pass


def _request_bytes(method: str, url: str, **kwargs: Any) -> bytes:
    timeout = float(kwargs.pop("timeout"))
    max_bytes = int(kwargs.pop("max_bytes"))
    headers = kwargs.pop("headers", {})
    data = kwargs.pop("data", None)
    kwargs.pop("failure_message", None)
    if method != "POST" or url != ENDPOINT or kwargs:
        raise ValueError("OpenAI text inference transport request is invalid")
    return provider_http.post(
        ENDPOINT,
        headers=headers,
        data=data,
        timeout=timeout,
        max_bytes=max_bytes,
        label="OpenAI text inference",
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def complete(
    *,
    api_key: str,
    model: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    transport: Transport = _request_bytes,
    usage_recorder: UsageRecorder | None = None,
) -> dict[str, Any]:
    """Return one schema-validated object or raise a bounded adapter error."""
    if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("text inference prompt is empty or too large")
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("OpenAI API key is missing")
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > 128:
        raise ValueError("OpenAI model is invalid")
    if not isinstance(schema_name, str) or SCHEMA_NAME_RE.fullmatch(schema_name) is None:
        raise ValueError("text inference schema name is invalid")
    schema = json_contract.validate_object_schema(schema, max_bytes=MAX_SCHEMA_BYTES)
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return a JSON object that matches the supplied schema.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    encoded = _json_bytes(body)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("text inference request is too large")
    raw = transport(
        "POST",
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=encoded,
        failure_message="OpenAI text inference failed.",
        timeout=TIMEOUT_SECONDS,
        max_bytes=MAX_RESPONSE_BYTES,
    )
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if usage_recorder is not None:
            usage_recorder(model, None)
        raise InferenceResponseError("OpenAI returned an invalid structured response") from exc
    if usage_recorder is not None:
        usage_recorder(model, response)
    try:
        choices = response["choices"]
        content = choices[0]["message"]["content"]
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise InferenceResponseError("OpenAI returned an invalid structured response") from exc
    if not isinstance(result, dict) or not json_contract.matches(result, schema):
        raise InferenceResponseError("OpenAI response did not match the requested schema")
    return result
