"""Shared media-download plumbing for generation tools.

Every generation tool that saves a finished render into the agent workspace does
the same thing with the provider's authoritative output URL: stream it under a
size bound, admit only known media types, and hand the host one
``OpenedStreamingAsset``. Only the provider name in the messages, the filename,
and the status mapping differ.

The bound and the media-type allowlist are what keep an unexpected provider
response from becoming an arbitrary file in the operator's workspace, so they
live here once instead of being re-typed per provider where they could drift
apart — the same reason ``is_public_https_url`` is shared.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
from typing import Callable, Iterator

from host.tools.results import OpenedStreamingAsset, StreamingAssetError
from host.tools.shared.web import WebRequestError, open_response_stream

# A render smaller than this is not a video, and the upper bound matches the
# agent asset store's own ceiling.
MIN_VIDEO_BYTES = 512
MAX_MEDIA_BYTES = 200_000_000
# Only containers the workspace can name from the response alone. An unknown
# media type is refused rather than guessed at, since the suffix decides the
# filename the operator ends up with.
VIDEO_SUFFIXES = {"video/mp4": ".mp4", "video/quicktime": ".mov"}

# Conservative email cap leaves room for base64 encoding and message headers
# under Zoho's smallest documented 20 MB message limit.
MAX_EMAIL_ATTACHMENT_BYTES = 10_000_000


def matches_media_signature(prefix: bytes, media_type: str) -> bool:
    """Check the supported container signature, not an agent's extension alone.

    This identifies a container; it does not claim to decode its media or scan
    it for malware. Active formats such as SVG are deliberately excluded.
    """
    if media_type in {"image/jpeg", "image/jpg"}:
        return prefix.startswith(b"\xff\xd8\xff")
    if media_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/webp":
        return prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    if media_type in {"video/mp4", "video/quicktime"}:
        if len(prefix) < 16 or prefix[4:8] != b"ftyp":
            return False
        box_size = int.from_bytes(prefix[:4], "big")
        if box_size < 16:
            return False
        brands = {prefix[8:12], *[prefix[i:i + 4] for i in range(16, min(box_size, len(prefix)), 4)]}
        if media_type == "video/quicktime":
            return b"qt  " in brands
        # ISO BMFF versions identify the container independently of its codec
        # (for example, HEVC exports commonly advertise compatible iso6).
        return bool(brands & {
            b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso7",
            b"iso8", b"iso9", b"isoa", b"isob", b"isoc",
            b"mp41", b"mp42", b"avc1", b"M4V ", b"dash",
        })
    return False


@contextmanager
def open_downloaded_video(
    url: str,
    *,
    provider: str,
    filename_stem: str,
    map_failure: Callable[[WebRequestError], str],
    timeout: int = 120,
    headers: Mapping[str, str] | None = None,
) -> Iterator[OpenedStreamingAsset]:
    """Open a provider's finished video for the host to stream into the workspace.

    ``map_failure`` turns a transport failure into that provider's curated,
    secret-free message; it may also raise, which lets a package report an
    unmapped failure as a Host warning instead of a vague string.
    """
    with _open_downloaded_media(
        url, provider=provider, filename_stem=filename_stem, map_failure=map_failure,
        kind="video", suffixes=VIDEO_SUFFIXES, min_bytes=MIN_VIDEO_BYTES,
        timeout=timeout,
        headers=headers,
    ) as opened:
        yield opened


@contextmanager
def open_downloaded_image(
    url: str,
    *,
    provider: str,
    filename_stem: str,
    map_failure: Callable[[WebRequestError], str],
    timeout: int = 120,
    headers: Mapping[str, str] | None = None,
) -> Iterator[OpenedStreamingAsset]:
    """Stream a completed image with the same size/type bounds as video."""
    with _open_downloaded_media(
        url, provider=provider, filename_stem=filename_stem, map_failure=map_failure,
        kind="image", suffixes={"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"},
        min_bytes=1, timeout=timeout, headers=headers,
    ) as opened:
        yield opened


@contextmanager
def open_downloaded_audio(
    url: str,
    *,
    provider: str,
    filename_stem: str,
    map_failure: Callable[[WebRequestError], str],
    timeout: int = 120,
    headers: Mapping[str, str] | None = None,
) -> Iterator[OpenedStreamingAsset]:
    """Stream generated MP3 speech through the same bounded asset handoff."""
    with _open_downloaded_media(
        url, provider=provider, filename_stem=filename_stem, map_failure=map_failure,
        kind="audio", suffixes={"audio/mpeg": ".mp3"}, min_bytes=1,
        timeout=timeout, headers=headers,
    ) as opened:
        yield opened


@contextmanager
def _open_downloaded_media(
    url: str,
    *,
    provider: str,
    filename_stem: str,
    map_failure: Callable[[WebRequestError], str],
    kind: str,
    suffixes: dict[str, str],
    min_bytes: int,
    timeout: int,
    headers: Mapping[str, str] | None = None,
) -> Iterator[OpenedStreamingAsset]:
    failure_message = f"{provider} {kind} download failed."
    try:
        with open_response_stream(
            "GET", url, headers=headers, failure_message=failure_message, timeout=timeout
        ) as (source, response_headers):
            raw_length = response_headers.get("content-length", "")
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise StreamingAssetError(
                    f"{provider} {kind} download did not include a valid size."
                )
            size_bytes = int(raw_length)
            if not min_bytes <= size_bytes <= MAX_MEDIA_BYTES:
                raise StreamingAssetError(
                    f"{provider} {kind} download size is outside the supported range."
                )
            media_type = (
                response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            suffix = suffixes.get(media_type)
            if suffix is None:
                raise StreamingAssetError(
                    f"{provider} {kind} download returned an unsupported media type."
                )
            yield OpenedStreamingAsset(
                filename=f"{filename_stem}{suffix}",
                media_type=media_type,
                size_bytes=size_bytes,
                source=source,
            )
    except StreamingAssetError:
        raise
    except WebRequestError as exc:
        raise StreamingAssetError(map_failure(exc)) from exc
    except ValueError as exc:
        raise StreamingAssetError(str(exc) or failure_message) from exc
