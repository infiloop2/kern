"""Agent-facing tools service: HTTP over a Unix domain socket.

Agent runtimes call bundled tools through an MCP shim
(``host.runtime.agent_shim.mcp_shim``) that forwards to this service (the
dedicated ``kern-tools`` process; see ``tools_service``). Unix peer
credentials give a kernel-verified caller identity, and every route is
scoped to exactly one peer: the agent uid gets the MCP surface
(``GET /tools``, ``POST /call``) and the admin uid gets the operator
delegation routes (``POST /operator/...``) — neither can call the other's
routes, no admin password involved and none required.

The agent-facing HTTP surface is four routes:

- ``GET /tools`` — a constant four-entry declaration: ``list_bundled_tools``,
  ``describe_tool``, ``call_tool``, and ``check_tool_approval``. It does not
  enumerate the bundled actions and does not vary with the enablement set; the
  catalog is reached by calling those declarations instead (see
  ``host/agent_tool_surface.py`` for why the listing must stay constant). The
  MCP shim separately aggregates the network introspection and the stable
  Workspace/history tools into the agent-facing tool list.
- ``POST /call`` — ``{"name": ..., "input": {...}}`` executes one action and
  returns either the JSON result shape from ``tools_host`` (``executed`` /
  ``pending_approval`` / ``failed``) or one exclusive binary asset response.
- ``POST /assets/video``: raw MP4/MOV bytes streamed by the MCP shim, with
  bounded metadata in headers; returns an opaque tool-scoped asset id.
- ``POST /assets/image``: raw JPEG/PNG/WebP bytes streamed by the MCP shim,
  with the same private, bounded, tool-scoped storage contract.
Approval status checks are a built-in action invoked through ``POST /call``,
so the agent can resume after the operator decides in the admin UI.

Approval-gated actions never execute during the initial agent call: they create
a pending approval and the operator decides in the admin UI (see ``admin_api``).
"""

from __future__ import annotations

from http import HTTPStatus
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, BinaryIO, NoReturn, cast
from urllib.parse import quote, unquote

from host import agent_tool_surface
from host.constants import TOOLS_SOCKET_PATH
from host.runtime.core import host_errors, state
from host.runtime.core.unix_socket_service import (
    UnixSocketRequestHandler,
    UnixSocketServer,
    peer_uids,
)
from host.runtime.tools import assets as tool_assets, tools_host
from host.tools import ToolServiceError
from host.tools import OpenedStreamingAsset, StreamingAssetError
from host.tools.shared.web import ProviderWarning, UnmappedProviderError

SOCKET_PATH = os.environ.get("KERN_TOOLS_SOCKET", TOOLS_SOCKET_PATH)
# Peers are scoped strictly by path: the agent gets exactly the MCP surface
# (GET /tools, POST /call), and the admin service gets exactly the operator
# delegation routes (POST /operator/...) that need this service's egress
# (OAuth code exchange, token revoke) or run tool code that touches
# third-party data. Neither peer can call the other's routes.
AGENT_PEER_USER = "kern-agent"
ADMIN_PEER_USER = "kern-admin"
MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_VIDEO_BODY_BYTES = tool_assets.MAX_VIDEO_BYTES
MAX_IMAGE_BODY_BYTES = tool_assets.MAX_IMAGE_BYTES
# Tool calls block a handler thread on third-party requests (30s timeouts in
# most packages, minutes for a synchronous image render), so cap concurrency
# instead of letting a runaway agent stack threads.
MAX_CONCURRENT_CALLS = 8
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
ASSET_CLEANUP_INTERVAL_SECONDS = 3600
MAX_STREAMING_ASSET_BYTES = 200_000_000
STREAMING_RESULT_HEADER = "streaming-asset"
# The media type is emitted verbatim as the Content-Type header, so its shape
# is enforced here at the socket boundary rather than trusted from the tool.
STREAMING_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)

CHECK_APPROVAL_TOOL = agent_tool_surface.CHECK_APPROVAL_TOOL
LIST_BUNDLED_TOOLS_TOOL = agent_tool_surface.LIST_BUNDLED_TOOLS_TOOL
DESCRIBE_TOOL_TOOL = agent_tool_surface.DESCRIBE_TOOL_TOOL
CALL_TOOL_TOOL = agent_tool_surface.CALL_TOOL_TOOL


def action_listing() -> list[dict[str, Any]]:
    """The agent-facing tool declarations for this socket.

    Deliberately constant: it does not consult the enablement set or the
    bundled registry. Declarations head the model prompt, so a listing that
    varied with operator state would re-encode the whole cached context on
    every toggle. The catalog itself is reached through list_bundled_tools /
    describe_tool / call_tool, whose results append instead
    (host/agent_tool_surface.py).
    """
    return [dict(tool) for tool in agent_tool_surface.TOOLS_SOCKET_TOOLS]


def _resolve_action(name: str) -> tuple[str, str] | None:
    """Map a flat ``<tool_id>_<action>`` name back to (tool_id, action).

    These names are no longer listed — the agent addresses actions through
    call_tool — but approval records and audit rows store them, so they stay
    resolvable."""
    for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
        prefix = f"{tool_id}_"
        if name.startswith(prefix) and tool.manifest.action(name[len(prefix):]) is not None:
            return tool_id, name[len(prefix):]
    return None


def call_action(
    name: Any,
    tool_input: Any,
    asset_store: tool_assets.ToolAssetStore | None = None,
) -> dict[str, Any] | tools_host.StreamingAction:
    if not isinstance(name, str):
        raise tools_host.ToolCallError("Tool name must be a string.")
    if name == "check_tool_approval":
        return _check_approval(tool_input)
    if name == "list_bundled_tools":
        return _list_bundled_tools(tool_input)
    if name == "describe_tool":
        return _describe_tool(tool_input)
    if name == "call_tool":
        return _call_tool(tool_input, asset_store)
    # Flat "<tool_id>_<action_id>" names are no longer listed, but stay
    # callable: approval records, audit rows, and any agent that learned a name
    # before this change all address actions that way.
    resolved = _resolve_action(name)
    if resolved is None:
        raise tools_host.ToolCallError(f"Unknown tool: {name}.")
    return tools_host.execute_action(resolved[0], resolved[1], tool_input, asset_store)


def _string_field(tool_input: Any, field: str) -> str:
    if not isinstance(tool_input, dict):
        raise tools_host.ToolCallError("Tool input must be an object.")
    value = tool_input.get(field)
    if not isinstance(value, str) or not value:
        raise tools_host.ToolCallError(f"{field} must be a non-empty string.")
    return value


def _list_bundled_tools(tool_input: Any) -> dict[str, Any]:
    """The bundled catalog, optionally restricted to known tool ids.

    The unfiltered form is a cheap capability index: it lets an agent tell the
    operator which existing tool to enable instead of rebuilding it, but omits
    every action and agent note. Those fields grow with each integration and
    are useful only after the agent has selected a tool. Passing tool_ids
    returns the focused entries with actions and agent_notes; input and output
    schemas remain behind describe_tool."""
    if not isinstance(tool_input, dict):
        raise tools_host.ToolCallError("Tool input must be an object.")
    if set(tool_input) - {"tool_ids"}:
        raise tools_host.ToolCallError("list_bundled_tools accepts only tool_ids.")

    requested = tool_input.get("tool_ids")
    if requested is None:
        selected_ids = list(tools_host.BUNDLED_TOOLS)
        unknown_ids: list[str] | None = None
    else:
        if (
            not isinstance(requested, list)
            or not 1 <= len(requested) <= 32
            or any(not isinstance(tool_id, str) or not tool_id for tool_id in requested)
            or len(set(requested)) != len(requested)
        ):
            raise tools_host.ToolCallError(
                "tool_ids must be an array of 1 to 32 unique non-empty strings."
            )
        selected_ids = [tool_id for tool_id in requested if tool_id in tools_host.BUNDLED_TOOLS]
        unknown_ids = [tool_id for tool_id in requested if tool_id not in tools_host.BUNDLED_TOOLS]

    enabled = state.enabled_tool_ids()
    tools = []
    for tool_id in selected_ids:
        tool = tools_host.BUNDLED_TOOLS[tool_id]
        manifest = tool.manifest
        entry: dict[str, Any] = {
            "tool_id": tool_id,
            "display_name": manifest.display_name,
            "description": manifest.description,
            "connection": manifest.connection,
            "enabled": tool_id in enabled,
        }
        if requested is not None:
            actions: list[dict[str, Any]] = []
            for spec in manifest.actions:
                action: dict[str, Any] = {"id": spec.id, "description": spec.description}
                # Only the exceptional case is stated; absent means "direct".
                if spec.approval == "operator":
                    action["approval"] = "operator"
                actions.append(action)
            entry["agent_notes"] = manifest.agent_notes
            entry["actions"] = actions
            if manifest.connection == "oauth":
                entry["connected_accounts"] = state.tool_connections(tool_id)
        tools.append(entry)
    result: dict[str, Any] = {"tools": tools}
    if unknown_ids is not None:
        result["unknown_tool_ids"] = unknown_ids
    return {"status": "executed", "result": result}


def _describe_tool(tool_input: Any) -> dict[str, Any]:
    """One bundled tool's actions with their full input and output schemas.

    The output schema is what the agent gets back, so it belongs next to the
    input schema: it says which fields a result carries and what each means,
    which is how the agent plans the call after this one without running the
    action to find out. It is present only for actions that return a JSON
    result; an approval-gated action and one that returns a file both carry no
    output schema, which the approval field and the description already state.

    Agent guidance is included once per described tool. Broad discovery omits
    it to keep the capability index small, so describe_tool must be a
    self-contained path from an unfiltered catalog to safe tool use. A caller
    that used focused discovery may see the same short note twice."""
    tool_id = _string_field(tool_input, "tool_id")
    if set(tool_input) - {"tool_id"}:
        raise tools_host.ToolCallError("describe_tool accepts only tool_id.")
    tool = tools_host.BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise tools_host.ToolCallError(
            f"Unknown tool_id: {tool_id}. Call list_bundled_tools for the catalog."
        )
    manifest = tool.manifest
    return {
        "status": "executed",
        "result": {
            "tool_id": tool_id,
            "display_name": manifest.display_name,
            "enabled": tool_id in state.enabled_tool_ids(),
            "agent_notes": manifest.agent_notes,
            **(
                {"connected_accounts": state.tool_connections(tool_id)}
                if manifest.connection == "oauth"
                else {}
            ),
            "actions": [
                {
                    "id": spec.id,
                    "description": spec.description,
                    "approval": spec.approval,
                    "input_schema": spec.input_schema,
                    **({"output_schema": spec.output_schema} if spec.output_schema else {}),
                }
                for spec in manifest.actions
            ],
        },
    }


def _call_tool(
    tool_input: Any,
    asset_store: tool_assets.ToolAssetStore | None,
) -> dict[str, Any] | tools_host.StreamingAction:
    """Invoke one bundled action addressed by tool_id and action_id."""
    tool_id = _string_field(tool_input, "tool_id")
    action_id = _string_field(tool_input, "action_id")
    extra = set(tool_input) - {"tool_id", "action_id", "connection_id", "input"}
    if extra:
        raise tools_host.ToolCallError(
            "call_tool accepts only tool_id, action_id, connection_id, and input; "
            f"got {', '.join(sorted(extra))}."
        )
    tool = tools_host.BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise tools_host.ToolCallError(
            f"Unknown tool_id: {tool_id}. Call list_bundled_tools for the catalog."
        )
    if tool.manifest.action(action_id) is None:
        raise tools_host.ToolCallError(
            f"Unknown action_id for {tool_id}: {action_id}. Call describe_tool for its actions."
        )
    action_input = tool_input.get("input")
    if action_input is None:
        action_input = {}
    connection_id = tool_input.get("connection_id")
    if connection_id is not None and (not isinstance(connection_id, str) or not connection_id):
        raise tools_host.ToolCallError("connection_id must be a non-empty string when provided.")
    return tools_host.execute_action(
        tool_id,
        action_id,
        action_input,
        asset_store,
        connection_id=connection_id,
    )


def _check_approval(tool_input: Any) -> dict[str, Any]:
    approval_id = tool_input.get("approval_id") if isinstance(tool_input, dict) else None
    if not isinstance(approval_id, str) or not approval_id:
        raise tools_host.ToolCallError("check_tool_approval requires approval_id.")
    # The id carries the token; state.tool_approval verifies it constant-time
    # and returns None on any mismatch, so a guessed number never resolves.
    record = state.tool_approval(approval_id)
    if record is None:
        raise tools_host.ToolCallError(f"Unknown approval: {approval_id}.")
    result: dict[str, Any] = {
        "status": "executed",
        "result": {
            "approval_id": record["approval_id"],
            "approval_status": record["status"],
            "summary": record["summary"],
        },
    }
    if record["status"] in {"executed", "failed"}:
        result["result"]["execution_result"] = record["result"]
    return result


class OperatorError(Exception):
    """An operator delegation failure carrying the HTTP status to return."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# Operator operations the admin service reverse-proxies to this service: the
# whole OAuth connect flow (so no OAuth tool code runs in the admin service) and
# approval decisions (which run the approved payload over this service's egress).
OPERATOR_START_RE = re.compile(r"^/operator/tools/([a-z0-9_]{1,64})/oauth_connect/start$")
OPERATOR_COMPLETE_RE = re.compile(r"^/operator/tools/([a-z0-9_]{1,64})/oauth_connect/complete$")
OPERATOR_DISCONNECT_RE = re.compile(r"^/operator/tools/([a-z0-9_]{1,64})/oauth_connect/disconnect$")
OPERATOR_SERVICE_RE = re.compile(
    r"^/operator/tools/([a-z0-9_]{1,64})/service/"
    r"(status|connect|disconnect|enable|disable)$"
)
OPERATOR_DECIDE_RE = re.compile(
    r"^/operator/tools/([a-z0-9_]{1,64})/approvals/([A-Za-z0-9._:-]{1,128})/(approve|deny)$"
)
CONNECTION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _operator_connection_id(body: Any, *, create: bool = False) -> str:
    value = body.get("connection_id") if isinstance(body, dict) else None
    if value is None and create:
        return f"connection_{secrets.token_hex(12)}"
    if value is None:
        raise OperatorError(HTTPStatus.BAD_REQUEST, "connection_id is required")
    if not isinstance(value, str) or CONNECTION_ID_RE.fullmatch(value) is None:
        raise OperatorError(HTTPStatus.BAD_REQUEST, "connection_id is invalid")
    return value


def _operator_connect_flow(tool_id: str, *, require_enabled: bool = True) -> Any:
    """The tool's OAuth connect flow (tool.credentials), or an OperatorError.
    tool.credentials is the single source of truth for whether a tool has a
    connect flow; a non-None flow narrows the type for the connect calls."""
    tool = tools_host.BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise OperatorError(HTTPStatus.NOT_FOUND, f"unknown tool: {tool_id}")
    flow = tool.credentials
    if flow is None:
        raise OperatorError(HTTPStatus.CONFLICT, f"{tool_id} has no OAuth connect flow")
    if require_enabled and tool_id not in state.enabled_tool_ids():
        raise OperatorError(HTTPStatus.CONFLICT, f"{tool_id} is not enabled")
    return flow


def _operator_start_connect(
    tool_id: str, body: Any, asset_store: tool_assets.ToolAssetStore | None = None
) -> dict[str, Any]:
    flow = _operator_connect_flow(tool_id)
    if not isinstance(body, dict) or not isinstance(body.get("redirect_uri"), str) or not body["redirect_uri"]:
        raise OperatorError(HTTPStatus.BAD_REQUEST, "redirect_uri is required")
    connection_id = _operator_connection_id(body, create=True)
    tool = tools_host.bundled_tool(tool_id)
    api = tools_host.host_api_for(
        tool,
        tools_host.connection_scope(tool, connection_id),
        asset_store=asset_store,
    )
    try:
        result = flow.start_connect({"redirect_uri": body["redirect_uri"]}, api)
        return {**result, "connection_id": connection_id}
    except (ValueError, KeyError, tools_host.ToolConfigKeyUnsetError) as exc:
        raise OperatorError(HTTPStatus.BAD_REQUEST, str(exc) or "invalid connect request") from exc
    except ProviderWarning as exc:
        _report_operator_provider_warning(tool_id, "oauth_connect_start", exc)
    except Exception as exc:  # noqa: BLE001 - tool packages redact their messages
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc) or "tool connect flow failed") from exc


def _operator_complete_connect(
    tool_id: str, body: Any, asset_store: tool_assets.ToolAssetStore | None = None
) -> dict[str, Any]:
    flow = _operator_connect_flow(tool_id)
    if not isinstance(body, dict):
        raise OperatorError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
    params = {key: body.get(key) for key in ("code", "redirect_uri", "state")}
    if not all(isinstance(value, str) and value for value in params.values()):
        raise OperatorError(HTTPStatus.BAD_REQUEST, "code, redirect_uri, and state are required")
    connection_id = _operator_connection_id(body)
    tool = tools_host.bundled_tool(tool_id)
    api = tools_host.host_api_for(
        tool,
        tools_host.connection_scope(tool, connection_id),
        asset_store=asset_store,
    )
    try:
        result = flow.complete_connect(params, api)
    except (ValueError, KeyError, tools_host.ToolConfigKeyUnsetError) as exc:
        raise OperatorError(HTTPStatus.BAD_REQUEST, str(exc) or "invalid connect request") from exc
    except ProviderWarning as exc:
        _report_operator_provider_warning(tool_id, "oauth_connect_complete", exc)
    except Exception as exc:  # noqa: BLE001 - tool packages redact their messages
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc) or "tool connect flow failed") from exc
    account = result.get("account") if isinstance(result, dict) else None
    label = account.get("label") if isinstance(account, dict) else None
    account_id = account.get("id") if isinstance(account, dict) else None
    state.record_tool_event(
        tool_id,
        "oauth_connect",
        "connected",
        label if isinstance(label, str) else "",
        connection_id=connection_id,
        account_id=account_id if isinstance(account_id, str) else "",
        account_label=label if isinstance(label, str) else "",
    )
    return {**result, "connection_id": connection_id}


def _operator_disconnect(
    tool_id: str,
    body: Any = None,
    asset_store: tool_assets.ToolAssetStore | None = None,
) -> dict[str, Any]:
    # Disconnect skips the enabled gate so stored tokens can always be revoked.
    flow = _operator_connect_flow(tool_id, require_enabled=False)
    connection_id = _operator_connection_id({} if body is None else body)
    credential = state.tool_credential(tool_id, connection_id)
    account = credential["account"] if credential is not None else {"id": "", "label": ""}
    tool = tools_host.bundled_tool(tool_id)
    api = tools_host.host_api_for(
        tool,
        tools_host.connection_scope(tool, connection_id),
        asset_store=asset_store,
    )
    try:
        flow.disconnect(api)
    except ProviderWarning as exc:
        _report_operator_provider_warning(tool_id, "oauth_disconnect", exc)
    except Exception as exc:  # noqa: BLE001 - tool packages redact their messages
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc) or "tool disconnect failed") from exc
    state.record_tool_event(
        tool_id,
        "oauth_connect",
        "disconnected",
        account["label"],
        connection_id=connection_id,
        account_id=account["id"],
        account_label=account["label"],
    )
    return {"tool_id": tool_id, "connection_id": connection_id, "connected": False}


def _report_operator_provider_warning(tool_id: str, action_id: str, exc: ProviderWarning) -> NoReturn:
    context: dict[str, Any] = {
        "tool_id": tool_id,
        "action_id": action_id,
        "provider": exc.provider,
        "operation": exc.operation,
        "http_status": exc.status,
    }
    if exc.response_body:
        context["provider_response"] = exc.response_body
    host_errors.report_warning(
        "tools.operator_provider_request",
        exc,
        context=context,
        kind="provider_failure",
    )
    message = (
        "Provider request failed. Check Host diagnostics for details."
        if isinstance(exc, UnmappedProviderError)
        else str(exc)
    )
    raise OperatorError(
        HTTPStatus.BAD_GATEWAY,
        message,
    ) from None


def _operator_decide(
    tool_id: str,
    approval_id: str,
    decision: str,
    asset_store: tool_assets.ToolAssetStore | None = None,
) -> dict[str, Any]:
    # The approval is addressed under its tool, so reject a decision whose tool
    # does not own the approval before spending it.
    if state.tool_approval(approval_id, tool_id=tool_id) is None:
        raise OperatorError(HTTPStatus.NOT_FOUND, "unknown approval")
    try:
        return tools_host.decide_approval(approval_id, decision, asset_store)
    except tools_host.ToolCallError as exc:
        raise OperatorError(HTTPStatus.CONFLICT, str(exc)) from exc


def _operator_service(tool_id: str, operation: str) -> dict[str, Any]:
    tool = tools_host.BUNDLED_TOOLS.get(tool_id)
    if tool is None:
        raise OperatorError(HTTPStatus.NOT_FOUND, f"unknown tool: {tool_id}")
    service = tools_host.tool_service(tool)
    if service is None:
        raise OperatorError(HTTPStatus.CONFLICT, f"{tool_id} has no managed service")
    try:
        return dict(service.operator(operation, lambda: tool_id in state.enabled_tool_ids()))
    except ToolServiceError as exc:
        raise OperatorError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc


def handle_operator(
    path: str,
    body: Any,
    asset_store: tool_assets.ToolAssetStore | None = None,
) -> dict[str, Any]:
    """Dispatch one admin-delegated operator operation. Raises OperatorError
    with the HTTP status the admin service should return."""
    start = OPERATOR_START_RE.fullmatch(path)
    if start:
        return _operator_start_connect(start.group(1), body, asset_store)
    complete = OPERATOR_COMPLETE_RE.fullmatch(path)
    if complete:
        return _operator_complete_connect(complete.group(1), body, asset_store)
    disconnect = OPERATOR_DISCONNECT_RE.fullmatch(path)
    if disconnect:
        return _operator_disconnect(disconnect.group(1), body, asset_store)
    service = OPERATOR_SERVICE_RE.fullmatch(path)
    if service:
        return _operator_service(service.group(1), service.group(2))
    decide = OPERATOR_DECIDE_RE.fullmatch(path)
    if decide:
        return _operator_decide(
            decide.group(1), decide.group(2), decide.group(3), asset_store
        )
    raise OperatorError(HTTPStatus.NOT_FOUND, "unknown path")


def agent_peer_uids() -> frozenset[int]:
    """The uids allowed to call the agent MCP routes (GET /tools, POST /call):
    the agent only. Falls back to the current uid off a bootstrapped host."""
    return peer_uids(AGENT_PEER_USER)


def admin_peer_uids() -> frozenset[int]:
    """The uids allowed to call the operator delegation routes: the admin
    service only. Falls back to the current uid off a bootstrapped host."""
    return peer_uids(ADMIN_PEER_USER)


class ToolsRequestHandler(UnixSocketRequestHandler):
    server: "ToolsServer"

    def _peer_is_agent(self) -> bool:
        return self._peer()[1] in self.server.agent_uids

    def _peer_is_admin(self) -> bool:
        return self._peer()[1] in self.server.admin_uids

    def do_GET(self) -> None:
        # The sole GET route belongs to the agent MCP surface.
        if not self._peer_is_agent():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        if self.path == "/tools":
            self._send_json(HTTPStatus.OK, {"tools": action_listing()})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})

    def do_DELETE(self) -> None:
        if not self._peer_is_agent():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})

    @staticmethod
    def _validated_stream_metadata(opened: OpenedStreamingAsset) -> tuple[str, str, int]:
        filename = opened.filename
        if (
            not isinstance(filename, str)
            or not 1 <= len(filename.encode("utf-8")) <= 255
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        ):
            raise ValueError("invalid filename")
        size_bytes = opened.size_bytes
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= MAX_STREAMING_ASSET_BYTES
        ):
            raise ValueError("invalid size")
        media_type = opened.media_type
        if not isinstance(media_type, str) or not STREAMING_MEDIA_TYPE_RE.fullmatch(media_type):
            raise ValueError("invalid media type")
        return filename, media_type, size_bytes

    def _send_streaming_action(self, streaming: tools_host.StreamingAction) -> None:
        """Relay one exclusive binary result without landing it on admin disk."""
        headers_committed = False
        error: str | None = None
        try:
            with streaming.asset.open_stream() as opened:
                filename, media_type, size_bytes = self._validated_stream_metadata(opened)
                self.send_response(HTTPStatus.OK.value)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(size_bytes))
                self.send_header("X-Kern-Result", STREAMING_RESULT_HEADER)
                self.send_header("X-Kern-Filename", quote(filename, safe=""))
                self.send_header("Cache-Control", "private, no-store, max-age=0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                headers_committed = True

                # Keep the final chunk until one-byte lookahead proves the
                # provider supplied exactly the declared length. An oversized
                # stream therefore reaches the shim as a truncated response,
                # never as an apparently complete file.
                remaining = size_bytes
                pending = b""
                while remaining:
                    requested = min(1024 * 1024, remaining)
                    chunk = opened.source.read(requested)
                    if not chunk:
                        raise StreamingAssetError("Tool asset stream ended early.")
                    if len(chunk) > requested:
                        raise StreamingAssetError("Tool asset stream exceeded its declared size.")
                    if pending:
                        self.wfile.write(pending)
                    pending = chunk
                    remaining -= len(chunk)
                if opened.source.read(1):
                    raise StreamingAssetError("Tool asset stream exceeded its declared size.")
                self.wfile.write(pending)
                self.wfile.flush()
        except StreamingAssetError as exc:
            error = str(exc) or "Tool asset stream failed."
        except ProviderWarning as exc:
            context: dict[str, Any] = {
                "tool_id": streaming.tool_id,
                "action_id": streaming.action,
                "provider": exc.provider,
                "operation": exc.operation,
                "http_status": exc.status,
            }
            if exc.response_body:
                context["provider_response"] = exc.response_body
            host_errors.report_warning(
                "tools.streaming_provider_request",
                exc,
                context=context,
                kind="provider_failure",
            )
            error = (
                "Provider request failed. Check Host diagnostics for details."
                if isinstance(exc, UnmappedProviderError)
                else str(exc)
            )
        except ValueError:
            error = "Tool returned invalid streaming asset metadata."
        except Exception as exc:
            host_errors.report_warning(
                "tools.streaming_asset",
                exc,
                context={"tool_id": streaming.tool_id, "action_id": streaming.action},
            )
            error = "Tool asset stream failed."

        tools_host.finish_streaming_action(streaming, error)
        if error is not None and not headers_committed:
            self._send_json(
                HTTPStatus.OK,
                {"status": "failed", "error": error, "reconnect_required": False},
            )

    def do_POST(self) -> None:
        # Peers are scoped strictly by path: operator delegation routes belong
        # to the admin service, everything else (the agent MCP surface) to the
        # agent; neither peer can call the other's routes.
        is_operator = self.path.startswith("/operator/")
        if is_operator and not self._peer_is_admin():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Operator routes require the admin peer."})
            return
        if not is_operator and not self._peer_is_agent():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        asset_kind = {"/assets/video": "video", "/assets/image": "image"}.get(self.path)
        if not is_operator and self.path not in {"/call", "/assets/video", "/assets/image"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Malformed Content-Length."})
            return
        if length < 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Malformed Content-Length."})
            return
        max_length = (
            MAX_VIDEO_BODY_BYTES if asset_kind == "video"
            else MAX_IMAGE_BODY_BYTES if asset_kind == "image"
            else MAX_REQUEST_BODY_BYTES
        )
        if length > max_length:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large."})
            return
        if asset_kind is not None:
            self._stage_asset(length, cast(tool_assets.AssetKind, asset_kind))
            return
        if is_operator:
            # Operator routes are operator-initiated and low volume, so they do not
            # share the agent-call concurrency cap; a busy agent must not be able to
            # 429 the operator's approve/deny/connect/disconnect.
            body = self.read_json_object_body(length)
            if body is None:
                return
            try:
                operator_result = handle_operator(self.path, body, self.server.asset_store)
            except OperatorError as exc:
                self._send_json(exc.status, {"error": exc.message})
                return
            self._send_json(HTTPStatus.OK, operator_result)
            return
        # Agent tool calls each block a handler thread on a third-party request, so
        # they are capacity-capped, checked before the agent-controlled body is read.
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many concurrent tool calls."})
            return
        action_result: dict[str, Any] | tools_host.StreamingAction
        try:
            try:
                body = self.read_json_object_body(length)
                if body is None:
                    return
                action_result = call_action(
                    body.get("name"), body.get("input"), self.server.asset_store
                )
            except tools_host.ToolCallError as exc:
                action_result = {"status": "failed", "error": str(exc), "reconnect_required": False}
            except Exception as exc:
                # Tool packages map their own errors; anything else must not leak
                # internals to the agent.
                host_errors.report_warning("tools.agent_call", exc)
                action_result = {"status": "failed", "error": "Tool call failed.", "reconnect_required": False}
            if isinstance(action_result, tools_host.StreamingAction):
                self._send_streaming_action(action_result)
            else:
                self._send_json(HTTPStatus.OK, action_result)
        finally:
            _CALL_SLOTS.release()

    def _stage_asset(self, length: int, kind: tool_assets.AssetKind) -> None:
        """Receive raw media bytes from the agent-side shim into private,
        tools-owned storage. Metadata comes from bounded headers; a pathname
        is never accepted by this service."""
        if not _UPLOAD_SLOTS.acquire(blocking=False):
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "Too many concurrent asset uploads."},
            )
            return
        try:
            tool_id = self.headers.get("X-Kern-Tool") or ""
            allowed_tools = (
                {"runway", "instagram"} if kind == "video" else {"runway", "openai_images"}
            )
            if tool_id not in allowed_tools:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"{kind.title()} destination tool is invalid."},
                )
                return
            # Refuse to stage into a disabled tool: otherwise the agent could
            # fill the bounded asset store with uploads for a tool the
            # operator never enabled and can never use.
            if tool_id not in state.enabled_tool_ids():
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "The destination tool is not enabled."},
                )
                return
            encoded_filename = self.headers.get("X-Kern-Filename") or ""
            if len(encoded_filename) > 1024:
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": f"{kind.title()} filename is too long."}
                )
                return
            try:
                filename = unquote(encoded_filename, errors="strict")
            except (UnicodeDecodeError, ValueError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"{kind.title()} filename is invalid."})
                return
            media_type = (self.headers.get("Content-Type") or "").lower()
            try:
                metadata = self.server.asset_store.stage(
                    kind=kind,
                    tool_id=tool_id,
                    filename=filename,
                    media_type=media_type,
                    size_bytes=length,
                    source=cast(BinaryIO, self.rfile),
                )
            except tool_assets.AssetError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except Exception as exc:
                host_errors.report_warning(
                    "tools.asset_staging",
                    exc,
                    context={"tool_id": tool_id, "asset_kind": kind},
                )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"{kind.title()} staging failed."},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    f"{kind}_asset_id": metadata.asset_id,
                    "filename": metadata.filename,
                    "media_type": metadata.media_type,
                    "size_bytes": metadata.size_bytes,
                    "sha256": metadata.sha256,
                    "expires_at": metadata.expires_at,
                    "for_tool": tool_id,
                },
            )
        finally:
            _UPLOAD_SLOTS.release()


class ToolsServer(UnixSocketServer):
    def __init__(
        self,
        socket_path: str,
        agent_uids: frozenset[int],
        admin_uids: frozenset[int] | None = None,
        asset_root: Path | None = None,
    ) -> None:
        # Strictly path-scoped peers: agent_uids reach the agent MCP routes,
        # admin_uids reach the operator delegation routes, nothing overlaps by
        # construction (off a bootstrapped host both fall back to the
        # developer's uid).
        self.agent_uids = agent_uids
        self.admin_uids = admin_uids if admin_uids is not None else admin_peer_uids()
        self.asset_store = tool_assets.ToolAssetStore(
            asset_root or Path(socket_path).parent / "assets", clean_start=True
        )
        self._next_asset_cleanup = time.monotonic() + ASSET_CLEANUP_INTERVAL_SECONDS
        super().__init__(socket_path, ToolsRequestHandler)

    def service_actions(self) -> None:
        """Delete expired staged media hourly even when no tool call touches it."""
        now = time.monotonic()
        if now < self._next_asset_cleanup:
            return
        self._next_asset_cleanup = now + ASSET_CLEANUP_INTERVAL_SECONDS
        self.asset_store.cleanup_expired()


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    """Bind the tools socket and serve it in the foreground (the dedicated
    kern-tools service entry point)."""
    ToolsServer(
        socket_path,
        agent_peer_uids(),
        asset_root=tool_assets.DEFAULT_ASSET_ROOT,
    ).serve_forever()
