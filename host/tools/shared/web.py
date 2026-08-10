"""Provider-neutral HTTP helpers for tool packages.

Every bundled tool makes plain HTTPS calls with urllib; this module holds the
shared request/response plumbing so each package only describes its provider's
protocol. Error messages raised here are caller-supplied and user/agent
visible, so callers keep them free of secrets and raw provider bodies.
"""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import ipaddress
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import BinaryIO, Iterable, Iterator, cast

from host.tools.json_types import JSONObject, JSONValue

DEFAULT_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Raised when a provider body exceeds the cap. Fail loudly rather than
# silently truncating a partial body into a "malformed response" further down.
RESPONSE_TOO_LARGE_MESSAGE = "The provider returned a response larger than the allowed size."


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript constants that Python's decoder accepts by default.

    NaN and infinities are not JSON values. Letting one into a tool result can
    make the later host/MCP serialization fail even though the provider body
    appeared to parse successfully.
    """
    raise ValueError(f"Invalid JSON constant: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON number is outside the finite float range: {value}")
    return parsed


class WebRequestError(RuntimeError):
    """An HTTP-level failure with the status code preserved so callers can map
    provider-specific statuses (401 reconnect, 429 rate limit) without
    exposing the raw body across the tool boundary."""

    def __init__(self, message: str, *, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ProviderWarning(RuntimeError):
    """A provider failure that should be visible in Host diagnostics.

    The exception message is curated for the agent/operator result. A bounded
    provider response is retained separately for the authenticated operator
    diagnostic and is never included in ``str(exc)``.
    """

    def __init__(
        self,
        provider: str,
        operation: str,
        message: str,
        *,
        status: int = 0,
        body: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.status = status
        self.response_body = body.decode("utf-8", "replace").strip()


class UnmappedProviderError(ProviderWarning):
    """A provider failure with no curated user-facing mapping.

    Tool packages let it reach the host boundary, which records a warning in
    Host diagnostics and returns a generic failure to the agent. Generic
    provider bodies remain discarded; tool-specific mapped warnings may retain
    a bounded response when that provider integration deliberately opts in.
    """

    def __init__(
        self,
        provider: str,
        operation: str,
        *,
        status: int = 0,
    ) -> None:
        transport = f"HTTP {status}" if status else "transport error"
        super().__init__(
            provider,
            operation,
            f"Unmapped {provider} {operation} provider failure ({transport}).",
            status=status,
        )


def known_provider_transport_error(exc: WebRequestError) -> str:
    """Return a curated status-free transport failure, if one is known."""

    return RESPONSE_TOO_LARGE_MESSAGE if str(exc) == RESPONSE_TOO_LARGE_MESSAGE else ""


def unmapped_provider_error(provider: str, operation: str, exc: WebRequestError) -> UnmappedProviderError:
    """Create an operator-diagnostic failure with a generic agent result."""

    return UnmappedProviderError(provider, operation, status=exc.status)


def provider_warning(
    provider: str,
    operation: str,
    exc: WebRequestError,
    message: str,
) -> ProviderWarning:
    """Create a provider warning with a curated agent-facing message."""

    return ProviderWarning(
        provider,
        operation,
        message,
        status=exc.status,
        body=exc.body,
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    ``urllib``'s default handler replays request headers — including
    ``Authorization`` — to the redirect target and accepts non-HTTPS targets,
    so a provider open-redirect could exfiltrate a bearer token or reach an
    internal host (SSRF). Tool endpoints are fixed API URLs that should never
    redirect, so any 3xx is surfaced as a failure the caller maps to its
    ``failure_message`` rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


# One shared opener with redirects disabled and no proxy handler. Built once so
# every request_bytes call gets the same hardened behavior.
_OPENER = urllib.request.build_opener(_NoRedirectHandler, urllib.request.ProxyHandler({}))


@contextmanager
def open_response_stream(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    failure_message: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[tuple[BinaryIO, dict[str, str]]]:
    """Open a redirect-free HTTPS response for bounded streaming by a caller.

    The caller validates Content-Length and media type before consuming bytes.
    Raw provider errors and signed URLs never escape this helper.
    """
    if not url.startswith("https://"):
        raise WebRequestError(failure_message)
    request = urllib.request.Request(url, headers=dict(headers or {}), method=method)
    try:
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            pass
        raise WebRequestError(failure_message, status=exc.code, body=body) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        raise WebRequestError(failure_message) from exc
    try:
        with response:
            response_headers = {name.lower(): value for name, value in response.headers.items()}
            yield cast(BinaryIO, response), response_headers
    except WebRequestError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # Network failures while the caller reads remain provider failures.
        # Caller exceptions such as ValueError pass through unchanged.
        raise WebRequestError(failure_message) from exc


def encode_query(params: Mapping[str, str]) -> str:
    return urllib.parse.urlencode(params)


def is_public_https_url(value: str) -> bool:
    """Structural checks only — this does NOT pin the hostname.

    Media providers return upload and output URLs from their own authenticated
    HTTPS API responses and generally do not document which asset/CDN hosts
    those URLs use (presigned object-store or CDN hosts are likely), so pinning
    a domain would risk rejecting legitimate provider traffic. What is enforced:
    plain HTTPS on the default port to a named public host — no userinfo, no IP
    literals, no oversized URLs.
    """
    if len(value) > 2_048:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        pass
    return (
        parsed.scheme == "https"
        and "." in hostname
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def request_bytes(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    failure_message: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes:
    body, _ = _request_bytes_and_headers(
        method,
        url,
        headers=headers,
        data=data,
        failure_message=failure_message,
        timeout=timeout,
        max_bytes=max_bytes,
    )
    return body


def _request_bytes_and_headers(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    failure_message: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[bytes, dict[str, str]]:
    """The one urllib request path behind request_bytes and the json_request
    entry points, also returning the response headers with
    lower-cased names — some providers return created-resource ids only in a
    header (e.g. LinkedIn's x-restli-id). Bodies over ``max_bytes`` fail with
    RESPONSE_TOO_LARGE_MESSAGE rather than being silently truncated; callers
    that expect large bodies (e.g. base64 image data) pass a bigger cap."""
    if not url.startswith("https://"):
        raise WebRequestError(failure_message)
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            response_headers = {name.lower(): value for name, value in response.headers.items()}
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise WebRequestError(RESPONSE_TOO_LARGE_MESSAGE)
            return body, response_headers
    except WebRequestError:
        raise
    except urllib.error.HTTPError as exc:
        # A refused redirect (_NoRedirectHandler) also arrives here as a 3xx
        # HTTPError; it is mapped to failure_message like any other HTTP error.
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            pass
        raise WebRequestError(failure_message, status=exc.code, body=body) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        # Post-connect failures (socket timeout, IncompleteRead, header
        # validation on hostile values) must not escape as raw exceptions whose
        # text can include the full URL with signed query credentials.
        raise WebRequestError(failure_message) from exc


def stream_request_bytes(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: BinaryIO | Iterable[bytes],
    content_length: int,
    failure_message: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes:
    """Send a known-length streaming body without materializing it in memory.

    This is used only for provider upload protocols. The URL must be HTTPS;
    redirects are returned as failures instead of followed, and the response is
    bounded like the urllib helpers above.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise WebRequestError(failure_message)
    port = parsed.port or 443
    connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    request_headers = dict(headers)
    request_headers["Content-Length"] = str(content_length)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read(max_bytes + 1)
        if len(response_body) > max_bytes:
            raise WebRequestError(RESPONSE_TOO_LARGE_MESSAGE)
        if not 200 <= response.status < 300:
            raise WebRequestError(
                failure_message, status=response.status, body=response_body[:4096]
            )
        return response_body
    except WebRequestError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise WebRequestError(failure_message) from exc
    finally:
        connection.close()


def json_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: JSONObject | None = None,
    form: Mapping[str, str] | None = None,
    failure_message: str,
    invalid_response_message: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> JSONObject:
    decoded, _ = json_request_with_headers(
        method,
        url,
        headers=headers,
        body=body,
        form=form,
        failure_message=failure_message,
        invalid_response_message=invalid_response_message,
        timeout=timeout,
        max_bytes=max_bytes,
    )
    return decoded


def json_request_with_headers(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: JSONObject | None = None,
    form: Mapping[str, str] | None = None,
    failure_message: str,
    invalid_response_message: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[JSONObject, dict[str, str]]:
    """One JSON-in/JSON-out HTTP call, also returning response headers
    (lower-cased names). ``body`` sends JSON, ``form`` sends URL-encoded form
    data; at most one may be provided."""
    if body is not None and form is not None:
        raise ValueError("json_request accepts body or form, not both.")
    request_headers = {"accept": "application/json", **(headers or {})}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request_headers.setdefault("content-type", "application/x-www-form-urlencoded")
    raw, response_headers = _request_bytes_and_headers(
        method,
        url,
        headers=request_headers,
        data=data,
        failure_message=failure_message,
        timeout=timeout,
        max_bytes=max_bytes,
    )
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(invalid_response_message) from exc
    if not text:
        return {}, response_headers
    try:
        decoded = json.loads(
            text,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(invalid_response_message) from exc
    if isinstance(decoded, list):
        # Several providers return a bare JSON array for list endpoints; wrap it
        # so callers always work with an object.
        return {"items": cast(list[JSONValue], decoded)}, response_headers
    if not isinstance(decoded, dict):
        raise RuntimeError(invalid_response_message)
    return cast(JSONObject, decoded), response_headers
