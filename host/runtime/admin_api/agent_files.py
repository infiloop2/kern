"""Agent-home file browsing helpers and request validation."""

from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api.request_params import one as _one
from host.runtime.core.root_helpers import HelperTimedOut, run_root_helper


HELPER_TIMEOUT_SECONDS = 10
HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/read-agent-file"]
UPLOAD_HELPER_COMMAND = ["/usr/bin/sudo", "-n", "/usr/local/lib/kern-host/upload-agent-file"]
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
UPLOAD_FILENAME_MAX_BYTES = 200
STREAM_MAX_BYTES = 200_000_000
IMAGE_STREAM_MAX_BYTES = 25 * 1024 * 1024
DOWNLOAD_MAX_BYTES = 200_000_000
STREAM_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def list_files(path: str) -> dict[str, Any]:
    return _run_file_helper("list", path)


def read_file(path: str) -> dict[str, Any]:
    return _run_file_helper("read", path)


def _run_file_helper(action: str, path: str) -> dict[str, Any]:
    try:
        proc = run_root_helper([*HELPER_COMMAND, action, path], HELPER_TIMEOUT_SECONDS)
    except HelperTimedOut as exc:
        message = (
            "agent file helper timed out (the root helper could not be terminated)"
            if exc.could_not_terminate
            else "agent file helper timed out"
        )
        raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, message) from exc
    if proc.returncode != 0:
        message = helper_error_message(proc.stdout, proc.stderr)
        status = {
            2: HTTPStatus.NOT_FOUND,
            3: HTTPStatus.BAD_REQUEST,
            4: HTTPStatus.BAD_REQUEST,
        }.get(proc.returncode, HTTPStatus.INTERNAL_SERVER_ERROR)
        raise ApiError(status, message or "agent file helper failed")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "agent file helper returned invalid JSON",
        ) from exc
    if not isinstance(value, dict):
        raise ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "agent file helper returned invalid JSON",
        )
    return value


def helper_error_message(stdout: str, stderr: str) -> str:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return stderr.strip()
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
    return stderr.strip()


def path_from_query(query: dict[str, list[str]]) -> str:
    value = _one(query, "path")
    if value is None or value == "":
        return "/"
    if "\0" in value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path contains a NUL byte")
    if len(value) > 4096:
        raise ApiError(HTTPStatus.BAD_REQUEST, "path is too long")
    return value


def content_disposition(path: str) -> str:
    filename = Path(path).name or "download"
    clipped = filename.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "download"
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", clipped)[:120] or "download"
    encoded = quote(clipped, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def upload_filename(query: dict[str, list[str]]) -> str:
    unexpected = sorted(set(query) - {"filename"})
    if unexpected:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"unsupported agent file upload query parameter: {unexpected[0]}",
        )
    value = _one(query, "filename")
    if value is None or value in {"", ".", ".."}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "filename must be non-empty")
    if any(character in value for character in ("/", "\\", "\0")):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "filename must not contain path separators or a NUL byte",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "filename must not contain control characters",
        )
    if len(value.encode("utf-8")) > UPLOAD_FILENAME_MAX_BYTES:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"filename must be at most {UPLOAD_FILENAME_MAX_BYTES} UTF-8 bytes",
        )
    return value
