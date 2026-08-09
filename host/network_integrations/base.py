"""Shared types for network integrations.

Pure module: imported by ``host.config`` and every integration ``manifest.py``,
so it must not import ``host.runtime``.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from host.param_guard import find_denial

AccountAttestor = Callable[[str], str | None]


@dataclass(frozen=True)
class DenialReason:
    """Catalog entry for one denial code — the stable snake_case string a
    guard returns, sent in the 403 body, and stored in network events. The
    guidance says what it means and what to do about it, written for the
    agent (and naming the operator action that would change the outcome)."""

    code: str
    guidance: str


class IntegrationConfig(Protocol):
    """The parsed, validated config of one integration: at minimum an
    ``enabled`` flag and the exact operator-facing JSON it round-trips to.
    A disabled integration carries no other state (validation enforces it),
    so serializers emit only enabled entries."""

    @property
    def enabled(self) -> bool: ...

    def to_json(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IntegrationManifest:
    """The static contract of one network integration.

    ``owned_apexes`` are the fixed domain apexes the integration owns. The
    proxy dispatches those hosts to this integration even while it is disabled,
    so the custom-domain integration can never bypass a managed guard. Apex
    claims must be disjoint across the registry.

    ``denial_reasons`` catalogs every denial code the integration's guard can
    emit, with agent-facing guidance; the agent introspection tools join
    network events against it.
    """

    integration_id: str
    display_name: str
    description: str
    owned_apexes: tuple[str, ...]
    denial_reasons: tuple[DenialReason, ...] = ()


@dataclass(frozen=True)
class ManagedIntegration:
    """Config for an integration with no options beyond on/off."""

    enabled: bool

    def to_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


class IntegrationConfigError(ValueError):
    """Invalid integration config. ``host.config`` re-raises it as
    ``ConfigError`` so operator-facing validation errors stay uniform."""


def reject_extra(raw: dict[str, Any], allowed: set[str], context: str) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        raise IntegrationConfigError(f"{context} has unsupported fields: {', '.join(extra)}")


def parse_simple_integration(raw: dict[str, Any], context: str) -> ManagedIntegration:
    if not raw:
        return ManagedIntegration(False)
    reject_extra(raw, {"enabled"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise IntegrationConfigError(f"{context}.enabled must be true or false")
    return ManagedIntegration(enabled)


def simple_integration_parser(context: str) -> Callable[[dict[str, Any]], ManagedIntegration]:
    def parse(raw: dict[str, Any]) -> ManagedIntegration:
        return parse_simple_integration(raw, context)

    return parse


# Denials produced by the proxy core rather than an integration guard: the
# domain/method/path decision, transport rules, and inspection limits. Kept
# next to the integration catalogs so the agent introspection tools serve one
# uniform reason lookup.
_CORE_PROXY_DENIAL_REASONS: tuple[DenialReason, ...] = (
    DenialReason(
        "network_policy_denied",
        "No network policy rule allows this host, method, and path. The operator can add a "
        "custom-domain rule or enable a managed network integration covering it in the admin UI's "
        "Network tab.",
    ),
    DenialReason(
        "host_not_allowed",
        "The host is not in the allowed network policy, so the connection was refused before "
        "DNS resolution. The operator can add a custom-domain rule or enable a managed network "
        "integration covering it.",
    ),
    DenialReason(
        "network_policy_unavailable",
        "The stored network policy could not be loaded or parsed, so every request is denied "
        "until it is restored. The operator should check the host's health status.",
    ),
    DenialReason(
        "connect_port_denied",
        "Only HTTPS on port 443 is allowed. Reissue the request against the standard HTTPS "
        "port.",
    ),
    DenialReason(
        "plain_http_denied",
        "Plain HTTP is not supported; every allowed destination speaks HTTPS. Reissue the "
        "request over https://.",
    ),
    DenialReason(
        "request_target_invalid",
        "The request target was not origin-form. Use a standard HTTP client; hand-built "
        "request lines with absolute-form targets are refused.",
    ),
    DenialReason(
        "host_header_invalid",
        "The Host header was missing, duplicated, or did not match the connected host. Use a "
        "standard HTTP client that sends one matching Host header.",
    ),
    DenialReason(
        "duplicate_header_denied",
        "A single-valued header (Content-Type, Content-Encoding, Content-Length, "
        "Transfer-Encoding, or Authorization) was sent more than once, so the request had no one "
        "meaning to inspect. Use a standard HTTP client that sends each of these once.",
    ),
    DenialReason(
        "request_body_malformed",
        "The request body framing (Content-Length or chunked encoding) was malformed, so the "
        "body could not be inspected. Resend with valid framing.",
    ),
    DenialReason(
        "request_body_too_large",
        "The request body exceeds the proxy's inspection limit (128 MiB). Split the upload or "
        "send less data per request.",
    ),
    DenialReason(
        "websocket_upgrade_declined",
        "The upstream did not accept the WebSocket handshake with 101 Switching Protocols, so "
        "the proxy closed the connection instead of treating an ordinary HTTP response as an "
        "unchecked tunnel. Check the WebSocket URL and authentication, then reconnect.",
    ),
    DenialReason(
        "websocket_not_allowed",
        "WebSocket upgrades are enabled only for guarded OpenAI endpoints or a custom domain "
        "whose operator rule explicitly sets allow_websocket. Use ordinary HTTPS, or ask the "
        "operator to enable the custom-domain option if a WebSocket is intended.",
    ),
    DenialReason(
        "websocket_uninspectable",
        "A WebSocket message on this guarded domain could not be safely inspected (unsupported "
        "framing, extension, or size), so the connection was closed. Reconnect without "
        "extensions and keep messages under the inspection limit.",
    ),
)




# --- Outbound request parameter guard on the proxy path ------------------
#
# The same deterministic guard that tools apply through ``HostAPI.outbound``
# (host.param_guard), run here over the one agent-authored dimension of a
# managed-integration request that an attacker can read back: the URL. npm and
# PyPI publish per-package download statistics, so a requested package name is
# a real, if slow, channel. Route allowlists stay authoritative; this adds
# content strictness on the values they cannot constrain by shape.
#
# Headers are not scanned. On these first-party destinations nothing reflects a
# request header back, so there is no reader for that channel; what the proxy
# does instead is remove what a header can *do* — credentials on the package
# registries, the agent's Authorization on GitHub (replaced with the host
# token), and the free text in User-Agent.
# See docs/architecture/tools/outbound-request-filtering.md.

# Cataloged with the proxy-core reasons (codes are globally unique across
# the catalog); several integration guards emit these, so they live here
# rather than in any one integration's manifest.
_PARAM_GUARD_DENIAL_REASONS: tuple[DenialReason, ...] = (
    DenialReason(
        "request_param_too_large",
        "A request value exceeded the parameter guard's fixed length limit. "
        "Shorten the value and retry.",
    ),
    DenialReason(
        "request_param_encoded_blob_denied",
        "A request value looked like an encoded payload (control characters, "
        "an overlong unbroken token, or a random-looking string). Rewrite it as "
        "plain text and retry.",
    ),
    DenialReason(
        "request_param_secret_denied",
        "A request value appeared to contain a secret or credential (API key, "
        "token, private key, password, or similar). Remove it and retry; secrets "
        "must never ride in request parameters.",
    ),
    DenialReason(
        "request_param_pii_denied",
        "A request value appeared to contain a personal or financial "
        "identifier (email, phone, card, account, or code). Remove it and retry.",
    ),
)



# The one header the proxy replaces outright rather than forwarding. Two
# reasons. It is the largest free-text field a real client sends, and — unlike
# the rest of a request to these hosts — it is not unread: PyPI publishes a
# public download dataset whose installer name and version are derived from
# User-Agent, so an agent-chosen product token there really is attacker-
# readable. A fixed host value removes the field as a channel entirely; these
# destinations require a User-Agent to be present but do not care what it says.
# Headers that carry an identity. On the package registries the agent has no
# legitimate credential, so these are removed rather than forwarded: the harm
# they can do is authenticate as someone else or hand a token to a provider
# that should not have it. GitHub handles its own Authorization separately, by
# replacing it with the host-held token.
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

PROXY_USER_AGENT = "kern-proxy/1"


def fixed_user_agent(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Replace User-Agent with the host's own value, adding it if absent."""
    rewritten = [(key, value) for key, value in headers if key.lower() != "user-agent"]
    rewritten.append(("User-Agent", PROXY_USER_AGENT))
    return rewritten


def strip_credentials(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop identity-bearing headers. Used where the agent has no credential
    the destination should receive."""
    return [(key, value) for key, value in headers if key.lower() not in CREDENTIAL_HEADERS]


def _strict_unquote_header(value: str) -> str:
    """Percent-decode until stable; each real escape shortens the value."""
    while True:
        decoded = urllib.parse.unquote(value, errors="strict")
        if decoded == value:
            return value
        value = decoded


def request_param_denial(path: str, query: str, *, token_rules: bool = True) -> str | None:
    """Run the parameter guard over a managed-integration request's URL and
    return the first denial code.

    The whole reconstructed URL (`https://host<path>?<query>`) is decoded and
    scanned as one value rather than parsing path segments and query pairs
    individually. This keeps the proxy path simple and still enforces the
    credential-named-query-key rule, because scanning a full URL routes
    through the same `CRED_URL` guard (G10) that parses the query and denies
    a credential key carrying a long value - so `?access_token=<16+ chars>`
    is caught without the proxy reparsing anything. The unbroken URL is a
    plain `https` URL, so the token-length rule does not fire on it; long or
    encoded payloads inside a path segment are caught by the unnatural-token
    rule instead.

    Percent-decoding is strict: bytes that are not valid UTF-8 would be
    smoothed into replacement characters by lenient decoding (and pass the
    printable rule) while the raw bytes still went upstream - a binary
    exfiltration channel - so invalid encodings deny outright.
    """
    raw = "https://host" + path
    if query:
        raw += "?" + query
    try:
        decoded = urllib.parse.unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return "request_param_encoded_blob_denied"
    denial = find_denial(decoded, token_rules=token_rules)
    if denial is not None:
        return denial.reason
    # An integration exemption excuses a header from the *semantic* guards,
    # because the value legitimately looks like the thing they detect — an
    # operator-approved credential really is a credential. It does not excuse
    # the structural floors. Those are applied to all instances of a header
    # joined, not to each one: a field can repeat (the proxy forwards every
    # Cookie it is given), so a per-instance limit would bound nothing.
    return None


PROXY_DENIAL_REASONS: tuple[DenialReason, ...] = (
    _CORE_PROXY_DENIAL_REASONS + _PARAM_GUARD_DENIAL_REASONS
)
