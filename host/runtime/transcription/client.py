"""Bounded operator-only client for local speech recognition."""

from __future__ import annotations

import base64
import binascii
import http.client
from http import HTTPStatus
import json
import socket
from typing import Any

from host.constants import TRANSCRIPTION_SOCKET_PATH
from host.runtime.admin_api.errors import ApiError

SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * 12
MAX_REQUEST_BYTES = MAX_AUDIO_BYTES * 4 // 3 + 1024
MAX_RESPONSE_BYTES = 32 * 1024
TIMEOUT_SECONDS = 60


def decode_audio(body: Any) -> bytes:
    if not isinstance(body, dict) or set(body) != {"audio"}:
        raise ValueError("Expected a PCM audio segment.")
    encoded = body["audio"]
    if not isinstance(encoded, str) or len(encoded) > MAX_AUDIO_BYTES * 4 // 3:
        raise ValueError("Audio segment is too large.")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid audio encoding.") from exc
    if not 2 <= len(audio) <= MAX_AUDIO_BYTES or len(audio) % 2:
        raise ValueError("Expected up to 12 seconds of mono 16 kHz PCM16 audio.")
    return audio


class _Connection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=TIMEOUT_SECONDS)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(TRANSCRIPTION_SOCKET_PATH)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _request(method: str, path: str, body: Any = None, *, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    conn = _Connection()
    conn.timeout = timeout
    try:
        conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Oversized transcription response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid transcription response")
        if response.status != 200:
            message = {
                "model_not_ready": "Transcription model isn't loaded yet. Click the mic to retry.",
                "busy": "Transcription is busy. Your audio is saved; click the mic to retry.",
            }.get(str(result.get("error")), "Transcription is unavailable. Click the mic to retry.")
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, message)
        return result
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE,
                       "Transcription is unavailable. Click the mic to retry.") from exc
    finally:
        conn.close()


def readiness() -> dict[str, bool]:
    result = _request("GET", "/ready", timeout=2)
    if result.get("ready") is not True:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Transcription model isn't loaded yet. Click the mic to retry.")
    return {"ready": True}


def transcribe(body: Any) -> dict[str, str]:
    try:
        decode_audio(body)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    result = _request("POST", "/transcribe", body)
    if not isinstance(result.get("text"), str):
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "The host returned an unreadable response. Click the mic to retry.")
    return {"text": result["text"]}
