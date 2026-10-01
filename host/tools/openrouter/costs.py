"""Submission estimates at list rates, without promotional discounts.

Rates verified 2026-10-01 against OpenRouter's /api/v1/videos/models and
https://help.heygen.com/en/articles/17272995-introducing-heygen-video-generate-cinematic-clips-from-a-prompt.
Reference mode bills output plus video-reference seconds at its higher rate.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import BinaryIO, Iterator, cast

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue

LIST_RATES = {"480p": ("0.02", "0.04"), "768p": ("0.03", "0.06")}
UNKNOWN_VIDEO_SECONDS = 60


def _atoms(source: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Walk bounded MP4/MOV atoms without loading the video into memory."""
    for _ in range(1024):
        if start + 8 > end:
            return
        source.seek(start)
        header = source.read(8)
        if len(header) != 8:
            return
        size, kind = int.from_bytes(header[:4], "big"), header[4:]
        payload = start + 8
        if size == 1:
            extended = source.read(8)
            if len(extended) != 8:
                return
            size, payload = int.from_bytes(extended, "big"), start + 16
        elif size == 0:
            size = end - start
        if size < payload - start or start + size > end:
            return
        yield kind, payload, start + size
        start += size


def _video_seconds(api: HostAPI, asset_id: str) -> int:
    metadata = api.assets.describe(asset_id)
    with api.assets.open(asset_id) as source:
        for kind, start, end in _atoms(source, 0, metadata.size_bytes):
            if kind != b"moov":
                continue
            for child, payload, limit in _atoms(source, start, end):
                if child != b"mvhd":
                    continue
                source.seek(payload)
                header = source.read(min(32, limit - payload))
                if not header or header[0] not in (0, 1):
                    continue
                offset, width = (12, 4) if header[0] == 0 else (20, 8)
                if len(header) < offset + 4 + width:
                    continue
                scale = int.from_bytes(header[offset:offset + 4], "big")
                duration = int.from_bytes(header[offset + 4:offset + 4 + width], "big")
                if scale and 0 < duration < (1 << (width * 8)) - 1:
                    return (duration + scale - 1) // scale
    # Typical outputs are 5-15 seconds. This allowance deliberately leans high
    # for unreadable reference metadata; it is an estimate, not an upper bound.
    return UNKNOWN_VIDEO_SECONDS


def quote_usd(body: JSONObject, catalog: JSONObject, api: HostAPI) -> str:
    resolution = cast(str, body["resolution"])
    reference = bool(body.get("input_references"))
    baseline = Decimal(LIST_RATES[resolution][int(reference)])
    skus = catalog.get("pricing_skus")
    key = ("reference_" if reference else "") + "duration_seconds_" + resolution
    value: JSONValue = skus.get(key) if isinstance(skus, dict) else None
    # The documented list rates also cover missing/malformed catalog pricing.
    # Never use a lower live/promotional rate to understate the estimate.
    rate = baseline
    if not isinstance(value, bool) and isinstance(value, (str, int, float)) and len(str(value)) <= 64:
        try:
            candidate = Decimal(str(value))
            if candidate.is_finite() and 0 <= candidate < 1_000_000_000:
                rate = max(rate, candidate)
        except InvalidOperation:
            pass
    seconds = cast(int, body["duration"])
    # Count occurrences, even if the same staged file is referenced twice.
    for row in cast(list[JSONObject], body.get("input_references", [])):
        if row["type"] == "video_url":
            asset_id = cast(str, cast(JSONObject, row["video_url"])["url"])
            seconds += _video_seconds(api, asset_id)
    amount = seconds * rate
    if amount >= 1_000_000_000:
        raise ValueError("OpenRouter's submission estimate exceeds Kern's supported usage amount.")
    return format(amount.quantize(Decimal("0.000000001"), rounding=ROUND_CEILING), "f")
