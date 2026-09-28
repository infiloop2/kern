"""Private staged media uploaded to fal storage after action validation."""
from __future__ import annotations
import re
from urllib.parse import urlsplit
from host.tools.host_api import HostAPI
from host.tools.json_types import JSONValue
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.web import is_public_https_url, json_request, stream_request_bytes

UPLOAD_INIT = "https://rest.fal.ai/storage/upload/initiate?storage_type=gcs"
MEDIA_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "video/mp4": ".mp4", "video/quicktime": ".mov", "audio/mpeg": ".mp3", "audio/wav": ".wav"}
MAX_INPUT_BYTES = {"image": 20_000_000, "video": 200_000_000, "audio": 20_000_000}


def staged_asset_id(value: JSONValue, kind: str, api: HostAPI) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise ToolInputValidationError("falAI requires a valid staged asset id.")
    metadata = api.assets.describe(value)
    if metadata.media_type not in MEDIA_TYPES or not metadata.media_type.startswith(kind + "/"):
        raise ToolInputValidationError(f"falAI requires a staged {kind} of a supported type.")
    if not 1 <= metadata.size_bytes <= MAX_INPUT_BYTES[kind]:
        raise ToolInputValidationError("falAI staged media exceeds the supported size limit.")
    return value


def provider_url(value: JSONValue, *, upload: bool = False) -> str:
    if not isinstance(value, str) or not is_public_https_url(value):
        raise ToolInputValidationError("fal returned an invalid media URL.")
    host = urlsplit(value).hostname or ""
    allowed = host == "storage.googleapis.com" or host.endswith(".storage.googleapis.com")
    if not upload:
        allowed = allowed or host == "fal.media" or host.endswith(".fal.media")
    if not allowed:
        raise ToolInputValidationError("fal returned an unexpected media host.")
    return value


def upload(asset_id: str, api: HostAPI, headers: dict[str, str]) -> str:
    meta = api.assets.describe(asset_id)
    response = json_request("POST", UPLOAD_INIT, headers=headers,
        body={"file_name": "source" + MEDIA_TYPES[meta.media_type], "content_type": meta.media_type},
        failure_message="fal upload initialization failed.", invalid_response_message="fal returned invalid upload metadata.")
    upload_url = provider_url(response.get("upload_url"), upload=True)
    file_url = provider_url(response.get("file_url"))
    with api.assets.open(asset_id) as source:
        stream_request_bytes("PUT", upload_url, headers={"Content-Type": meta.media_type}, body=source,
            content_length=meta.size_bytes, timeout=120, failure_message="fal source media upload failed.")
    return file_url
