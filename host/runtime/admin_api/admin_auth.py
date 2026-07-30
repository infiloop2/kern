"""Admin login sessions and brute-force throttling for the localhost admin API.

The admin API's own password login is the authentication boundary: it is
reached over the optional Cloudflare Tunnel (or an SSH port forward) with no
Cloudflare Access gate in front, so this module hardens that login to stand
alone. A completed factor sequence mints an opaque, server-held session token
delivered as an ``HttpOnly``, ``SameSite=Strict`` cookie. On enrolled public
HTTPS, a correct password can issue only short-lived pre-authentication state;
the final session requires successful WebAuthn verification. The raw admin
password never lands in a browser-readable store and a stolen token is bounded
by idle and absolute lifetimes. Login attempts are throttled per source: once a
source uses its per-window budget it is fully blocked with a ``429`` (even a
correct password is refused) until the window clears. The block is per-source
only, never global, so it cannot lock every operator out at once, and the strong
generated password is what ultimately defeats brute force.

Session, pre-authentication, and throttle state are process-local, in-memory,
and thread-safe (the admin API is a ``ThreadingHTTPServer``, so every request
runs on its own thread). Durable password and passkey records are read through
their narrow storage modules. A host restart clears ephemeral authentication
state; the operator simply logs in again. Timing uses a monotonic clock so a
wall-clock change never extends a session or a lockout.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from typing import Any, Callable, cast, Sequence

from host.runtime.admin_api import admin_passkeys
from host.runtime.core.state import load_admin_password_hash


# The session cookie the browser holds after login. Opaque and server-held: it
# never carries the password or any decodable claim. Over HTTPS the cookie uses
# the ``__Host-`` prefix, which the browser only accepts when it is Secure,
# Path=/, and has no Domain; a sibling agent origin on the shared ``kern.me``
# parent domain therefore cannot set or shadow it (cookie-tossing defense). The
# plain name is used only over the plain-HTTP SSH-forward loopback, where
# ``__Host-`` cannot apply (it requires Secure) and localhost has no siblings.
HOST_SESSION_COOKIE_NAME = "__Host-tc_admin_session"
SESSION_COOKIE_NAME = "tc_admin_session"
# A correct public-path password temporarily receives this cookie while the
# browser completes WebAuthn. It is not an admin session and is never accepted
# by _authenticate.
PASSKEY_LOGIN_COOKIE_NAME = "__Host-tc_admin_passkey_login"
# Cookie-authenticated requests must also carry this header. Same-origin admin
# UI JavaScript sets it on every request; a cross-site page cannot (adding it to
# a cross-origin request forces a CORS preflight the admin API never answers),
# so the ``SameSite=Strict`` cookie and this required header together close CSRF.
CSRF_HEADER_NAME = "X-Kern-Csrf"
# A valid same-origin request carrying this marker may refresh the idle clock.
# It is deliberately not another credential: the session cookie plus CSRF
# header still perform all authentication. The admin UI adds it only following
# recent human interaction, so scheduled polling cannot extend an abandoned
# browser session.
SESSION_ACTIVITY_HEADER_NAME = "X-Kern-Session-Activity"
MAX_PASSWORD_BYTES = 256

# Cached at startup so login performs no per-request database work.
# Reconfigure restarts the service, which reloads the verifier.
_ADMIN_PASSWORD_HASH: str | None = None
_ADMIN_PASSWORD_HASH_LOCK = threading.Lock()


class AccessPath(Enum):
    """The only two browser-facing paths into the loopback admin service."""

    SSH_FORWARD = "ssh_forward"
    PUBLIC_HTTPS = "public_https"


@dataclass(frozen=True)
class RequestAuthContext:
    """Immutable transport/auth policy selected once at request entry."""

    access_path: AccessPath
    public_hostname: str | None = None

    def __post_init__(self) -> None:
        if (self.access_path is AccessPath.PUBLIC_HTTPS) != (
            self.public_hostname is not None
        ):
            raise ValueError("public HTTPS context and hostname must be set together")

    @property
    def is_public_https(self) -> bool:
        return self.access_path is AccessPath.PUBLIC_HTTPS

    @property
    def passkey_context(self) -> tuple[str, str] | None:
        if not self.is_public_https:
            return None
        hostname = cast(str, self.public_hostname)
        return hostname, f"https://{hostname}"


LOCAL_SSH_FORWARD = RequestAuthContext(AccessPath.SSH_FORWARD)

# Authentication routes that exist only on the WebAuthn-capable HTTPS origin.
# service.py consults this before dispatch or session authentication, so adding
# another HTTPS-only auth ceremony requires an explicit policy entry here.
HTTPS_ONLY_AUTH_ROUTES = frozenset({
    ("GET", "/v1/login/status"),
    ("POST", "/v1/login/passkey"),
    ("GET", "/v1/admin-passkeys"),
    ("POST", "/v1/admin-passkeys/register/options"),
    ("POST", "/v1/admin-passkeys/register"),
})


class RequestBoundaryError(ValueError):
    """A request claimed the tunnel path but did not satisfy its boundary."""


class PublicHttpsRequired(RequestBoundaryError):
    def __init__(self, hostname: str) -> None:
        super().__init__("HTTPS is required")
        self.hostname = hostname


class SessionAuthError(ValueError):
    """A request did not carry a live session for its classified access path."""


class MissingSessionRequestHeader(SessionAuthError):
    """A cookie-bearing request omitted the CSRF request header."""


class LoginRateLimited(ValueError):
    """This source exhausted its password-attempt budget."""


class InvalidPassword(ValueError):
    """The password request was malformed or did not match."""


class MissingPasskeyRequestHeader(ValueError):
    """A passkey assertion omitted its same-origin request marker."""


class PasskeyStartError(ValueError):
    """A correct password could not begin its required passkey ceremony."""


class PasskeyVerificationError(ValueError):
    """A passkey assertion failed without minting an operator session."""

    def __init__(self, message: str, *, set_cookies: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.set_cookies = set_cookies


@dataclass(frozen=True)
class LoginResult:
    """A completed password/passkey step and its auth-owned cookies."""

    passkey_options: dict[str, Any] | None
    set_cookies: tuple[str, ...]


def classify_request(
    *,
    forwarded_proto_values: Sequence[str],
    host_values: Sequence[str],
    public_hostname_loader: Callable[[], str | None],
) -> RequestAuthContext:
    """Classify the request before routing.

    No forwarded-proto header is the SSH-forward path. Any presence claims the
    Cloudflare path and therefore requires one unambiguous header, the exact
    configured public Host, and HTTPS. The hostname loader is called only for
    the public path, keeping SSH recovery independent of database state.
    """
    if not forwarded_proto_values:
        return LOCAL_SSH_FORWARD
    if len(forwarded_proto_values) != 1:
        raise RequestBoundaryError("invalid public transport marker")
    hostname = public_hostname_loader()
    if (
        hostname is None
        or len(host_values) != 1
        or host_values[0].lower() not in {hostname, f"{hostname}:443"}
    ):
        raise RequestBoundaryError(
            "request host does not match the configured public admin hostname"
        )
    forwarded_proto = forwarded_proto_values[0].lower()
    if forwarded_proto == "http":
        raise PublicHttpsRequired(hostname)
    if forwarded_proto != "https":
        raise RequestBoundaryError("invalid public transport marker")
    return RequestAuthContext(AccessPath.PUBLIC_HTTPS, hostname)


def route_is_available(
    context: RequestAuthContext, method: str, path: str
) -> bool:
    """Whether an access path exposes this transport-specific auth route."""
    return (
        (method, path) not in HTTPS_ONLY_AUTH_ROUTES
        or context.is_public_https
    )


def _admin_password_hash() -> str:
    global _ADMIN_PASSWORD_HASH
    cached = _ADMIN_PASSWORD_HASH
    if cached is not None:
        return cached
    with _ADMIN_PASSWORD_HASH_LOCK:
        if _ADMIN_PASSWORD_HASH is None:
            _ADMIN_PASSWORD_HASH = load_admin_password_hash()
        return _ADMIN_PASSWORD_HASH


def preload_password_verifier() -> None:
    """Load the durable password verifier during service startup."""
    _admin_password_hash()


# A session is dropped after this much operator inactivity, or this much total
# age, whichever comes first. Ordinary background API polling only validates a
# session; it never refreshes the idle clock. The UI marks requests made within
# a short window of a real pointer/key/touch interaction, and only those calls
# refresh the clock. The absolute cap bounds a stolen token's usefulness even
# when an attacker actively keeps it busy.
SESSION_IDLE_TIMEOUT_SECONDS = 12 * 60 * 60
SESSION_ABSOLUTE_TIMEOUT_SECONDS = 3 * 24 * 60 * 60
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


def _create_session() -> str:
    """Mint a session and return its raw token (handed to the browser once)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _sessions_lock:
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda key: _sessions[key].created_at)
            del _sessions[oldest]
        _sessions[_hash(token)] = _Session(now)
    return token


def validate_session(token: str, *, refresh_idle: bool = False) -> str | None:
    """Return the token's stored hash if it names a live session, or ``None``
    (dropping any expired session) otherwise. ``refresh_idle`` is reserved for
    requests the UI has tied to recent operator interaction; scheduled polling
    validates without keeping an abandoned tab alive. Only the hash is stored,
    so the raw token is never held after it is minted."""
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
        if refresh_idle:
            session.last_used_at = now
        return token_hash


def _destroy_session(token_hash: str) -> None:
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


def _session_cookie(token: str, *, context: RequestAuthContext) -> str:
    """The ``Set-Cookie`` value that installs a session. ``HttpOnly`` keeps it
    out of JavaScript, ``SameSite=Strict`` withholds it from cross-site
    requests, ``Path=/`` and no ``Domain`` scope it to this host, and over HTTPS
    it is ``Secure`` with the ``__Host-`` name so a sibling cannot toss it."""
    attributes = [
        f"{_cookie_name(context.is_public_https)}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={SESSION_ABSOLUTE_TIMEOUT_SECONDS}",
    ]
    if context.is_public_https:
        attributes.append("Secure")
    return "; ".join(attributes)


def _clear_session_cookie(*, context: RequestAuthContext) -> str:
    """The ``Set-Cookie`` value that removes the session cookie on logout, with
    attributes matching :func:`_session_cookie` so the browser drops it."""
    attributes = [
        f"{_cookie_name(context.is_public_https)}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=0",
    ]
    if context.is_public_https:
        attributes.append("Secure")
    return "; ".join(attributes)


def _passkey_login_cookie(token: str) -> str:
    return "; ".join([
        f"{PASSKEY_LOGIN_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=300",
        "Secure",
    ])


def _clear_passkey_login_cookie() -> str:
    return "; ".join([
        f"{PASSKEY_LOGIN_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=0",
        "Secure",
    ])


def _parse_passkey_login_token(cookie_header: str) -> str | None:
    found: list[str] = []
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == PASSKEY_LOGIN_COOKIE_NAME and value:
            found.append(value)
    return found[0] if len(found) == 1 else None


def _completed_session(
    context: RequestAuthContext,
    *,
    additional_cookies: tuple[str, ...] = (),
) -> LoginResult:
    """The only final operator-session minting path."""
    cookie = _session_cookie(_create_session(), context=context)
    return LoginResult(
        passkey_options=None,
        set_cookies=(cookie, *additional_cookies),
    )


def begin_password_login(
    context: RequestAuthContext,
    *,
    client_key: str,
    password_loader: Callable[[], str | None],
) -> LoginResult:
    """Verify factor one and either mint a session or begin factor two."""
    if not register_attempt(client_key):
        raise LoginRateLimited("too many failed admin logins; try again later")
    password = password_loader()
    try:
        encoded_password = password.encode("utf-8") if password is not None else b""
    except UnicodeEncodeError:
        encoded_password = b""
    expected = _admin_password_hash()
    if (
        not encoded_password
        or len(encoded_password) > MAX_PASSWORD_BYTES
        or not expected
        or not hmac.compare_digest(
            hashlib.sha256(encoded_password).hexdigest(),
            expected,
        )
    ):
        raise InvalidPassword("missing or invalid admin password")

    # The throttle protects the password factor. Cancelling a browser prompt
    # must not consume the password-guess budget; the separate pre-auth cookie
    # and ceremony remain short-lived and single-use.
    record_success(client_key)
    if context.is_public_https and admin_passkeys.configured():
        passkey_context = cast(tuple[str, str], context.passkey_context)
        try:
            token, options = admin_passkeys.begin_login(
                rp_id=passkey_context[0],
                origin=passkey_context[1],
                client_key=client_key,
            )
        except admin_passkeys.PasskeyError as exc:
            raise PasskeyStartError(str(exc)) from exc
        return LoginResult(
            passkey_options=options,
            set_cookies=(_passkey_login_cookie(token),),
        )
    return _completed_session(context)


def complete_passkey_login(
    context: RequestAuthContext,
    *,
    cookie_header: str,
    csrf_header: str,
    client_key_loader: Callable[[], str],
    response_loader: Callable[[], Any],
) -> LoginResult:
    """Verify factor two and mint the final session only after success."""
    if not context.is_public_https:
        raise RuntimeError("passkey completion reached outside public HTTPS")
    if not csrf_header:
        raise MissingPasskeyRequestHeader("missing passkey login request header")
    token = _parse_passkey_login_token(cookie_header)
    if token is None:
        raise PasskeyVerificationError(
            "passkey login expired; enter the admin password again"
        )
    client_key = client_key_loader()
    response = response_loader()
    try:
        admin_passkeys.finish_login(
            token,
            response,
            client_key=client_key,
        )
    except admin_passkeys.PasskeyError as exc:
        raise PasskeyVerificationError(
            str(exc),
            set_cookies=(_clear_passkey_login_cookie(),),
        ) from exc
    return _completed_session(
        context,
        additional_cookies=(_clear_passkey_login_cookie(),),
    )


def passkey_login_configured() -> bool:
    """The non-secret factor-two enrollment bit on the HTTPS login origin."""
    return admin_passkeys.configured()


def logout(
    context: RequestAuthContext,
    *,
    session_token_hash: str,
) -> str:
    """Revoke one operator session and return its matching clearing cookie."""
    _destroy_session(session_token_hash)
    return _clear_session_cookie(context=context)


def parse_session_token(
    cookie_header: str, *, context: RequestAuthContext
) -> str | None:
    """The session token from a request ``Cookie`` header, or ``None``. The
    public HTTPS path accepts only its ``__Host-`` cookie: a sibling on the
    shared parent domain cannot set or shadow it. The plain name is accepted
    only over the loopback SSH forward. Reject duplicate values instead of
    choosing one, so cookie ordering can never influence authentication. The
    token is URL-safe base64, so it needs no decoding to compare."""
    # Bind the accepted cookie name to the observed transport, using the same
    # choice as _session_cookie(): HTTPS accepts only the __Host- cookie, while
    # the plain loopback SSH-forward transport accepts only the local cookie.
    expected_name = _cookie_name(context.is_public_https)
    found: list[str] = []
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == expected_name and value:
            found.append(value)
    return found[0] if len(found) == 1 else None


def authenticate_session_request(
    context: RequestAuthContext,
    *,
    cookie_header: str,
    csrf_header: str,
    activity_header: str,
) -> str:
    """Authenticate one routed API request and return its stored token hash.

    Transport-specific cookie selection, CSRF enforcement, session expiry, and
    idle refresh live together here so route handlers cannot accidentally
    authenticate the public and SSH-forward paths differently.
    """
    token = parse_session_token(cookie_header, context=context)
    if token is None:
        raise SessionAuthError("missing or invalid admin session")
    if not csrf_header:
        raise MissingSessionRequestHeader("missing admin session request header")
    token_hash = validate_session(
        token,
        refresh_idle=activity_header == "1",
    )
    if token_hash is None:
        raise SessionAuthError("missing or invalid admin session")
    return token_hash


def login_client_key(
    context: RequestAuthContext,
    *,
    local_address: str,
    cf_connecting_ip_values: Sequence[str],
    cf_connecting_ipv6_values: Sequence[str],
) -> str:
    """Return the brute-force throttle bucket for the classified path."""
    if not context.is_public_https:
        return f"local:{local_address}"
    values = (
        cf_connecting_ipv6_values
        if cf_connecting_ipv6_values
        else cf_connecting_ip_values
    )
    if len(values) != 1:
        raise RequestBoundaryError("invalid tunnel client identity")
    try:
        return throttle_key_from_ip(values[0])
    except ValueError as exc:
        raise RequestBoundaryError("invalid tunnel client identity") from exc
