"""xAI request guard: account pin and server-tool enforcement.

Runs in the proxy for hosts under the xAI apexes. The integration owns its
exact hosts and methods plus two request controls.

Every guarded request to the CLI chat proxy must authenticate with an xAI
OAuth token whose JWT claims the account pinned from the Grok login.
Requests that would make xAI reach an external source with request data
(X search, web search, remote MCP, hosted browsing or media generation) are
denied unconditionally; the integration exposes no options.

Only two hosts are opened under the owned apexes. Everything else beneath them
is denied by the route table, which is what keeps ``api.x.ai`` — the metered
developer API, billed against console credits rather than the operator's Grok
subscription — closed, along with ``code.grok.com`` session sync.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from host.network_integrations.base import AccountAttestor
from host.network_integrations.xai.manifest import XaiIntegration
from host.runtime.core.network_policy import decode_body, normalized_path, route_allowed
from host.runtime.core.state import (
    read_proxy_xai_account_id,
    read_proxy_xai_status_probe_account_id,
)

# Hosted tools that make xAI reach an external source with request data, run
# code off-box, or generate media on xAI infrastructure. All are denied
# outright. ``web_search`` is not listed here only because it keeps its own
# denial code; it is denied just as unconditionally.
#
# xAI names the same tool two ways, so both spellings are listed. The wire
# ``type`` a Responses API request carries is ``code_interpreter`` and
# ``file_search``; the Python SDK's helpers for those are ``code_execution``
# and ``collections_search``, and a client that serialises the helper name
# must not slip past a set that only knows the wire one. ``browser``,
# ``computer_use`` and the media families are not tools xAI documents today;
# they are held here so a rename lands on a name already denied.
_DENIED_HOSTED_TOOL_TYPES = frozenset(
    {
        "x_search",
        "browser",
        "computer_use",
        "code_execution",
        "code_interpreter",
        "collections_search",
        "file_search",
        "image_generation",
        "video_generation",
    }
)
# The one tool family the agent executes locally. Its egress is the agent's own,
# already gated by this proxy, so it is the only entry a ``tools`` array may
# carry at all.
_CLIENT_EXECUTED_TOOL_TYPE = "function"
# History items replaying an earlier hosted call. They appear in legitimate
# follow-up requests and declare no new capability, so every walk below skips
# them *and everything beneath them*. Descending would be worse than pointless:
# an `mcp_list_tools` item carries a `tools` array of descriptors of tools that
# already ran, and the declaration-site rule would read those untyped
# descriptors as undeclarable capabilities and deny an ordinary follow-up turn.
#
# The trade-off is deliberate. A client could in principle bury a real
# declaration under a forged replay envelope, but xAI takes declarations from
# the request's top-level parameters, not from inside a history item — so that
# would evade this guard without enabling anything. Breaking legitimate
# multi-turn conversations is the certain cost; the evasion is speculative.
_HISTORY_ITEM_TYPES = frozenset(
    {
        "web_search_call",
        "x_search_call",
        "code_interpreter_call",
        "file_search_call",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
    }
)



def _is_replay_item(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("type"), str)
        and node["type"] in _HISTORY_ITEM_TYPES
    )


# The chat proxy is not only an inference endpoint. The same host serves blob
# storage (`/v1/storage/batch_upload` takes multipart, so a body guard that only
# understands JSON cannot inspect it), remote session registration and search,
# workspace and skill sync, cloud sandboxes, feedback, and trace upload. Opening
# the host without a path allowlist would therefore hand xAI the agent's files
# and conversation history through routes that have nothing to do with
# inference.
#
# So this is an allowlist of what a login plus inference actually needs, and
# everything else on the host is denied. Each entry earns its place:
#
#   /v1/responses, /v1/chat/completions  the two inference shapes
#   /v1/models                           the model catalog
#   /v1/settings                         remote settings, read at startup
#   /v1/user                             the subscription/entitlement check
#   /v1/billing                          the usage snapshot for the top bar
#
# Deliberately absent: storage, sessions, workspaces, skills, sandbox, bundles,
# feedback, and traces. Widen this against an observed denial, which is visible
# in the network event log, rather than in anticipation.
CHAT_PROXY_INFERENCE_PATHS = (
    r"^/v1/(?:responses|chat/completions)(?:\?.*)?$",
)
CHAT_PROXY_READ_PATHS = (
    *CHAT_PROXY_INFERENCE_PATHS,
    r"^/v1/(?:models|settings|user|billing)(?:\?.*)?$",
)
ROUTES = {
    # The OAuth issuer: discovery, device-code, authorize, and token exchange.
    # Unpinned by construction — it is the endpoint that establishes which
    # account exists, and it carries no model traffic. Mirrors auth.openai.com.
    "auth.x.ai": (("GET", "POST"), ()),
    # Reads cover inference plus startup/status metadata. Writes are a separate
    # path rule below: settings/user/billing are account controls, not model
    # traffic, and must never become mutable merely because the bearer lives in
    # the agent-owned auth file.
    "cli-chat-proxy.grok.com": (("GET",), CHAT_PROXY_READ_PATHS),
}
GUARDED_HOSTS = frozenset({"cli-chat-proxy.grok.com"})
# A trusted-but-non-active login may use only these read-only maintenance
# routes. Inference never consults the status-probe pin, so a slow background
# entitlement check cannot temporarily reopen model traffic after the ordinary
# data-plane pin was cleared.
STATUS_PROBE_PATHS = frozenset({"/v1/models", "/v1/settings", "/v1/user", "/v1/billing"})


def host_allowed(config: XaiIntegration, host: str) -> bool:
    del config
    return host.lower() in ROUTES


def request_denied(
    config: XaiIntegration,
    method: str,
    host: str,
    path: str,
    query: str,
    headers: list[tuple[str, str]],
    body: bytes,
    account_attestor: AccountAttestor | None = None,
) -> str | None:
    """Apply the xAI-owned route, account, token, and server-tool controls.

    ``config`` carries only enablement, which the runtime has already decided
    before a request reaches here; the controls below are unconditional."""
    del account_attestor, config
    lowered_host = host.lower()
    route = ROUTES.get(lowered_host)
    allowed = route is not None and route_allowed(method, path, query, *route)
    if lowered_host in GUARDED_HOSTS and not allowed:
        allowed = route_allowed(
            method,
            path,
            query,
            ("POST",),
            CHAT_PROXY_INFERENCE_PATHS,
        )
    if not allowed:
        return "network_policy_denied"
    if lowered_host not in GUARDED_HOSTS:
        return None
    request_path = normalized_path(path)
    is_get = method.upper() == "GET"
    status_probe = is_get and request_path in STATUS_PROBE_PATHS
    account_id = read_proxy_xai_account_id()
    if not account_id and status_probe:
        account_id = read_proxy_xai_status_probe_account_id()
    if not account_id:
        return "xai_account_unavailable"
    denial = _token_account_denial(headers, account_id)
    if denial is not None:
        return denial
    return _server_tool_denial(headers, body)


def _token_account_denial(headers: list[tuple[str, str]], account_id: str) -> str | None:
    """Bind the bearer credential directly to the pinned account.

    Require exactly one Authorization header carrying a Bearer token whose JWT
    payload claims the pinned account.

    Which claim carries it follows xAI's own token handling: a personal login
    takes the account id from the token's ``sub``, and a team login takes it
    from ``principal_id`` (see the vendor's OIDC user-info extraction, where a
    Team principal's user id *is* the principal id). The signed
    ``principal_type`` decides which claim is authoritative; anything else
    fails closed — a missing or duplicated header, a
    non-Bearer scheme, an opaque non-JWT bearer, and a token claiming another
    account.

    The payload is parsed WITHOUT signature verification, which is sound here:
    a tampered claim breaks the signature xAI itself verifies, so only a genuine
    token of the pinned account both passes this check and authenticates
    upstream.
    """
    authorization = [value for key, value in headers if key.lower() == "authorization"]
    if len(authorization) != 1:
        return "xai_token_account_mismatch"
    token = _bearer_token(authorization[0])
    if token is None:
        return "xai_token_account_mismatch"
    if account_id != _jwt_account_id(token):
        return "xai_token_account_mismatch"
    return None


def _bearer_token(value: str) -> str | None:
    scheme, _, credential = value.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def _jwt_account_id(token: str) -> str | None:
    """The effective account an xAI OAuth token claims, else ``None``.

    Split on ".", base64url-decode the payload with padding restored, and
    tolerate any failure. Only the two identity claims are read; every other
    claim is ignored so an unrelated string in the token can never satisfy the
    pin.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    def claim(*names: str) -> str | None:
        for name in names:
            value = payload.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    principal_type = claim("principal_type", "principalType")
    subject_id = claim("sub")
    principal_id = claim("principal_id", "principalId")
    if principal_type:
        normalized_type = principal_type.casefold()
        if normalized_type == "user":
            return subject_id
        if normalized_type == "team":
            return principal_id
        return None
    # Older personal tokens may omit principal_type. Only accept them when the
    # two possible account claims agree, so the choice is unambiguous.
    if subject_id and subject_id == principal_id:
        return subject_id
    return None


def _server_tool_denial(headers: list[tuple[str, str]], body: bytes) -> str | None:
    """Deny a request that declares an xAI server-side tool reaching an external
    source or running code off-box. Mirrors the OpenAI and Claude body guards:
    decode, confirm the body parses as JSON, then enforce structurally. A body
    that cannot be decoded, parsed, or walked as declared fails closed."""
    if not body:
        return None
    header_map = {key.lower(): value for key, value in headers}
    decoded = decode_body(body, header_map.get("content-encoding", ""))
    if decoded is None:
        return "xai_body_undecodable"
    body = decoded
    content_type = header_map.get("content-type", "").split(";", 1)[0].strip().lower()
    looks_json = content_type == "application/json" or body.lstrip().startswith((b"{", b"["))
    if not looks_json:
        # xAI parses requests as JSON; a body it cannot parse cannot declare
        # tools, whatever its content-type label.
        return None
    try:
        payload = json.loads(body)
        return _tool_violation(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "xai_body_not_json"
    except RecursionError:
        # Nesting deep enough to exhaust the interpreter stack, either in the
        # parser or in the structural walkers below. Such a body cannot be
        # decided, so it is refused exactly like an unparseable one: the
        # connection already failed closed, but through a traceback rather
        # than a denial the operator can see in the audit trail.
        return "xai_body_not_json"


def _tool_violation(payload: Any) -> str | None:
    """Decide every guarded tool object in the request.

    Grok declares hosted tools as entries in the request's ``tools`` array,
    alongside the client-executed ``function`` tools it runs locally: the CLI
    splices raw ``{"type": "web_search"}`` and ``{"type": "x_search"}`` entries
    into that array.

    Collection is deliberately two-sided. A ``tools`` array is a declaration
    site, so *every* entry there that is not a client-executed ``function`` is
    guarded — that is what makes a hosted tool this host has never heard of
    (xAI adds them: web search, X search, and code execution today) fail closed
    instead of being forwarded because it is missing from a denylist. The
    named hosted families are additionally collected anywhere in the body, so
    a declaration nested under some other key is still inspected.

    No hosted tool survives this. ``web_search`` keeps its own denial code
    rather than folding into ``xai_server_tool_denied``, because "the host does
    not offer this" is a different thing for an agent to read than "you
    declared something unrecognised"."""
    search_violation = _search_parameters_violation(payload)
    if search_violation is not None:
        return search_violation
    for tool in _iter_tool_objects(payload):
        tool_type = tool.get("type")
        if tool_type == "mcp":
            return "xai_remote_mcp_denied"
        if tool_type == "web_search":
            return "xai_web_search_denied"
        return "xai_server_tool_denied"
    if _contains_key(payload, "server_url"):
        return "xai_remote_mcp_denied"
    return None


# xAI's second way of asking for a server-side search. It is not a ``tools``
# entry and carries no ``type`` of its own, so the tool collection above cannot
# see it: a request may ask for a live search purely with
# ``{"search_parameters": {"mode": "on"}}``. Left unhandled that is a silent
# bypass of the server-tool decision, so it is decided here, first.
_SEARCH_PARAMETERS_KEY = "search_parameters"


def _search_parameters_violation(payload: Any) -> str | None:
    """Deny xAI's second way of asking for a live search.

    Fail closed on the mode: only an explicit ``mode: "off"`` is off, so an
    absent, unknown, or non-string mode counts as a live search and is denied.
    ``sources`` is not read at all — no corpus is reachable, so which one a
    request names decides nothing."""
    for parameters in _iter_search_parameters(payload):
        mode = parameters.get("mode")
        if isinstance(mode, str) and mode.strip().lower() == "off":
            continue
        return "xai_web_search_denied"
    return None


def _iter_search_parameters(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _is_replay_item(node):
                return
            parameters = node.get(_SEARCH_PARAMETERS_KEY)
            if isinstance(parameters, dict):
                found.append(parameters)
            elif parameters is not None:
                # Present but not an object: undecidable, so record an empty
                # declaration and let the caller deny it.
                found.append({})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _contains_key(payload: Any, key: str) -> bool:
    if isinstance(payload, dict):
        if _is_replay_item(payload):
            # A replayed `mcp_call` names the server the earlier call reached.
            # That is history, not a new declaration, and remote MCP is denied
            # on the way out regardless.
            return False
        if key in payload:
            return True
        return any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(item, key) for item in payload)
    return False


def _iter_tool_objects(payload: Any) -> list[dict[str, Any]]:
    """Collect every guarded tool object in the request.

    Two collection rules, because they fail closed against different things:

    1. Every non-``function`` entry of a ``tools`` array, wherever that array
       appears. A ``tools`` array is a declaration site, so an unrecognised
       entry there is an undeclarable capability rather than an unknown shape —
       denying it is what keeps a hosted tool xAI adds later from being
       forwarded unreviewed.
    2. The named hosted families anywhere else in the body: a remote-MCP tool
       (``type: mcp``), any ``type`` starting with ``web`` (covering
       ``web_search`` and any future dated or renamed variant), or a
       ``_DENIED_HOSTED_TOOL_TYPES`` member. This catches a declaration nested
       under some other key.

    Replay history items are excluded from both, along with their entire
    subtrees: they describe an earlier hosted call rather than declaring a new
    one, and appear in legitimate follow-up requests. Descending into one would
    read `mcp_list_tools`'s array of already-executed tool descriptors as a
    declaration site and deny the turn."""
    matches: list[dict[str, Any]] = []

    def is_guarded(node: dict[str, Any]) -> bool:
        # Replay items never reach here; walk returns before calling this.
        type_value = node.get("type")
        if not isinstance(type_value, str):
            return False
        return (
            type_value == "mcp"
            or type_value.startswith("web")
            or type_value in _DENIED_HOSTED_TOOL_TYPES
        )

    def walk(node: Any, *, in_tools_array: bool = False) -> None:
        if isinstance(node, dict):
            if _is_replay_item(node):
                return
            raw_type = node.get("type")
            declared = in_tools_array and raw_type != _CLIENT_EXECUTED_TOOL_TYPE
            if declared or is_guarded(node):
                matches.append(node)
            for key, value in node.items():
                if key == _SEARCH_PARAMETERS_KEY:
                    # Decided by _search_parameters_violation, which reports it
                    # as `xai_web_search_denied`. Descending would collect its
                    # {"type": "web"}-shaped source entries on the web-prefix
                    # rule and report the same request as an unrecognised
                    # hosted tool instead.
                    continue
                walk(value, in_tools_array=key == "tools")
        elif isinstance(node, list):
            for item in node:
                walk(item, in_tools_array=in_tools_array)

    walk(payload)
    return matches
