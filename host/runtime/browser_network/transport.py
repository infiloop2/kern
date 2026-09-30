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


def failure(stage: str, exc: Exception, *, host: str = "", status: int | None = None) -> BrowserError:
    """Expose connection facts, never provider text, headers or credentials."""
    context: dict[str, Any] = {"stage": stage, "host": host, "error_type": type(exc).__name__}
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        context["errno"] = exc.errno
    if status is not None:
        context["proxy_status"] = status
    suffix = f" (HTTP {status})" if status is not None else ""
    message = f"Browser connection failed during {stage.replace('_', ' ')}{suffix}. Check Host diagnostics."
    host_errors.report_warning("browser.network", message, context=context)
    return BrowserError(message)


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
        response = bytearray()
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
        raise failure(stage, exc, host=host, status=status_code) from exc
