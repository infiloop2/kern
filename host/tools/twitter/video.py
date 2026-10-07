"""Private staged video -> X v2 chunked upload, during approved execution only.

Protocol: https://docs.x.com/x-api/media/quickstart/media-upload-chunked
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from typing import cast

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.web import ProviderWarning, WebRequestError, json_request, provider_warning, request_bytes

API_BASE = "https://api.x.com/2/media/upload"
MAX_VIDEO_BYTES = 200_000_000  # Same bounded staging limit as the host.
CHUNK_BYTES = 4 * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 300
MAX_STATUS_CHECKS = 30
MEDIA_ID_RE = re.compile(r"^[0-9]{1,19}$")


def _provider_failure(message: str, response: JSONObject, *, operation: str = "video upload") -> ProviderWarning:
    # Preserve bounded processing/upload errors for operator diagnostics;
    # never return raw provider text to the agent.
    error = WebRequestError(message, status=200, body=json.dumps(response).encode()[:4096])
    return provider_warning("X", operation, error, message)


def snapshot(asset_id: object, api: HostAPI) -> JSONObject:
    if not isinstance(asset_id, str) or not asset_id:
        raise ToolInputValidationError("X video_asset_id must be a staged video reference.")
    metadata = api.assets.describe(asset_id)
    if metadata.media_type not in {"video/mp4", "video/quicktime"}:
        raise ToolInputValidationError("X video posting requires a staged MP4 or MOV video.")
    if not 512 <= metadata.size_bytes <= MAX_VIDEO_BYTES:
        raise ToolInputValidationError("X video size must be between 512 and 200,000,000 bytes.")
    return {
        "asset_id": metadata.asset_id, "filename": metadata.filename,
        "media_type": metadata.media_type, "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
    }


def upload(access_token: str, asset: JSONObject, api: HostAPI) -> str:
    """No retries, external URLs, public grants, or agent-controlled paths."""
    if snapshot(asset.get("asset_id"), api) != asset:
        raise ToolInputValidationError("X video changed after approval. Queue a new approval.")
    deadline = time.monotonic() + UPLOAD_TIMEOUT_SECONDS
    headers = {"authorization": f"Bearer {access_token}"}

    def timeout() -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("X video upload or processing timed out; no post was submitted.")
        return min(30, max(1, math.ceil(remaining)))

    def media_request(method: str, suffix: str, body: JSONObject | None = None) -> JSONObject:
        response = json_request(
            method, API_BASE + suffix, headers=headers, body=body, timeout=timeout(),
            failure_message="X video upload request failed.",
            invalid_response_message="X video upload returned an invalid response.",
        )
        if response.get("errors"):
            raise _provider_failure("X rejected the video upload; no post was submitted.", response)
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("X video upload returned an invalid response.")
        return cast(JSONObject, data)

    # Open and verify the exact staged bytes BEFORE initializing any upload.
    # Keep this descriptor open through upload, including if the TTL elapses.
    with api.assets.open(cast(str, asset["asset_id"])) as source:
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_VIDEO_BYTES:
                raise ToolInputValidationError("X staged video exceeds the size limit.")
            digest.update(chunk)
        if size != asset["size_bytes"] or digest.hexdigest() != asset["sha256"]:
            raise ToolInputValidationError("X staged video bytes changed after approval.")
        source.seek(0)
        initialized = media_request("POST", "/initialize", {
            "media_type": asset["media_type"], "total_bytes": size,
            "media_category": "tweet_video",
        })
        media_id = initialized.get("id")
        if not isinstance(media_id, str) or not MEDIA_ID_RE.fullmatch(media_id):
            raise RuntimeError("X did not confirm the video upload id.")
        segment_index = 0
        while chunk := source.read(CHUNK_BYTES):
            boundary = "kern-" + secrets.token_hex(16)
            # Fixed filename and type: no untrusted metadata enters MIME headers.
            multipart = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"segment_index\"\r\n\r\n"
                f"{segment_index}\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="media"; filename="video.bin"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n'
            ).encode("ascii") + chunk + f"\r\n--{boundary}--\r\n".encode("ascii")
            raw = request_bytes(
                "POST", f"{API_BASE}/{media_id}/append",
                headers={**headers, "content-type": f"multipart/form-data; boundary={boundary}"},
                data=multipart, timeout=timeout(), failure_message="X video chunk upload failed.",
            )
            if raw.strip():
                try:
                    appended = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError("X video chunk upload returned an invalid response.") from exc
                if not isinstance(appended, dict):
                    raise RuntimeError("X video chunk upload returned an invalid response.")
                if appended.get("errors"):
                    raise _provider_failure("X rejected a video chunk; no post was submitted.", appended)
            segment_index += 1
    finalized = media_request("POST", f"/{media_id}/finalize")
    if finalized.get("id") != media_id:
        raise RuntimeError("X did not confirm the finalized video id.")
    processing = finalized.get("processing_info")
    if processing is None:
        timeout()
        return media_id
    for checks in range(MAX_STATUS_CHECKS + 1):
        timeout()
        if not isinstance(processing, dict):
            raise RuntimeError("X returned invalid video processing status.")
        state = processing.get("state")
        if state == "succeeded":
            return media_id
        if state == "failed":
            raise _provider_failure("X video processing failed; no post was submitted.", processing)
        if state not in {"pending", "in_progress"}:
            raise RuntimeError("X returned invalid video processing status.")
        if checks == MAX_STATUS_CHECKS:
            break
        delay = processing.get("check_after_secs", 1)
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 0:
            raise RuntimeError("X returned invalid video processing delay.")
        if delay >= deadline - time.monotonic():
            raise RuntimeError("X video processing timed out; no post was submitted.")
        time.sleep(max(1, delay))
        status = media_request("GET", f"?command=STATUS&media_id={media_id}")
        if "id" in status and status["id"] != media_id:
            raise RuntimeError("X returned a different video processing id.")
        processing = status.get("processing_info")
    raise RuntimeError("X video processing timed out; no post was submitted.")
