"""CONNECT-only public HTTPS routing. Resolve once, then pin the upstream IP."""
from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

from host.runtime.browser.client import BrowserError


def is_public(address: str) -> bool:
    value = ipaddress.ip_address(address)
    if not value.is_global or value.is_multicast or value.is_reserved:
        return False
    if isinstance(value, ipaddress.IPv6Address):
        return value in ipaddress.IPv6Network("2000::/3") and not value.sixtofour and not value.teredo
    return True


def public_addresses(host: str, port: int = 443) -> list[str]:
    try:
        addresses = list(dict.fromkeys(str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not is_public(address) for address in addresses):
            raise ValueError
        return addresses
    except (ValueError, OSError) as exc:
        raise BrowserError("Browser destination must resolve only to public IP addresses.") from exc


def target(authority: str) -> list[str]:
    try:
        value = urlsplit("https://" + authority)
        if (not value.hostname or value.port != 443 or value.username is not None or value.password is not None
                or value.path or value.query or value.fragment or any(c.isspace() for c in authority)):
            raise ValueError
        return public_addresses(value.hostname)
    except ValueError as exc:
        raise BrowserError("Browser connections require a public HTTPS destination on port 443.") from exc


def connect_public(host: str, port: int) -> socket.socket:
    return connect_addresses(public_addresses(host, port), port)


def connect_addresses(addresses: list[str], port: int) -> socket.socket:
    """Try only the already-validated numeric addresses, without resolving again."""
    last_error: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout=15)
        except OSError as exc:
            last_error = exc
    raise BrowserError("Browser connection could not reach its destination.") from last_error


def connect_proxy(endpoint: tuple[str, int], address: str, credentials: tuple[str, str]) -> socket.socket:
    stream = connect_public(*endpoint)
    try:
        # Verify the gateway before sending any proxy credentials. No HTTP or
        # certificate-verification fallback, even if the provider rejects TLS.
        stream = ssl.create_default_context().wrap_socket(stream, server_hostname=endpoint[0])
        authority = f"[{address}]:443" if ":" in address else f"{address}:443"
        headers = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        if credentials:
            encoded = base64.b64encode(":".join(credentials).encode("ascii")).decode("ascii")
            headers += f"Proxy-Authorization: Basic {encoded}\r\n"
        stream.sendall((headers + "\r\n").encode("ascii"))
        # Avoid consuming bytes of the TLS tunnel while parsing proxy headers.
        response = bytearray()
        deadline = time.monotonic() + 15
        while not response.endswith(b"\r\n\r\n") and len(response) < 16384:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError("The selected Browser proxy timed out during connection.")
            stream.settimeout(remaining)
            data = stream.recv(1)
            if not data:
                break
            response.extend(data)
        status = bytes(response).split(b"\r\n", 1)[0].split(b" ")
        if not response.endswith(b"\r\n\r\n") or len(status) < 2 or status[1] != b"200":
            code = status[1].decode("ascii", errors="ignore") if len(status) > 1 else ""
            suffix = f" (HTTP {code})" if len(code) == 3 and code.isdigit() else ""
            raise BrowserError("The selected Browser connection rejected the tunnel" + suffix + ". Check credentials, balance and provider restrictions.")
        stream.settimeout(15)
        return stream
    except Exception:
        stream.close()
        raise
