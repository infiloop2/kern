"""Small, shared admission policy for native WhatsApp attachments."""

from __future__ import annotations

from host.tools.host_api import AssetMetadata
from host.tools.json_types import JSONObject

# Conservative Kern limits, not a claim about every WhatsApp client limit.
MAX_IMAGE_BYTES = 5_000_000
MAX_VIDEO_BYTES = 16_000_000
MIN_MEDIA_BYTES = 512
MEDIA_LIMITS = {
    "image/jpeg": MAX_IMAGE_BYTES,
    "image/png": MAX_IMAGE_BYTES,
    "video/mp4": MAX_VIDEO_BYTES,
}


def validate_media(media_type: str, size_bytes: int) -> None:
    maximum = MEDIA_LIMITS.get(media_type)
    if maximum is None or not MIN_MEDIA_BYTES <= size_bytes <= maximum:
        raise ValueError("WhatsApp media must be JPEG/PNG (512 bytes–5 MB) or MP4 (512 bytes–16 MB).")


def media_snapshot(metadata: AssetMetadata) -> JSONObject:
    validate_media(metadata.media_type, metadata.size_bytes)
    return {
        "asset_id": metadata.asset_id,
        "filename": metadata.filename,
        "media_type": metadata.media_type,
        "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
        "expires_at": metadata.expires_at,
    }
