"""HTTPS CONNECT transport. The Browser UID firewall owns host egress policy."""
from __future__ import annotations

import base64
import socket
import ssl
import time
from urllib.parse import urlsplit
from typing import Any

from host.runtime.browser.client import BrowserError
from host.runtime.core import host_errors


class ConnectionFailure(BrowserError):
    """A sanitized connection error safe to return through the local relay."""


class SessionEnded(ConnectionFailure):
    """Decodo explicitly rejected CONNECT because its sticky session ended."""


def failure(stage: str, exc: Exception, *, host: str = "", status: int | None = None,
            detail: str = "", facts: dict[str, Any] | None = None) -> ConnectionFailure:
    """Expose connection facts and the redacted Decodo error header."""
    context: dict[str, Any] = {"stage": stage, "host": host, "error_type": type(exc).__name__}
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        context["errno"] = exc.errno
    if status is not None:
        context["proxy_status"] = status
    if detail:
        context["proxy_error"] = detail
    context.update(facts or {})
    suffix = f" (HTTP {status})" if status is not None else ""
    message = f"Browser connection failed during {stage.replace('_', ' ')}{suffix}."
    message += f" Decodo: {detail}" if detail else " Check Host diagnostics."
    host_errors.report_warning("browser.network", message, context=context)
    return ConnectionFailure(message)


def redact(value: str, credentials: tuple[str, str]) -> str:
    """Replace known credential strings before any diagnostic truncation."""
    username, password = credentials
    secrets = [username, password, base64.b64encode(":".join(credentials).encode("ascii")).decode("ascii")]
    secrets.append(username.removeprefix("user-").split("-country-", 1)[0])
    if "-session-" in username:
        secrets.append(username.split("-session-", 1)[1].split("-", 1)[0])
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def proxy_details(response: bytes, credentials: tuple[str, str]) -> dict[str, Any]:
    # Ignore an incomplete trailing line, which might end partway through a secret.
    lines = redact(response.decode("utf-8", errors="replace"), credentials).split("\r\n")[:-1]
    details: dict[str, Any] = {"proxy_response_bytes": len(response),
                              "proxy_headers_complete": response.endswith(b"\r\n\r\n")}
    if not lines:
        return details
    details["proxy_status_line"] = lines[0]
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        if name.lower() in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
            line = f"{name}: [redacted]"
        headers.append(line)
    details["proxy_header_count"] = len(headers)
    for line in headers:
        name, _, value = line.partition(":")
        if name.lower() == "x-error-message":
            details["proxy_error"] = "".join(c if " " <= c <= "~" else "?" for c in value).strip()[:512]
            break
    # Use separate fields so the existing per-field and whole-record diagnostic
    # limits retain several headers, including authentication and request IDs.
    details.update({f"proxy_header_{i + 1}": line for i, line in enumerate(headers[:16])})
    return details


def target(authority: str) -> str:
    """Accept an HTTPS authority; leave DNS and address selection to the dialer."""
    try:
        value = urlsplit("https://" + authority)
        if (not value.hostname or value.port != 443 or value.username is not None or value.password is not None
                or value.path or value.query or value.fragment or any(c.isspace() for c in authority)):
            raise ValueError
        return value.hostname
    except ValueError as exc:
        raise BrowserError("Browser connections require an HTTPS destination on port 443.") from exc


def connect_proxy(endpoint: tuple[str, int], host: str, credentials: tuple[str, str]) -> socket.socket:
    stream: socket.socket | None = None
    stage, status_code = "proxy_connection", None
    started = time.monotonic()
    response = bytearray()
    try:
        stream = socket.create_connection(endpoint, timeout=10)
        # Verify the gateway before sending any proxy credentials. No HTTP or
        # certificate-verification fallback, even if the provider rejects TLS.
        stage = "proxy_tls"
        stream = ssl.create_default_context().wrap_socket(stream, server_hostname=endpoint[0])
        stage = "proxy_connect"
        authority = f"[{host}]:443" if ":" in host else f"{host}:443"
        headers = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        encoded = base64.b64encode(":".join(credentials).encode("ascii")).decode("ascii")
        headers += f"Proxy-Authorization: Basic {encoded}\r\n\r\n"
        stream.sendall(headers.encode("ascii"))
        # Avoid consuming bytes of the TLS tunnel while parsing proxy headers.
        deadline = time.monotonic() + 15
        while not response.endswith(b"\r\n\r\n") and len(response) < 16384:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            stream.settimeout(remaining)
            data = stream.recv(1)
            if not data:
                break
            response.extend(data)
        status = bytes(response).split(b"\r\n", 1)[0].split(b" ")
        code = status[1].decode("ascii", errors="ignore") if len(status) > 1 else ""
        if len(code) == 3 and code.isdigit():
            status_code = int(code)
        if not response.endswith(b"\r\n\r\n") or code != "200":
            raise BrowserError("Proxy rejected CONNECT.")
        stream.settimeout(15)
        return stream
    except Exception as exc:
        if stream is not None:
            stream.close()
        facts = {
            "gateway": f"{endpoint[0]}:{endpoint[1]}",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error_message": redact(str(exc), credentials),
            **proxy_details(bytes(response), credentials),
        }
        error = failure(stage, exc, host=host, status=status_code,
                        detail=facts.get("proxy_error", ""), facts=facts)
        # Classify only complete gateway CONNECT headers, before diagnostic
        # redaction/truncation. Never retry generic 502s or tunneled requests.
        if stage == "proxy_connect" and status_code == 502 and response.endswith(b"\r\n\r\n"):
            for line in bytes(response).split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.lower() == b"x-error-message" and any(
                    reason in value.lower() for reason in (b"the session has ended", b"the session has failed")
                ):
                    raise SessionEnded(str(error)) from exc
        raise error from exc
