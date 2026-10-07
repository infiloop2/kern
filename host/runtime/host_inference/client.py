"""Bounded client for the dedicated host-inference Unix socket."""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any

from host.constants import HOST_INFERENCE_SOCKET_PATH as DEFAULT_SOCKET_PATH
from host.runtime.core import host_errors
from host.runtime.host_inference.providers import OPENAI_MAX_TIMEOUT_SECONDS, JEV_MAX_TIMEOUT_SECONDS
from host.runtime.host_inference.openai import MAX_PROMPT_BYTES


SOCKET_PATH = os.environ.get("KERN_HOST_INFERENCE_SOCKET", DEFAULT_SOCKET_PATH)
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


class HostInferenceError(RuntimeError):
    """A bounded host-inference call did not produce a usable result."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class _HostInferenceConnection(http.client.HTTPConnection):
    def __init__(self, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_PATH)


def _request(
    path: str,
    body: dict[str, Any],
    timeout_seconds: float,
    max_request_bytes: int,
) -> dict[str, Any]:
    try:
        payload = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > max_request_bytes:
            raise ValueError("host inference request is too large")
    except (TypeError, ValueError, RecursionError) as exc:
        host_errors.report_warning(
            "host_inference.client", exc, context={"path": path}, kind="unexpected_behavior"
        )
        raise HostInferenceError("Host inference request could not be encoded") from exc
    # Small fixed allowance for local dispatch and returning the provider result.
    connection = _HostInferenceConnection(timeout_seconds + 0.1)
    try:
        connection.request(
            "POST", path, body=payload, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("host inference service returned an invalid response")
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict) or "result" not in decoded:
            raise ValueError("host inference service returned an invalid response")
        result = decoded["result"]
        if result is not None and not isinstance(result, dict):
            raise ValueError("host inference service returned an invalid result")
    except (
        OSError,
        http.client.HTTPException,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        host_errors.report_warning(
            "host_inference.client", exc, context={"path": path}, kind="unexpected_behavior"
        )
        raise HostInferenceError("Host inference request failed") from exc
    finally:
        connection.close()
    # The service logs provider failures; disabled providers are expected.
    # Surface the outcome without duplicating those diagnostics.
    if result is None:
        if decoded.get("error") == "provider_disabled":
            raise HostInferenceError("Host inference provider is disabled", reason="provider_disabled")
        if decoded.get("error") == "timeout":
            raise HostInferenceError("Host inference timed out") from TimeoutError("Provider timed out")
        raise HostInferenceError("Host inference returned no usable result")
    return result


def openai_text_completion(
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    *,
    model: str,
    instructions: str,
    reasoning_effort: str,
    max_output_tokens: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0.1 <= timeout_seconds <= OPENAI_MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"text completion timeout_seconds must be between 0.1 and {OPENAI_MAX_TIMEOUT_SECONDS}")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise HostInferenceError("Host inference input exceeds the size limit", reason="input_too_large")
    return _request(
        "/openai/text-completion",
        {
            "model": model,
            "instructions": instructions,
            "reasoning_effort": reasoning_effort,
            "max_output_tokens": max_output_tokens,
            "prompt": prompt,
            "schema": schema,
            "schema_name": schema_name,
            "timeout_seconds": timeout_seconds,
        },
        float(timeout_seconds),
        MAX_REQUEST_BYTES,
    )


def openai_decisions(
    input: str, questions: list[dict[str, Any]], *, model: str, timeout_seconds: float,
) -> dict[str, Any]:
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= timeout_seconds <= OPENAI_MAX_TIMEOUT_SECONDS):
        raise ValueError(f"decision timeout_seconds must be between 0.1 and {OPENAI_MAX_TIMEOUT_SECONDS}")
    return _request(
        "/openai/decisions",
        {"input": input, "questions": questions, "model": model, "timeout_seconds": timeout_seconds},
        float(timeout_seconds), MAX_REQUEST_BYTES,
    )


def typesafe_jev_judgment(
    state: Any,
    questions: dict[str, dict[str, Any]],
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0.1 <= timeout_seconds <= JEV_MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"judgment timeout_seconds must be between 0.1 and {JEV_MAX_TIMEOUT_SECONDS}"
        )
    if not isinstance(questions, dict) or not questions:
        raise ValueError("judgment questions must be a non-empty object")
    return _request(
        "/typesafe/jev-judgment",
        {"state": state, "questions": questions, "timeout_seconds": timeout_seconds},
        float(timeout_seconds),
        MAX_REQUEST_BYTES,
    )
