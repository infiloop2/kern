"""Agent network introspection over a peer-authenticated Unix socket.

This service exposes only the two read-only network tools. It runs as the
non-egress ``kern-agent-network`` user and reads only the policy and
network-event tables granted to that database role. The MCP shim aggregates
this socket with the independent tools and app sockets.
"""

from __future__ import annotations

from http import HTTPStatus
import os
import threading
from typing import Any

from host import agent_tool_surface
from host.constants import AGENT_NETWORK_SOCKET_PATH
from host.network_integrations import registry
from host.runtime.core import host_errors, network_policy, state
from host.runtime.core.unix_socket_service import (
    UnixSocketRequestHandler,
    UnixSocketServer,
    peer_uids,
)

SOCKET_PATH = os.environ.get("KERN_AGENT_NETWORK_SOCKET", AGENT_NETWORK_SOCKET_PATH)
AGENT_PEER_USER = "kern-agent"
MAX_REQUEST_BODY_BYTES = 16 * 1024
MAX_CONCURRENT_CALLS = 8
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)

LIST_NETWORK_INTEGRATIONS_TOOL = agent_tool_surface.LIST_NETWORK_INTEGRATIONS_TOOL
RECENT_NETWORK_DENIALS_TOOL = agent_tool_surface.RECENT_NETWORK_DENIALS_TOOL


class NetworkToolCallError(ValueError):
    pass


def action_listing() -> list[dict[str, Any]]:
    return [LIST_NETWORK_INTEGRATIONS_TOOL, RECENT_NETWORK_DENIALS_TOOL]


def call_action(name: Any, tool_input: Any) -> dict[str, Any]:
    if name == "list_network_integrations":
        return _list_network_integrations()
    if name == "recent_network_denials":
        return _recent_network_denials(tool_input)
    raise NetworkToolCallError(f"Unknown network tool: {name}.")


def _list_network_integrations() -> dict[str, Any]:
    policy = network_policy.load_policy()
    stored_integrations = policy.get("network_integrations")
    stored_integrations = stored_integrations if isinstance(stored_integrations, dict) else {}
    integrations = []
    for integration_id, registered in registry.NETWORK_INTEGRATIONS.items():
        stored = stored_integrations.get(integration_id)
        stored = stored if isinstance(stored, dict) else {}
        entry: dict[str, Any] = {
            "integration_id": integration_id,
            "display_name": registered.manifest.display_name,
            "description": registered.manifest.description,
            # A disabled integration serializes away entirely, so presence in
            # the stored policy is enablement.
            "enabled": bool(stored),
        }
        options = {key: value for key, value in stored.items() if key != "enabled"}
        if options:
            entry["options"] = options
        integrations.append(entry)
    return {"status": "executed", "result": {"network_integrations": integrations}}


# A repeating denial (a polling client retrying the same blocked request) can
# push everything else out of the feed within the hour, so identical denials
# collapse into one counted entry. Only the first occurrence of an identity
# consumes the distinct budget — repeats merely extend their entry — so a
# poller flood cannot age rare one-off denials out of the tool's reach. The
# row bound still keeps one tool call from becoming an unbounded table walk.
_DENIAL_DISTINCT_SCAN_LIMIT = 1000
_DENIAL_ROW_SCAN_LIMIT = 20_000
# The scan pages at its own size rather than the admin API's EVENT_PAGE_LIMIT,
# which is a UI page size and the cap on an admin request's limit parameter.
# network_events_decision_seq_idx serves this query as an ordered range scan,
# so page size only trades round trips against the size of one materialized
# page: 100 rows would cost 200 round trips to reach the row bound, while a
# page as large as the bound would hold the whole flood in memory at once.
_DENIAL_PAGE_SIZE = 1000

_DENIAL_IDENTITY_FIELDS = ("protocol", "method", "host", "port", "path", "query", "reason_code")


def _recent_network_denials(tool_input: Any) -> dict[str, Any]:
    limit = 20
    if isinstance(tool_input, dict) and tool_input.get("limit") is not None:
        raw_limit = tool_input["limit"]
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or not 1 <= raw_limit <= 100:
            raise NetworkToolCallError("limit must be an integer between 1 and 100.")
        limit = raw_limit
    catalog = registry.denial_reason_catalog()
    denials: list[dict[str, Any]] = []
    collapsed: dict[tuple[Any, ...], dict[str, Any]] = {}
    cursor: int | None = None
    distinct = 0
    rows = 0
    while distinct < _DENIAL_DISTINCT_SCAN_LIMIT and rows < _DENIAL_ROW_SCAN_LIMIT:
        page = state.page_network_events_before(
            cursor,
            decision="denied",
            limit=min(_DENIAL_PAGE_SIZE, _DENIAL_ROW_SCAN_LIMIT - rows),
        )
        if not page:
            break
        for event in page:
            rows += 1
            cursor = event["seq"]
            identity = tuple(event.get(field) for field in _DENIAL_IDENTITY_FIELDS)
            existing = collapsed.get(identity)
            if existing is not None:
                existing["count"] += 1
                # Pages are newest-first, so this occurrence is the oldest yet.
                existing["first_timestamp"] = event["timestamp"]
                continue
            distinct += 1
            entry = {
                key: event[key]
                for key in ("timestamp", "protocol", "method", "host", "port", "path", "query")
                if key in event
            }
            entry["count"] = 1
            code = event.get("reason_code")
            if code is not None:
                entry["reason_code"] = code
                reason = catalog.get(code)
                if reason is not None:
                    entry["guidance"] = reason.guidance
            # Track identities past the display limit too, so their repeats
            # collapse instead of re-charging the distinct budget.
            collapsed[identity] = entry
            if len(denials) < limit:
                denials.append(entry)
            if distinct >= _DENIAL_DISTINCT_SCAN_LIMIT:
                # Stop on the exact event that exhausts the budget. Finishing
                # the page would admit up to a page of further identities and
                # make both the scan depth and the counts depend on where the
                # budget happened to fall within a page.
                break
    truncated = (
        (distinct >= _DENIAL_DISTINCT_SCAN_LIMIT or rows >= _DENIAL_ROW_SCAN_LIMIT)
        and bool(state.page_network_events_before(cursor, decision="denied", limit=1))
    )
    return {
        "status": "executed",
        "result": {"denials": denials, "truncated": truncated},
    }


def _agent_peer_uids() -> frozenset[int]:
    return peer_uids(AGENT_PEER_USER)


class NetworkIntrospectionRequestHandler(UnixSocketRequestHandler):
    server: "NetworkIntrospectionServer"

    def _peer_allowed(self) -> bool:
        return self._peer()[1] in self.server.agent_uids

    def do_GET(self) -> None:
        if not self._peer_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
        elif self.path != "/tools":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
        else:
            self._send_json(HTTPStatus.OK, {"tools": action_listing()})

    def do_POST(self) -> None:
        if not self._peer_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Peer not allowed."})
            return
        if self.path != "/call":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown path."})
            return
        length = self.bounded_content_length(MAX_REQUEST_BODY_BYTES)
        if length is None:
            return
        if not _CALL_SLOTS.acquire(blocking=False):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many concurrent calls."})
            return
        try:
            body = self.read_json_object_body(length)
            if body is None:
                return
            try:
                result = call_action(body.get("name"), body.get("input"))
            except NetworkToolCallError as exc:
                result = {"status": "failed", "error": str(exc)}
            except Exception as exc:
                host_errors.report_warning("agent_network.call", exc)
                result = {"status": "failed", "error": "Network introspection failed."}
            self._send_json(HTTPStatus.OK, result)
        finally:
            _CALL_SLOTS.release()


class NetworkIntrospectionServer(UnixSocketServer):
    def __init__(self, socket_path: str, agent_uids: frozenset[int]) -> None:
        self.agent_uids = agent_uids
        super().__init__(socket_path, NetworkIntrospectionRequestHandler)


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    NetworkIntrospectionServer(socket_path, _agent_peer_uids()).serve_forever()
