"""Shared media-download plumbing for generation tools.

Every generation tool that saves a finished render into the agent workspace does
the same thing with the provider's authoritative output URL: stream it under a
size bound, admit only known video media types, and hand the host one
``OpenedStreamingAsset``. Only the provider name in the messages, the filename,
and the status mapping differ.

The bound and the media-type allowlist are what keep an unexpected provider
response from becoming an arbitrary file in the operator's workspace, so they
live here once instead of being re-typed per provider where they could drift
apart — the same reason ``is_public_https_url`` is shared.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from host.tools.results import OpenedStreamingAsset, StreamingAssetError
from host.tools.shared.web import WebRequestError, open_response_stream

# A render smaller than this is not a video, and the upper bound matches the
# agent asset store's own ceiling.
MIN_VIDEO_BYTES = 512
MAX_VIDEO_BYTES = 200_000_000
# Only containers the workspace can name from the response alone. An unknown
# media type is refused rather than guessed at, since the suffix decides the
# filename the operator ends up with.
VIDEO_SUFFIXES = {"video/mp4": ".mp4", "video/quicktime": ".mov"}


@contextmanager
def open_downloaded_video(
    url: str,
    *,
    provider: str,
    filename_stem: str,
    map_failure: Callable[[WebRequestError], str],
    timeout: int = 120,
) -> Iterator[OpenedStreamingAsset]:
    """Open a provider's finished video for the host to stream into the workspace.

    ``map_failure`` turns a transport failure into that provider's curated,
    secret-free message; it may also raise, which lets a package report an
    unmapped failure as a Host error instead of a vague string.
    """
    failure_message = f"{provider} video download failed."
    try:
        with open_response_stream(
            "GET", url, failure_message=failure_message, timeout=timeout
        ) as (source, response_headers):
            raw_length = response_headers.get("content-length", "")
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise StreamingAssetError(
                    f"{provider} video download did not include a valid size."
                )
            size_bytes = int(raw_length)
            if not MIN_VIDEO_BYTES <= size_bytes <= MAX_VIDEO_BYTES:
                raise StreamingAssetError(
                    f"{provider} video download size is outside the supported range."
                )
            media_type = (
                response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            suffix = VIDEO_SUFFIXES.get(media_type)
            if suffix is None:
                raise StreamingAssetError(
                    f"{provider} video download returned an unsupported media type."
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
