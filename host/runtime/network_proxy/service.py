"""Localhost network policy proxy (127.0.0.1:7445), runs as kern-proxy.

All agent traffic is forced here: nftables drops direct outbound traffic for
non-root users, and the agent runs with HTTP(S)_PROXY pointing at this proxy.

The proxy is HTTPS/WSS-only: traffic arrives as CONNECT and is inspected by
terminating client TLS with a certificate signed by the Kern proxy CA,
then opening a separate TLS connection upstream. Plain HTTP is denied with a
logged 403 — no allowed destination speaks it, and the GitHub credential must
never travel an unencrypted socket.

Policy checks happen before any upstream DNS resolution or connection, so a
denied host name is never resolved (host names are otherwise a data
exfiltration channel). Every decision is recorded in the network_events table.
Requests are denied whenever the persisted policy cannot be parsed.

A connection becomes a WebSocket only when the upstream answers a handshake
with 101; a client's Upgrade header alone never turns off the per-request
checks. Every allowed WebSocket validates and bounds the client frame stream;
the owning integration then checks each complete message's content (OpenAI's
external-URL/tool rule) or explicitly allows it (an opted-in custom domain).
A violation closes the connection with a 1008 close frame.
"""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import select
import socket
import ssl
import subprocess
import threading
from typing import Any
import urllib.parse

from host.config import NetworkControls, parse_network_controls
from host.constants import LOOPBACK, PROXY_PORT
from host.network_integrations import runtime as integrations
from host.runtime.core.network_policy import load_policy
from host.runtime.core.state import (
    append_network_event,
    network_proxy_cert_files,
)


HOST = LOOPBACK
PORT = PROXY_PORT
BUFFER_SIZE = 65536
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 128 * 1024 * 1024  # bodies are buffered in memory for inspection
MAX_CONNECTIONS = 64  # cap concurrent handlers so buffered bodies cannot OOM the proxy
MAX_GENERATED_CERTS = 512  # cap the durable per-host certificate cache on the admin volume
IDLE_TIMEOUT = 310.0
CLAUDE_ATTESTATION_TIMEOUT = 10.0
CLAUDE_ATTESTATION_BODY_LIMIT = 64 * 1024


def _load_enforcement_policy() -> NetworkControls:
    """Load and validate typed integration configs for this request.

    There is deliberately no fallback cache:
    any failure — an unavailable database exactly like an invalid policy —
    propagates and the request is denied. The other enforcement inputs
    (account pins) and the decision log live in the same database, so a
    cached policy could not keep requests flowing through an outage anyway;
    denying everything until the database returns is the simple, fail-safe
    behavior.
    """
    return parse_network_controls(load_policy())


def _policy_load_denial() -> tuple[NetworkControls | None, str | None]:
    try:
        return _load_enforcement_policy(), None
    except Exception:
        return None, "network_policy_unavailable"


CERT_LOCK = threading.Lock()
CONNECTION_SLOTS = threading.BoundedSemaphore(MAX_CONNECTIONS)


def _is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def connect_public(host: str, port: int, timeout: float) -> socket.socket:
    """Resolve ``host`` and connect only if every resolved address is publicly
    routable. An allowed domain (especially a wildcard) that resolves to a
    loopback, link-local, or private address — by misconfiguration or DNS
    rebinding — must not let the proxy reach internal services (SSRF).
    Connects to the vetted address rather than re-resolving."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"could not resolve {host}") from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not isinstance(ip, str):
            raise OSError(f"resolved non-string address {ip!r} for {host}")
        if not _is_public_ip(ip):
            raise OSError(f"refusing to connect to non-public address {ip} for {host}")
        ips.append(ip)
    return socket.create_connection((ips[0], port), timeout=timeout)


def request_denial_reason(
    policy: NetworkControls,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str | None:
    """The denial code for this request, or None if it is allowed. The code is
    returned in the 403 body and logged to network events; the denial catalog
    maps it to guidance. After the core domain/method/path decision, the
    request is decided by exactly the integration that owns the host (if any)
    — see ``host.network_integrations.runtime``."""
    return integrations.request_denied(
        policy,
        method,
        host,
        path,
        query,
        headers,
        body,
        attest_claude_token_account,
    )


def attest_claude_token_account(token: str) -> str | None:
    """Return the provider-signed account uuid for one presented Claude token.

    This fixed-endpoint request runs inside the trusted proxy rather than
    recursively through it. It uses the same public-IP-only connector as
    ordinary forwarding, verifies Anthropic's TLS certificate, caps the
    response body, and returns no identity on any transport, HTTP, or parse
    failure. The bearer is used only for this call and is never persisted.
    """
    try:
        # wrap_socket detaches the raw socket on success, so the outer with
        # closes it only when the TLS handshake never took ownership.
        with connect_public(
            "api.anthropic.com", 443, timeout=CLAUDE_ATTESTATION_TIMEOUT
        ) as upstream_raw, ssl.create_default_context().wrap_socket(
            upstream_raw, server_hostname="api.anthropic.com"
        ) as upstream_tls:
            upstream_tls.settimeout(CLAUDE_ATTESTATION_TIMEOUT)
            send_http_request(
                upstream_tls,
                "GET",
                "/api/oauth/profile",
                [
                    ("Host", "api.anthropic.com"),
                    ("Authorization", f"Bearer {token}"),
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                ],
                b"",
                websocket=False,
            )
            with http.client.HTTPResponse(upstream_tls) as response:
                response.begin()
                if response.status != 200:
                    return None
                body = response.read(CLAUDE_ATTESTATION_BODY_LIMIT + 1)
                if len(body) > CLAUDE_ATTESTATION_BODY_LIMIT:
                    return None
                value = json.loads(body)
                account = value.get("account") if isinstance(value, dict) else None
                account_uuid = account.get("uuid") if isinstance(account, dict) else None
                return account_uuid.strip() if isinstance(account_uuid, str) and account_uuid.strip() else None
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError):
        return None


# Headers that carry one value and that the body guards and the upstream both
# interpret. The guards read them through a last-value-wins map while every
# original instance is forwarded, so two instances give one request two
# meanings: `Content-Encoding: gzip` followed by `Content-Encoding: identity`
# reads as uncompressed to the guard, which then sees the still-compressed
# bytes as non-JSON and passes them, while an upstream that joins duplicate
# fields decodes the gzip and acts on what the guard never inspected. There is
# no correct value to pick, so the request is denied instead.
SINGLE_VALUED_HEADERS = frozenset(
    {"content-encoding", "content-type", "content-length", "transfer-encoding", "authorization"}
)


def duplicate_header_denial(headers: list[tuple[str, str]]) -> str | None:
    seen: set[str] = set()
    for key, _value in headers:
        lower = key.lower()
        if lower not in SINGLE_VALUED_HEADERS:
            continue
        if lower in seen:
            return "duplicate_header_denied"
        seen.add(lower)
    return None


def host_header_denial(headers: list[tuple[str, str]], expected_host: str, expected_port: int) -> str | None:
    presented = [value for key, value in headers if key.lower() == "host"]
    if not presented or len(presented) != 1:
        return "host_header_invalid"
    try:
        host, port = _split_host_port(presented[0], expected_port)
    except ValueError:
        return "host_header_invalid"
    if host.lower() != expected_host.lower() or port != expected_port:
        return "host_header_invalid"
    return None


class ProxyHandler(BaseHTTPRequestHandler):
    timeout = IDLE_TIMEOUT

    def do_CONNECT(self) -> None:
        self.close_connection = True
        try:
            host, port = _split_host_port(self.path, 443)
        except ValueError:
            self.send_error(400, "CONNECT target is invalid")
            return
        # Deny before any DNS or upstream connection happens for this host.
        policy, policy_error = _policy_load_denial()
        if port != 443:
            denial = "connect_port_denied"
        elif policy_error is not None:
            denial = policy_error
        elif policy is None or not integrations.host_allowed(policy, host):
            denial = "host_not_allowed"
        else:
            denial = None
        if denial is not None:
            append_network_event("https", "CONNECT", host, port, "", "", False, denial)
            self.send_error(403, denial)
            return
        try:
            client_context = host_tls_context(host)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.send_error(502, str(exc))
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            client_tls = client_context.wrap_socket(self.connection, server_side=True)
        except OSError:
            # CONNECT already succeeded. If the client closes during the MITM
            # handshake there is no valid HTTP response channel left.
            return
        self._serve_tls_request(host, port, client_tls)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _deny_plain_http(self) -> None:
        """Plain HTTP (and WS) is not supported: every allowed destination
        speaks HTTPS/WSS via CONNECT, so an http:// request can only be a
        misconfiguration or a downgrade. Denied before any body read, DNS
        resolution, or upstream connection, and logged like any other
        denial."""
        self.close_connection = True
        host, port, path, query = "", 80, "/", ""
        try:
            parsed = urllib.parse.urlsplit(self.path)
            host = parsed.hostname or ""
            port = parsed.port or 80
            path = parsed.path or "/"
            query = parsed.query
        except ValueError:
            pass  # malformed authority: log the denial with the defaults
        denial = "plain_http_denied"
        append_network_event("http", self.command, host, port, path, query, False, denial)
        self.send_error(403, denial)

    # Every plain (non-CONNECT) method is denied the same way.
    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _deny_plain_http

    def _serve_tls_request(self, host: str, port: int, client_tls: ssl.SSLSocket) -> None:
        """Read one decrypted request from the client, decide, then connect
        upstream and forward. The forced ``Connection: close`` keeps the
        connection to a single policy-checked request."""
        upstream_tls = None
        try:
            client_tls.settimeout(IDLE_TIMEOUT)
            reader = SocketReader(client_tls)
            method, target, headers = read_request_head(reader)
            # Origin-form only: every real client sends origin-form inside a
            # CONNECT tunnel, and a leading "/" cannot carry a scheme or
            # authority, so nothing needs re-vetting against the tunnel host.
            # Anything else (absolute-form, authority-form, garbage) is denied
            # outright — fail closed, one reason. Strict origin-form also
            # rejects a "//" prefix and any "#": urlsplit reads those bytes as
            # an authority and a fragment, invisible to the path guards and
            # the audit row, while the request line below would forward them
            # verbatim (a guard would see /health for a wire target of
            # /health#/../../admin).
            path, query = "/", ""
            target_denial = None
            if target.startswith("/") and not target.startswith("//") and "#" not in target:
                parsed = urllib.parse.urlsplit(target)
                path = parsed.path or "/"
                query = parsed.query
            else:
                target_denial = "request_target_invalid"
            is_websocket = any(key.lower() == "upgrade" and value.lower() == "websocket" for key, value in headers)
            protocol = "wss" if is_websocket else "https"
            body, body_deny = read_body(reader, headers)
            policy, policy_error = _policy_load_denial()
            guard_denial = (
                body_deny
                or target_denial
                or duplicate_header_denial(headers)
                or host_header_denial(headers, host, port)
                or policy_error
            )
            if guard_denial is not None:
                append_network_event(protocol, method, host, port, path, query, False, guard_denial)
                message = guard_denial.encode()
                client_tls.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: "
                    + str(len(message)).encode()
                    + b"\r\n\r\n"
                    + message
                )
                return
            assert policy is not None
            denial = request_denial_reason(policy, method, host, path, query, headers, body)
            if denial is None and is_websocket and not integrations.websocket_allowed(policy, host):
                denial = "websocket_not_allowed"
            # The owning integration's gate runs after the deny decision
            # passes: a gated push that changes .github/ is answered with a
            # git report-status ("queued for approval") instead of being
            # forwarded.
            gate_response = None
            if denial is None:
                gate_response, gate_denial = integrations.gate_response(policy, method, host, path, body)
                if gate_denial is not None:
                    denial = gate_denial
            # Ordinary HTTP is decided entirely by the request guards. A
            # WebSocket is not allowed until the upstream confirms the
            # handshake with 101, so defer its successful event until then.
            if not is_websocket or denial is not None:
                append_network_event(
                    protocol, method, host, port, path, query, denial is None, denial
                )
            if gate_response is not None:
                client_tls.sendall(gate_response)
                return
            if denial is not None:
                message = denial.encode()
                client_tls.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: "
                    + str(len(message)).encode()
                    + b"\r\n\r\n"
                    + message
                )
                return
            # The owning integration may passively observe the upstream
            # response of an allowed request (Bedrock token-usage metering);
            # the relayed bytes are never modified. Selected from the
            # as-received headers, before the rewrite below replaces the
            # routing identity that attributes the request to its runtime.
            meter = integrations.response_meter(policy, method, host, path, query, headers, body)
            # After the allow decision, the owning integration may rewrite
            # headers: on GitHub domains the proxy authenticates the request
            # itself (agent Authorization stripped, the working token
            # injected), and on Bedrock domains it re-signs the request with
            # the operator's credential — the agent never holds either.
            headers = integrations.rewrite_request_headers(policy, method, host, path, query, headers, body)
            upstream_raw = connect_public(host, port, timeout=15)
            upstream_tls = ssl.create_default_context().wrap_socket(upstream_raw, server_hostname=host)
            upstream_tls.settimeout(IDLE_TIMEOUT)
            send_http_request(
                upstream_tls,
                method,
                target,
                headers,
                body,
                websocket=is_websocket,
            )
            if not is_websocket:
                forward_until_close(upstream_tls, client_tls, meter)
                return
            # A client header alone does not make a WebSocket. Only the
            # upstream's 101 does, and until it arrives this is still an
            # ordinary HTTP connection that must not become an opaque tunnel:
            # an upstream that ignores the Upgrade — every plain HTTP/1.1
            # server does — would otherwise leave a keep-alive socket relaying
            # unchecked bytes past every guard.
            upstream_reader = SocketReader(upstream_tls)
            status, _response_headers, response_head = read_response_head(upstream_reader)
            while 100 <= status < 200 and status != 101:
                # Optional informational heads do not decide the upgrade.
                status, _response_headers, response_head = read_response_head(upstream_reader)
            if status != 101:
                # Upgrade handling is deliberately not a second general HTTP
                # client. A fixed proxy error avoids duplicating response-body
                # framing solely for a failed upgrade.
                reason = b"websocket_upgrade_declined"
                append_network_event(
                    protocol, method, host, port, path, query, False, reason.decode()
                )
                client_tls.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: "
                    + str(len(reason)).encode()
                    + b"\r\n\r\n"
                    + reason
                )
                return
            append_network_event(protocol, method, host, port, path, query, True, None)
            client_tls.sendall(response_head)
            # drain(): frames each side pipelined behind the handshake.
            tunnel_websocket(
                client_tls, upstream_tls, policy, protocol, host, port, path,
                initial_client_bytes=reader.drain(),
                initial_upstream_bytes=upstream_reader.drain(),
            )
        except OSError:
            pass
        finally:
            if upstream_tls is not None:
                upstream_tls.close()
            client_tls.close()


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    if ":" not in authority:
        return authority, default_port
    host, port = authority.rsplit(":", 1)
    return host, int(port)



CERT_SUFFIXES = (".crt", ".key", ".csr", ".ext")


def evict_generated_certs(directory: Path, keep: int) -> None:
    """Drop the oldest generated certificates so the cache stays bounded. A
    wildcard rule allows unlimited distinct subdomains and each one mints a
    durable file family on the admin volume that Postgres shares, so without a
    cap an agent can fill the volume and take egress down with it. Families are
    counted by any member rather than by the certificate: a mint that fails
    partway leaves a key with no certificate, and those would otherwise
    accumulate outside the cap. Eviction only costs the evicted host one keygen
    if it is ever seen again; the cap is far above any real working set."""
    newest: dict[str, float] = {}
    for path in directory.iterdir():
        for suffix in CERT_SUFFIXES:
            if path.name.endswith(suffix):
                stem = path.name[: -len(suffix)]
                newest[stem] = max(newest.get(stem, 0.0), path.stat().st_mtime)
                break
    for stem in sorted(newest, key=lambda name: newest[name])[: max(0, len(newest) - keep)]:
        for suffix in CERT_SUFFIXES:
            (directory / f"{stem}{suffix}").unlink(missing_ok=True)


def _mint_host_cert(certs: Any, host: str) -> None:
    """Generate one host key/certificate family, removing every partial file if
    any step fails: a half-written family is unusable, and leaving it behind
    would put files on the volume that nothing ever replaces."""
    try:
        subprocess.run(
            ["/usr/bin/openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(certs.key), "-out", str(certs.csr), "-subj", f"/CN={host}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        certs.ext.write_text(f"subjectAltName=DNS:{host}\n")
        subprocess.run(
            ["/usr/bin/openssl", "x509", "-req", "-in", str(certs.csr),
             "-CA", str(certs.ca_cert), "-CAkey", str(certs.ca_key), "-CAcreateserial",
             "-out", str(certs.cert), "-days", "365", "-sha256", "-extfile", str(certs.ext)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        certs.key.chmod(0o600)
    except BaseException:
        for path in (certs.cert, certs.key, certs.csr, certs.ext):
            path.unlink(missing_ok=True)
        raise


def host_tls_context(host: str) -> ssl.SSLContext:
    """The server-side TLS context for one intercepted host, minting the
    certificate first if it is not cached. The files are loaded while CERT_LOCK
    is still held, because eviction runs under the same lock: a concurrent miss
    must not be able to unlink them between the lookup here and the load."""
    certs = network_proxy_cert_files(host)
    with CERT_LOCK:
        if not (certs.cert.exists() and certs.key.exists()):
            certs.directory.mkdir(parents=True, exist_ok=True)
            evict_generated_certs(certs.directory, MAX_GENERATED_CERTS - 1)
            _mint_host_cert(certs, host)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certs.cert), str(certs.key))
        return context


class SocketReader:
    """Minimal buffered reader over a socket. Unlike ``makefile`` it can hand
    back any unconsumed bytes, which matters when the connection turns into an
    upgraded frame stream after a WebSocket handshake."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    def readline(self, limit: int = MAX_HEADER_BYTES) -> bytes:
        while b"\n" not in self._buffer:
            if len(self._buffer) > limit:
                raise OSError("header line too long")
            chunk = self._sock.recv(BUFFER_SIZE)
            if not chunk:
                line, self._buffer = self._buffer, b""
                return line
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line + b"\n"

    def read(self, amount: int) -> bytes:
        while len(self._buffer) < amount:
            chunk = self._sock.recv(BUFFER_SIZE)
            if not chunk:
                break
            self._buffer += chunk
        data, self._buffer = self._buffer[:amount], self._buffer[amount:]
        return data

    def drain(self) -> bytes:
        data, self._buffer = self._buffer, b""
        return data


def read_request_head(reader: Any) -> tuple[str, str, list[tuple[str, str]]]:
    """Parse the request line and headers from anything with ``readline``."""
    request_line = reader.readline(MAX_HEADER_BYTES)
    if not request_line.strip():
        raise OSError("connection closed before request line")
    try:
        method, target, version = request_line.decode("iso-8859-1").strip().split(" ", 2)
    except ValueError as exc:
        raise OSError("malformed request line") from exc
    if not method or not target or not version.startswith("HTTP/"):
        raise OSError("malformed request line")
    headers: list[tuple[str, str]] = []
    total = len(request_line)
    while True:
        line = reader.readline(MAX_HEADER_BYTES)
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise OSError("request headers too large")
        if line in (b"\r\n", b"\n", b""):
            break
        if b":" in line:
            key, value = line.decode("iso-8859-1").split(":", 1)
            headers.append((key.strip(), value.strip()))
    return method.upper(), target, headers


def read_body(reader: Any, headers: list[tuple[str, str]]) -> tuple[bytes, str | None]:
    """Read the full request body (Content-Length or chunked) from anything
    with ``read``/``readline``. Returns (body, denial code). The body is
    buffered and capped so the policy check always sees it completely."""
    header_map = {key.lower(): value for key, value in headers}
    if "chunked" in header_map.get("transfer-encoding", "").lower():
        return read_chunked_body(reader)
    try:
        length = int(header_map.get("content-length", "0") or "0")
    except ValueError:
        return b"", "request_body_malformed"
    if length < 0:
        return b"", "request_body_malformed"
    if length > MAX_BODY_BYTES:
        return b"", "request_body_too_large"
    return reader.read(length), None


def read_chunked_body(reader: Any) -> tuple[bytes, str | None]:
    body = b""
    while True:
        size_line = reader.readline(MAX_HEADER_BYTES)
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return b"", "request_body_malformed"
        if size == 0:
            while True:  # consume optional trailers up to the blank line
                line = reader.readline(MAX_HEADER_BYTES)
                if line in (b"\r\n", b"\n", b""):
                    break
            return body, None
        if len(body) + size > MAX_BODY_BYTES:
            return b"", "request_body_too_large"
        body += reader.read(size)
        reader.read(2)  # CRLF after each chunk


def send_http_request(
    upstream: socket.socket,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes,
    *,
    websocket: bool,
) -> None:
    """Forward the request. The body was fully read (and de-chunked) for
    inspection, so it is re-sent with an explicit Content-Length. WebSocket
    handshakes keep their Connection/Upgrade headers; everything else is
    pinned to Connection: close so the upstream socket carries exactly one
    policy-checked request.
"""
    upstream.sendall(f"{method} {target} HTTP/1.1\r\n".encode("ascii"))
    had_body_header = False
    for key, value in headers:
        lower = key.lower()
        if lower in {"content-length", "transfer-encoding"}:
            had_body_header = True
            continue
        if lower in {"proxy-connection", "proxy-authorization"}:
            continue
        if lower == "sec-websocket-extensions":
            # Never forward the client's extension offer (e.g.
            # permessage-deflate): an accepted extension compresses frames and
            # sets RSV bits, which the message guard cannot inspect and would
            # deny mid-stream. With the offer dropped, neither side negotiates
            # an extension and the frames stay plain.
            continue
        if lower == "sec-websocket-key" and not websocket:
            # The random nonce is structural only on a real handshake. Do not
            # let an ordinary HTTP request reuse the exempt field as data.
            continue
        if lower == "connection" and not websocket:
            continue
        upstream.sendall(f"{key}: {value}\r\n".encode("iso-8859-1"))
    if body or had_body_header:
        upstream.sendall(f"Content-Length: {len(body)}\r\n".encode("ascii"))
    if not websocket:
        upstream.sendall(b"Connection: close\r\n")
    upstream.sendall(b"\r\n")
    if body:
        upstream.sendall(body)


def read_response_head(reader: Any) -> tuple[int, list[tuple[str, str]], bytes]:
    """Parse an upstream status line and headers from anything with
    ``readline``. Returns the status code, the parsed headers, and the raw
    bytes exactly as they arrived so the caller can relay the response
    unchanged whatever it decides about it."""
    status_line = reader.readline(MAX_HEADER_BYTES)
    if not status_line.strip():
        raise OSError("connection closed before status line")
    # The reason phrase is optional, so only the version and code are required.
    parts = status_line.decode("iso-8859-1").strip().split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise OSError("malformed status line")
    code = parts[1]
    headers: list[tuple[str, str]] = []
    raw = bytearray(status_line)
    while True:
        line = reader.readline(MAX_HEADER_BYTES)
        raw += line
        if len(raw) > MAX_HEADER_BYTES:
            raise OSError("response headers too large")
        if line in (b"\r\n", b"\n", b""):
            break
        if b":" in line:
            key, value = line.decode("iso-8859-1").split(":", 1)
            headers.append((key.strip(), value.strip()))
    return int(code), headers, bytes(raw)


def forward_until_close(source: socket.socket, target: socket.socket, meter: Any = None) -> None:
    """Relay upstream bytes to the client until close. ``meter`` (feed/finish)
    observes the raw bytes without touching the relay; finish() runs even on
    an aborted relay so the request is still counted."""
    try:
        data = source.recv(BUFFER_SIZE)
        while data:
            if meter is not None:
                meter.feed(data)
            target.sendall(data)
            data = source.recv(BUFFER_SIZE)
    finally:
        source.close()
        if meter is not None:
            meter.finish()


class WebSocketDenied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WebSocketClientGuard:
    """Parses client→upstream WebSocket frames so each complete text/binary
    message can be policy-checked before it is forwarded. RFC 6455 requires
    client frames to be masked and extensions are not negotiated by our
    clients, so anything malformed, extension-compressed (RSV bits), or
    oversized is denied rather than blindly forwarded — the same fail-closed
    posture as the HTTP body guard."""

    def __init__(self, message_denied: Any) -> None:
        self._message_denied = message_denied
        self._buffer = bytearray()
        self._message = bytearray()
        self._frames: list[bytes] = []  # raw frames of the in-progress message
        self._fragmented = False

    def feed(self, data: bytes) -> bytes:
        """Consume client bytes; return the frames cleared for forwarding.
        Raises WebSocketDenied when a message violates policy or the stream
        cannot be safely inspected."""
        self._buffer.extend(data)
        cleared = bytearray()
        while (frame := self._next_frame()) is not None:
            raw, fin, opcode, payload = frame
            if opcode in (0x8, 0x9, 0xA):  # close/ping/pong pass through
                cleared += raw
                continue
            if opcode == 0x0:  # continuation
                if not self._fragmented:
                    raise WebSocketDenied("websocket_uninspectable")
            elif opcode in (0x1, 0x2):  # text/binary
                if self._fragmented:
                    raise WebSocketDenied("websocket_uninspectable")
            else:
                raise WebSocketDenied("websocket_uninspectable")
            self._message.extend(payload)
            if len(self._message) > MAX_BODY_BYTES:
                raise WebSocketDenied("websocket_uninspectable")
            self._frames.append(raw)
            self._fragmented = not fin
            if fin:
                denial = self._message_denied(bytes(self._message))
                if denial is not None:
                    raise WebSocketDenied(denial)
                cleared += b"".join(self._frames)
                self._frames.clear()
                self._message.clear()
        return bytes(cleared)

    def _next_frame(self) -> tuple[bytes, bool, int, bytes] | None:
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        if buffer[0] & 0x70:
            raise WebSocketDenied("websocket_uninspectable")
        fin = bool(buffer[0] & 0x80)
        opcode = buffer[0] & 0x0F
        if not buffer[1] & 0x80:
            raise WebSocketDenied("websocket_uninspectable")
        length = buffer[1] & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length, offset = int.from_bytes(buffer[2:4], "big"), 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length, offset = int.from_bytes(buffer[2:10], "big"), 10
        if length > MAX_BODY_BYTES:
            raise WebSocketDenied("websocket_uninspectable")
        total = offset + 4 + length
        if len(buffer) < total:
            return None
        raw = bytes(buffer[:total])
        mask = raw[offset : offset + 4]
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw[offset + 4 :]))
        del buffer[:total]
        return raw, fin, opcode, payload


def _websocket_close_frame(status_code: int, reason: str) -> bytes:
    payload = status_code.to_bytes(2, "big") + reason.encode()[:120]
    return bytes([0x88, len(payload)]) + payload


def tunnel_websocket(
    client: socket.socket,
    upstream: socket.socket,
    policy: NetworkControls,
    protocol: str,
    host: str,
    port: int,
    path: str,
    initial_client_bytes: bytes = b"",
    initial_upstream_bytes: bytes = b"",
) -> None:
    """Relay a WebSocket connection whose 101 the caller has already verified.
    Every client→upstream message passes through the common frame validator
    and the owning integration's content decision before forwarding. OpenAI
    supplies its external-URL/tool guard; custom domains use a no-op content
    decision after the operator explicitly opts them in. A violation is
    logged, answered with a 1008 close frame, and ends the connection.
    Upstream→client frames pass through untouched."""
    guard = WebSocketClientGuard(integrations.ws_message_guard(policy, host))
    try:
        if initial_upstream_bytes:
            client.sendall(initial_upstream_bytes)
        if initial_client_bytes:
            cleared = guard.feed(initial_client_bytes)
            if cleared:
                upstream.sendall(cleared)
        sockets = [client, upstream]
        while True:
            readable, _, errors = select.select(sockets, [], sockets, IDLE_TIMEOUT)
            if errors or not readable:
                return
            for source in readable:
                data = source.recv(BUFFER_SIZE)
                if not data:
                    return
                if source is client:
                    cleared = guard.feed(data)
                    if cleared:
                        upstream.sendall(cleared)
                else:
                    client.sendall(data)
    except WebSocketDenied as denied:
        append_network_event(protocol, "MESSAGE", host, port, path, "", False, denied.code)
        try:
            client.sendall(_websocket_close_frame(1008, denied.code))
        except OSError:
            pass
    finally:
        upstream.close()
        client.close()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Cap concurrent connections. Each handler may buffer up to MAX_BODY_BYTES
    for inspection, so without a cap the untrusted agent could open many large
    POSTs at once and OOM the proxy — its only sanctioned network path."""

    def process_request(self, request, client_address):
        if not CONNECTION_SLOTS.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The slot is normally released by process_request_thread; if the
            # handler thread could not even start (e.g. resource exhaustion),
            # release here or the slot leaks and the proxy eventually drops
            # every connection — the agent's only network path.
            CONNECTION_SLOTS.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            CONNECTION_SLOTS.release()


def main() -> int:
    BoundedThreadingHTTPServer((HOST, PORT), ProxyHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
