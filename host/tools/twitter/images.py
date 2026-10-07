"""Private staged images -> X v2 upload, after approval only."""
from __future__ import annotations

import base64
import hashlib
from typing import cast

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.web import json_request
from host.tools.twitter import video

MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGES = 4


def snapshot(asset_id: object, api: HostAPI) -> JSONObject:
    if not isinstance(asset_id, str) or not asset_id:
        raise ToolInputValidationError("X image ids must be staged image references.")
    metadata = api.assets.describe(asset_id)
    if metadata.media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ToolInputValidationError("X image posting requires staged JPEG, PNG or WebP images.")
    if not 1 <= metadata.size_bytes <= MAX_IMAGE_BYTES:
        raise ToolInputValidationError("Each X image must be at most 5,000,000 bytes.")
    return {"asset_id": metadata.asset_id, "filename": metadata.filename,
            "media_type": metadata.media_type, "size_bytes": metadata.size_bytes,
            "sha256": metadata.sha256}


def snapshots(asset_ids: JSONValue, api: HostAPI) -> list[JSONValue]:
    if not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= MAX_IMAGES:
        raise ToolInputValidationError("X image_asset_ids must contain 1–4 staged image ids.")
    if any(not isinstance(item, str) for item in asset_ids) or len(set(cast(list[str], asset_ids))) != len(asset_ids):
        raise ToolInputValidationError("X image_asset_ids must contain distinct staged image ids.")
    return [snapshot(item, api) for item in asset_ids]


def upload(access_token: str, assets: JSONValue, api: HostAPI) -> list[JSONValue]:
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_IMAGES:
        raise ToolInputValidationError("X approved images are invalid.")
    # Verify ALL attachments before any provider upload; capture exact bounded
    # bytes so replacement or expiry between uploads cannot change the payload.
    contents: list[bytes] = []
    ids: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict) or snapshot(asset.get("asset_id"), api) != asset:
            raise ToolInputValidationError("X image changed after approval. Queue a new approval.")
        asset_id = cast(str, asset["asset_id"])
        if asset_id in ids:
            raise ToolInputValidationError("X approved images contain duplicate ids.")
        ids.add(asset_id)
        with api.assets.open(asset_id) as source:
            data = source.read(MAX_IMAGE_BYTES + 1)
        if len(data) != asset["size_bytes"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise ToolInputValidationError("X staged image bytes changed after approval.")
        contents.append(data)
    media_ids: list[JSONValue] = []
    for data in contents:
        response = json_request(
            "POST", video.API_BASE, headers={"authorization": f"Bearer {access_token}"},
            body={"media": base64.b64encode(data).decode("ascii"), "media_category": "tweet_image"},
            failure_message="X image upload failed; no post was submitted.",
            invalid_response_message="X image upload returned an invalid response; no post was submitted.",
        )
        if response.get("errors"):
            raise video._provider_failure("X rejected the image upload; no post was submitted.", response, operation="image upload")
        uploaded = response.get("data")
        media_id = uploaded.get("id") if isinstance(uploaded, dict) else None
        processing = uploaded.get("processing_info") if isinstance(uploaded, dict) else None
        if not isinstance(media_id, str) or not video.MEDIA_ID_RE.fullmatch(media_id):
            raise RuntimeError("X did not confirm the image upload id; no post was submitted.")
        if processing is not None and (not isinstance(processing, dict) or processing.get("state") != "succeeded"):
            raise RuntimeError("X image is not ready; no post was submitted. Stage a supported static image.")
        media_ids.append(media_id)
    return media_ids
