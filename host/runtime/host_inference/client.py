"""Bounded client for the dedicated host-inference Unix socket."""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any

from host.constants import HOST_INFERENCE_SOCKET_PATH as DEFAULT_SOCKET_PATH
from host.runtime.core import host_errors
from host.runtime.host_inference.typesafe import DEFAULT_TIMEOUT_SECONDS as JEV_MAX_TIMEOUT_SECONDS


SOCKET_PATH = os.environ.get("KERN_HOST_INFERENCE_SOCKET", DEFAULT_SOCKET_PATH)
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


class HostInferenceError(RuntimeError):
    """A bounded host-inference call did not produce a usable result."""


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
    connection = _HostInferenceConnection(timeout_seconds)
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
    # Provider adapters already recorded the reason for a null result. Surface
    # the failure to the caller without duplicating or relabeling that event.
    if result is None:
        raise HostInferenceError("Host inference returned no usable result")
    return result


def openai_text_completion(
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
    *,
    purpose: str,
) -> dict[str, Any]:
    return _request(
        "/openai/text-completion",
        {
            "prompt": prompt,
            "schema": schema,
            "schema_name": schema_name,
            "purpose": purpose,
        },
        25.0,
        MAX_REQUEST_BYTES,
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
