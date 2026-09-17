"""Grok video storage: host-held S3 signing, no CLI credentials or job registry.

xAI returns our signed PUT URL in the completed video response. Verifying that
URL lets us mint a GET URL for the same object without retaining job state.
Completed results must echo a host-signed upload URL for the dedicated prefix.
Downloads use the operator's ordinary custom-domain rule.
"""
from __future__ import annotations

import datetime
import hmac
import json
import re
import urllib.parse
import uuid
from typing import Any

from host.network_integrations.base import ResponseRewrite
from host.runtime.core import state
from host.runtime.core.aws_sigv4 import s3_presigned_url
from host.runtime.core.network_policy import decode_body, normalized_path

_KEY = re.compile(r"grok-videos/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4")


def storage_host(bucket: str, region: str) -> str:
    return f"{bucket}.s3.{region}.amazonaws.com"


def signed_url(config: dict[str, str], method: str, key: str, *, now: datetime.datetime | None = None) -> str:
    return s3_presigned_url(method, config["bucket"], config["region"], key,
                            config["access_key_id"], config["secret_access_key"], now=now)


def verified_key(config: dict[str, str], method: str, url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        prefix = "/"
        if parsed.scheme != "https" or parsed.netloc != storage_host(config["bucket"], config["region"]) or parsed.fragment:
            return None
        if not parsed.path.startswith(prefix):
            return None
        key = parsed.path[len(prefix):]
        if not _KEY.fullmatch(key):
            return None
        dates = urllib.parse.parse_qs(parsed.query).get("X-Amz-Date", [])
        if len(dates) != 1:
            return None
        when = datetime.datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()
        if not 0 <= age <= 900:
            return None
        expected = signed_url(config, method, key, now=when)
        return key if hmac.compare_digest(url, expected) else None
    except (ValueError, TypeError):
        return None


def prepare_request(method: str, host: str, path: str, headers: list[tuple[str, str]], body: bytes) -> tuple[list[tuple[str, str]], bytes]:
    """Called only after the account, route and media-input guards passed."""
    path = normalized_path(path)
    method = method.upper()
    if host.lower() != "api.x.ai" or not path.startswith("/v1/videos/"):
        return headers, body
    if method == "POST":
        config = state.read_xai_video_storage()
        if config is None:
            raise OSError("xai_video_storage_required")
        payload = json_body(headers, body)
        # Always replace caller-supplied output, so it cannot select storage.
        payload["output"] = {"upload_url": signed_url(config, "PUT", f"grok-videos/{uuid.uuid4()}.mp4")}
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = [(k, v) for k, v in headers if k.lower() not in {"content-encoding", "content-length"}]
    elif method == "GET":
        headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"] + [("Accept-Encoding", "gzip")]
    return headers, body


def prepare_response(method: str, host: str, path: str) -> ResponseRewrite | None:
    """Capture storage for the allowed polling request's response transform."""
    if method.upper() != "GET" or host.lower() != "api.x.ai" or not normalized_path(path).startswith("/v1/videos/"):
        return None
    config = state.read_xai_video_storage()
    if config is None:
        raise OSError("xai_video_storage_required")

    def rewrite_response(status: int, response_headers: list[tuple[str, str]], response_body: bytes):
        if status != 200:
            return response_headers, response_body
        payload = json_body(response_headers, response_body)
        if not isinstance(payload, dict) or payload.get("status") != "done":
            return response_headers, response_body
        video = payload.get("video")
        if not isinstance(video, dict) or not isinstance(video.get("url"), str):
            raise OSError("xai_video_output_invalid")
        key = verified_key(config, "PUT", video.get("url", ""))
        if key is None:
            raise OSError("xai_video_output_invalid")
        video["url"] = signed_url(config, "GET", key)
        response_headers = [(k, v) for k, v in response_headers if k.lower() not in {
            "content-encoding", "content-length", "etag", "content-md5",
        }]
        return response_headers, json.dumps(payload, separators=(",", ":")).encode()

    return ResponseRewrite(rewrite_response, "xai_video_response_invalid")


def json_body(headers: list[tuple[str, str]], body: bytes):
    encoding = next((v for k, v in headers if k.lower() == "content-encoding"), "")
    decoded = decode_body(body, encoding)
    if decoded is None:
        raise ValueError("undecodable JSON body")
    return json.loads(decoded)
