"""Small HTTPS helper shared by deterministic host-inference providers."""

from __future__ import annotations

import urllib.error
import urllib.request


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTP errors before credentials can leave the URL."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects())


def post(
    url: str,
    *,
    headers: dict[str, str],
    data: bytes,
    timeout: float,
    max_bytes: int,
    label: str,
) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{label} failed with HTTP {exc.code}") from exc
    if len(raw) > max_bytes:
        raise ValueError(f"{label} response is too large")
    return raw
