"""Claude/Anthropic request guard: account identity and server-tool controls.

Runs in the proxy for hosts under the Claude apexes.

Claude Code OAuth requests to api.anthropic.com use opaque, independently
rotating bearer tokens and do not carry an OpenAI-style account header. The
proxy therefore attests each distinct token through Anthropic's profile
endpoint and compares its provider-signed account uuid with the operator-
approved account id. Successful token hashes are cached in memory. A tiny
unauthenticated readiness path is allowed before the account is linked because
Claude Code probes it during startup.

Separately, Messages API requests may declare Anthropic server-side tools that
run on Anthropic's infrastructure and reach external URLs with request data —
web search (``web_search_*``), server-side web fetch (``web_fetch_*``), code
execution (``code_execution_*``), and remote MCP servers. The client's
WebFetch/Bash egress is already gated by the domain allow-list, but these
execute off-box, so the only enforcement point is the request that declares
them; the Claude integration denies them structurally.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import threading
from typing import Any

from host.network_integrations.base import AccountAttestor
from host.network_integrations.claude.manifest import ClaudeIntegration
from host.runtime.core.network_policy import decode_body, normalized_path, route_allowed
from host.runtime.core.state import read_proxy_claude_account_id

ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS = {
    "/api/oauth/profile",
    "/api/oauth/claude_cli/roles",
    "/api/organization/claude_code_first_token_date",
    "/api/claude_code/policy_limits",
    "/api/claude_code/settings",
}
ROUTES = {
    "api.anthropic.com": (("GET", "POST"), ()),
    "platform.claude.com": (("GET", "POST"), (r"^/v1/oauth(?:/.*)?$",)),
}
# A successful profile attestation binds an opaque token hash to the already
# approved account id. Keep only hashes (never bearer tokens), and bound the
# process-local cache so agent-authored candidate tokens cannot grow it.
_TOKEN_ATTESTATION_CACHE_LIMIT = 32
_TOKEN_ATTESTATION_CACHE: OrderedDict[tuple[str, str], None] = OrderedDict()
_TOKEN_ATTESTATION_LOCK = threading.Lock()


@dataclass
class _TokenAttestationFlight:
    done: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False


_TOKEN_ATTESTATIONS_IN_FLIGHT: dict[tuple[str, str], _TokenAttestationFlight] = {}


def host_allowed(config: ClaudeIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def request_denied(
    config: ClaudeIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
    account_attestor: AccountAttestor | None = None,
) -> str | None:
    """Apply the Claude-owned route, account, token, and server-tool controls."""
    route = ROUTES.get(host.lower())
    if route is None or not route_allowed(method, path, query, *route):
        return "network_policy_denied"
    if host.lower() != "api.anthropic.com":
        return None
    denial = _server_tool_denial(headers, body, config.web_search)
    if denial is not None:
        return denial
    if method.upper() == "GET" and path == "/api/hello":
        return None
    authorization = [value for key, value in headers if key.lower() == "authorization"]
    presented = [_bearer_token(value) for value in authorization]
    bearer_tokens = [token for token in presented if token is not None]
    authorized_account_id = read_proxy_claude_account_id()
    if authorized_account_id is None:
        if _pre_pin_bootstrap_allowed(method, path, bearer_tokens):
            return None
        return "anthropic_account_unavailable"
    if not bearer_tokens:
        return "anthropic_token_required"
    if len(authorization) != 1 or len(bearer_tokens) != 1 or account_attestor is None:
        return "anthropic_token_mismatch"
    if (
        _token_belongs_to_account(authorized_account_id, bearer_tokens[0], account_attestor)
        and read_proxy_claude_account_id() == authorized_account_id
    ):
        return None
    return "anthropic_token_mismatch"


def _token_belongs_to_account(
    account_id: str,
    token: str,
    attest_account: AccountAttestor,
) -> bool:
    """Return true when Anthropic binds the bearer to the pinned account.

    Parallel Claude processes can temporarily use different access tokens as
    the shared OAuth credential rotates. The proxy authorizes every token by
    the same identity rule: Anthropic's profile endpoint must bind it to the
    durable, operator-approved account uuid. Successful ``(account id, token
    hash)`` decisions are cached; raw bearer tokens are neither stored nor
    logged.

    A per-hash in-flight record prevents concurrent requests carrying one new
    token from generating duplicate profile calls. The network round trip does
    not hold the cache lock, so already-cached tokens and different rotations
    continue independently.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cache_key = (account_id, token_hash)
    with _TOKEN_ATTESTATION_LOCK:
        if cache_key in _TOKEN_ATTESTATION_CACHE:
            _TOKEN_ATTESTATION_CACHE.move_to_end(cache_key)
            return True
        flight = _TOKEN_ATTESTATIONS_IN_FLIGHT.get(cache_key)
        leader = flight is None
        if flight is None:
            flight = _TokenAttestationFlight()
            _TOKEN_ATTESTATIONS_IN_FLIGHT[cache_key] = flight
    assert flight is not None
    if not leader:
        flight.done.wait()
        return flight.allowed
    allowed = False
    try:
        try:
            attested_account_id = attest_account(token)
        except Exception:
            attested_account_id = None
        allowed = isinstance(attested_account_id, str) and hmac.compare_digest(
            attested_account_id, account_id
        )
        return allowed
    finally:
        with _TOKEN_ATTESTATION_LOCK:
            if allowed:
                _TOKEN_ATTESTATION_CACHE[cache_key] = None
                _TOKEN_ATTESTATION_CACHE.move_to_end(cache_key)
                while len(_TOKEN_ATTESTATION_CACHE) > _TOKEN_ATTESTATION_CACHE_LIMIT:
                    _TOKEN_ATTESTATION_CACHE.popitem(last=False)
            flight.allowed = allowed
            _TOKEN_ATTESTATIONS_IN_FLIGHT.pop(cache_key, None)
            flight.done.set()


def clear_token_attestation_cache() -> None:
    """Clear process-local rotation attestations (used by tests)."""
    with _TOKEN_ATTESTATION_LOCK:
        _TOKEN_ATTESTATION_CACHE.clear()
        for flight in _TOKEN_ATTESTATIONS_IN_FLIGHT.values():
            flight.done.set()
        _TOKEN_ATTESTATIONS_IN_FLIGHT.clear()


def _server_tool_denial(
    headers: list[tuple[str, str]], body: bytes, allow_web_search: bool
) -> str | None:
    """Deny a Messages API request that declares an Anthropic server-side tool
    reaching an external URL or running code off-box. Mirrors the OpenAI body
    guard: decode, confirm the body parses as JSON, then enforce structurally.
    Web search is allowed only when the operator opted in (``allow_web_search``);
    server web fetch, code execution, and remote MCP are always denied. A body
    that cannot be decoded or parsed as declared fails closed."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "anthropic_body_undecodable"
    body = decoded
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "anthropic_body_not_json"
    return _tool_violation(payload, allow_web_search)


def _tool_violation(payload: Any, allow_web_search: bool) -> str | None:
    """The Messages API declares tools in a top-level ``tools`` array and remote
    MCP servers in a top-level ``mcp_servers`` array. Deny the server-side,
    off-box tool families by ``type`` prefix (dated variants such as
    ``web_search_20260209`` share the prefix); client-executed built-ins
    (``bash_*``, ``text_editor_*``, ``memory_*``) and user-defined tools (which
    carry a ``name`` but no ``type``) do not match and pass. ``web_search`` is
    permitted only when the operator enabled it; the others are always denied."""
    if not isinstance(payload, dict):
        return None
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if not isinstance(tool_type, str):
                continue
            if tool_type.startswith("web_search"):
                if not allow_web_search:
                    return "anthropic_web_search_denied"
            elif tool_type.startswith(("web_fetch", "code_execution")):
                return "anthropic_server_tool_denied"
    mcp_servers = payload.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        return "anthropic_remote_mcp_denied"
    return None


def _pre_pin_bootstrap_allowed(method: str, path: str, bearer_tokens: list[str]) -> bool:
    # Claude Code exchanges the browser OAuth code with platform.claude.com,
    # then calls a small set of api.anthropic.com profile/settings endpoints
    # before its credential file is ready for the host to hash and pin. Let
    # only those bearer-authenticated bootstrap reads through pre-pin; model
    # traffic such as /v1/messages still fails closed until the hash is stored.
    return (
        method.upper() == "GET"
        and normalized_path(path) in ANTHROPIC_PRE_PIN_BOOTSTRAP_GET_PATHS
        and bool(bearer_tokens)
    )


def _bearer_token(value: str) -> str | None:
    scheme, _, credential = value.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()
