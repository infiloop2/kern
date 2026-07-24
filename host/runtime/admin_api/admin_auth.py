"""Admin login sessions and brute-force throttling for the localhost admin API.

The admin API's own password login is the authentication boundary: it is
reached over the optional Cloudflare Tunnel (or an SSH port forward) with no
Cloudflare Access gate in front, so this module hardens that login to stand
alone. A correct password mints an opaque, server-held session token delivered
as an ``HttpOnly``, ``SameSite=Strict`` cookie, so the raw admin password never
lands in a browser-readable store and a stolen token is bounded by idle and
absolute lifetimes. Login attempts are throttled per source: once a source uses
its per-window budget it is fully blocked with a ``429`` (even a correct password
is refused) until the window clears. The block is per-source only, never global,
so it cannot lock every operator out at once, and the strong generated password
is what ultimately defeats brute force.

Everything here is process-local, in-memory, and thread-safe (the admin API is a
``ThreadingHTTPServer``, so every request runs on its own thread). A host
restart clears sessions and throttle state; the operator simply logs in again,
and no durable state can go stale. Timing uses a monotonic clock so a wall-clock
change never extends a session or a lockout.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time


# The session cookie the browser holds after login. Opaque and server-held: it
# never carries the password or any decodable claim. Over HTTPS the cookie uses
# the ``__Host-`` prefix, which the browser only accepts when it is Secure,
# Path=/, and has no Domain; a sibling agent origin on the shared ``trustyclaw.me``
# parent domain therefore cannot set or shadow it (cookie-tossing defense). The
# plain name is used only over the plain-HTTP SSH-forward loopback, where
# ``__Host-`` cannot apply (it requires Secure) and localhost has no siblings.
HOST_SESSION_COOKIE_NAME = "__Host-tc_admin_session"
SESSION_COOKIE_NAME = "tc_admin_session"
# Cookie-authenticated requests must also carry this header. Same-origin admin
# UI JavaScript sets it on every request; a cross-site page cannot (adding it to
# a cross-origin request forces a CORS preflight the admin API never answers),
# so the ``SameSite=Strict`` cookie and this required header together close CSRF.
CSRF_HEADER_NAME = "X-TrustyClaw-Csrf"

# A session is dropped after this much idle time, or this much total age,
# whichever comes first. The absolute cap bounds a stolen token's usefulness
# even while it is actively refreshed.
SESSION_IDLE_TIMEOUT_SECONDS = 12 * 60 * 60
SESSION_ABSOLUTE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
# Bound the in-memory session table; the oldest session is evicted past the cap
# so a flood of logins can never grow it without limit.
MAX_SESSIONS = 1000

# Failed-login throttle. Attempts are counted per source and, once a source has
# used MAX_FAILURES_PER_CLIENT attempts inside FAILURE_WINDOW_SECONDS, that source
# is fully blocked until the window rolls over: further attempts are refused with
# 429 before the password is compared, so even a correct password from a blocked
# source is refused. Counting happens under the lock before the compare, so a
# concurrent burst can never obtain more than the allowed number of guesses. The
# block is per-source only; there is deliberately no global ceiling, which would
# be an attacker-triggerable lockout of every operator at once. A correct login
# clears the source's streak. A blocked operator recovers by waiting out the
# window, using the loopback SSH forward (a separate, exempt bucket), or a
# different IP; blocking the real operator requires flooding their exact egress
# IP, and a strong generated password already defeats brute force regardless.
FAILURE_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_CLIENT = 10
# Bound the per-source failure table so a spoofed-key flood cannot grow it
# without limit; the oldest entry is evicted past the cap.
MAX_TRACKED_CLIENTS = 10_000


def _now() -> float:
    return time.monotonic()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Session:
    __slots__ = ("created_at", "last_used_at")

    def __init__(self, now: float) -> None:
        self.created_at = now
        self.last_used_at = now


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def create_session() -> str:
    """Mint a session and return its raw token (handed to the browser once)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _sessions_lock:
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda key: _sessions[key].created_at)
            del _sessions[oldest]
        _sessions[_hash(token)] = _Session(now)
    return token


def validate_session(token: str) -> str | None:
    """Return the token's stored hash if it names a live session, refreshing the
    idle clock; return ``None`` (dropping any expired session) otherwise. Only
    the hash is stored, so the raw token is never held after it is minted."""
    token_hash = _hash(token)
    now = _now()
    with _sessions_lock:
        session = _sessions.get(token_hash)
        if session is None:
            return None
        if (
            now - session.created_at > SESSION_ABSOLUTE_TIMEOUT_SECONDS
            or now - session.last_used_at > SESSION_IDLE_TIMEOUT_SECONDS
        ):
            del _sessions[token_hash]
            return None
        session.last_used_at = now
        return token_hash


def destroy_session(token_hash: str) -> None:
    """Revoke one session by its stored hash (logout)."""
    with _sessions_lock:
        _sessions.pop(token_hash, None)


class _ClientFailures:
    __slots__ = ("count", "window_start")

    def __init__(self, now: float) -> None:
        self.count = 0
        self.window_start = now


_client_failures: dict[str, _ClientFailures] = {}
_throttle_lock = threading.Lock()


def register_attempt(client_key: str) -> bool:
    """Atomically count one login attempt from this source and return whether it
    may proceed. Returns ``False`` once the source has reached
    ``MAX_FAILURES_PER_CLIENT`` attempts inside the window: the source is then
    blocked (even a correct password must be refused) until the window rolls over.
    Because the count is consulted and incremented under the lock before the
    password is compared, a concurrent burst can never obtain more than the
    allowed number of guesses. A correct login clears the streak via
    :func:`record_success`; the block is per-source only, never global."""
    now = _now()
    with _throttle_lock:
        entry = _client_failures.get(client_key)
        if (
            entry is not None
            and now - entry.window_start <= FAILURE_WINDOW_SECONDS
            and entry.count >= MAX_FAILURES_PER_CLIENT
        ):
            return False
        if entry is None or now - entry.window_start > FAILURE_WINDOW_SECONDS:
            if len(_client_failures) >= MAX_TRACKED_CLIENTS and client_key not in _client_failures:
                oldest = min(_client_failures, key=lambda key: _client_failures[key].window_start)
                del _client_failures[oldest]
            entry = _ClientFailures(now)
        entry.count += 1
        _client_failures[client_key] = entry
        return True


def record_success(client_key: str) -> None:
    """Clear a source's attempt streak after a correct login."""
    with _throttle_lock:
        _client_failures.pop(client_key, None)


def throttle_key_from_ip(raw: str) -> str:
    """Canonical throttle bucket for a client IP: IPv4 per address, IPv6 per /64
    (so privacy-address rotation within a prefix cannot spread across buckets).
    Raises ``ValueError`` for an address that does not parse."""
    address = ipaddress.ip_address(raw.strip())
    if isinstance(address, ipaddress.IPv6Address):
        network = ipaddress.ip_network((address, 64), strict=False)
        return f"cf6:{network.network_address}"
    return f"cf4:{address.compressed}"


def _cookie_name(secure: bool) -> str:
    return HOST_SESSION_COOKIE_NAME if secure else SESSION_COOKIE_NAME


def session_cookie(token: str, *, secure: bool) -> str:
    """The ``Set-Cookie`` value that installs a session. ``HttpOnly`` keeps it
    out of JavaScript, ``SameSite=Strict`` withholds it from cross-site
    requests, ``Path=/`` and no ``Domain`` scope it to this host, and over HTTPS
    it is ``Secure`` with the ``__Host-`` name so a sibling cannot toss it."""
    attributes = [
        f"{_cookie_name(secure)}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={SESSION_ABSOLUTE_TIMEOUT_SECONDS}",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def clear_session_cookie(*, secure: bool) -> str:
    """The ``Set-Cookie`` value that removes the session cookie on logout, with
    attributes matching :func:`session_cookie` so the browser drops it."""
    attributes = [
        f"{_cookie_name(secure)}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=0",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def parse_session_token(cookie_header: str) -> str | None:
    """The session token from a request ``Cookie`` header, or ``None``. The
    ``__Host-`` cookie is preferred: a sibling on the shared parent domain cannot
    set or shadow it, so it is immune to cookie-tossing; the plain name is only
    accepted for the loopback SSH forward. The token is URL-safe base64, so it
    needs no decoding to compare."""
    found: dict[str, str] = {}
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name in (HOST_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME) and value and name not in found:
            found[name] = value
    return found.get(HOST_SESSION_COOKIE_NAME) or found.get(SESSION_COOKIE_NAME)
