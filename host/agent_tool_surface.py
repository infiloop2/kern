"""The static agent-facing MCP tool surface.

Every tool the agent sees is declared here, once, as a constant. Nothing in
this module depends on runtime state: not which integrations the operator
enabled, not whether a tool socket is reachable, not which tool packages
happen to be installed.

That invariant is the point. Tool declarations sit at the head of the model
prompt, so any change to them invalidates the whole cached prefix behind them.
A listing assembled from live state changes when an operator toggles an
integration, and — before this module — changed when a socket call merely
blipped, which silently re-encoded an entire multi-hundred-thousand-token
context at cache-write prices. Measured against real sessions, one such change
in a 250k context cost more than declaring the full bundled catalog outright.

The bundled catalog is therefore reached by explicit discovery rather than by
listing every action: ``list_bundled_tools`` names what exists,
``describe_tool`` returns one tool's action schemas, and ``call_tool`` invokes
one action. Those schemas arrive as tool *results*, which append to the
context instead of rewriting its prefix.

This module is imported by the tools service, the agent-network service, and
the agent-side MCP shim, so it must stay dependency-free and stdlib-only.
"""

from __future__ import annotations

from typing import Any

JSONObject = dict[str, Any]

_NO_ARGUMENTS: JSONObject = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

# Bundled-catalog discovery. Always listed, whether or not any integration is
# enabled, so the agent can always distinguish "bundled but not enabled" (ask
# the operator to enable it) from "no bundled tool at all" (tell the operator
# it is not implemented).
LIST_BUNDLED_TOOLS_TOOL: JSONObject = {
    "name": "list_bundled_tools",
    "description": (
        "List every tool bundled with this Kern host: its tool_id, whether the operator "
        "has enabled it, and a one-line description of each action. Start here, then call "
        "describe_tool for the schemas of the actions you intend to use and call_tool to "
        "run one. An action marked approval=operator queues for operator approval instead "
        "of running immediately. A tool listed here but not enabled exists on the host but "
        "cannot run until the operator enables it (and, for OAuth tools, connects it) under "
        "Home > Integrations in the admin UI — ask the operator instead of building a "
        "replacement. A capability with no entry here has no bundled tool at all. "
        "agent_notes adds to a tool's description: how to use it correctly, including what to do "
        "when no action covers what you need; follow it. Empty means there is nothing to add."
    ),
    "input_schema": _NO_ARGUMENTS,
}

DESCRIBE_TOOL_TOOL: JSONObject = {
    "name": "describe_tool",
    "description": (
        "Return one bundled tool's callable actions with their full JSON input schemas. "
        "Call this after list_bundled_tools, for the tool you are about to use; the "
        "schemas are not in your context until you ask for them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_id": {
                "type": "string",
                "description": "Bundled tool id from list_bundled_tools, e.g. gmail.",
            },
        },
        "required": ["tool_id"],
        "additionalProperties": False,
    },
}

CALL_TOOL_TOOL: JSONObject = {
    "name": "call_tool",
    "description": (
        "Run one action of one bundled tool. Use the tool_id and action_id from "
        "list_bundled_tools and an input matching the schema from describe_tool. An "
        "approval-gated action returns a pending status with an approval_id instead of a "
        "result; poll check_tool_approval with that id and do not re-issue the action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_id": {"type": "string", "description": "Bundled tool id, e.g. gmail."},
            "action_id": {"type": "string", "description": "Action id from that tool."},
            "input": {"description": "Action input object matching its describe_tool schema."},
        },
        "required": ["tool_id", "action_id"],
        "additionalProperties": False,
    },
}

CHECK_APPROVAL_TOOL: JSONObject = {
    "name": "check_tool_approval",
    "description": (
        "Check the status of a tool action approval. Approval-gated actions return an "
        "approval_id and wait for the operator to decide in the Kern admin UI; poll this "
        "with that id to learn the outcome (pending, approved, denied, expired, executed, "
        "or failed)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"approval_id": {"type": "string"}},
        "required": ["approval_id"],
        "additionalProperties": False,
    },
}

# Served by the tools socket (host.runtime.tools.api).
TOOLS_SOCKET_TOOLS: tuple[JSONObject, ...] = (
    LIST_BUNDLED_TOOLS_TOOL,
    DESCRIBE_TOOL_TOOL,
    CALL_TOOL_TOOL,
    CHECK_APPROVAL_TOOL,
)

LIST_NETWORK_INTEGRATIONS_TOOL: JSONObject = {
    "name": "list_network_integrations",
    "description": (
        "List every network integration on this host with whether it is enabled and its "
        "policy options. All agent network traffic passes through exactly one integration, "
        "including operator-configured custom domains. If a destination is not covered, ask "
        "the operator to enable or configure its integration in the admin UI's Network tab."
    ),
    "input_schema": _NO_ARGUMENTS,
}

RECENT_NETWORK_DENIALS_TOOL: JSONObject = {
    "name": "recent_network_denials",
    "description": (
        "List this host's most recent denied network requests with each denial's code and "
        "guidance on what would change the outcome. Use this after an HTTP request, git push, "
        "or package install failed with a 403 or unclear client error."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many recent denials to return (default 20).",
            },
        },
        "additionalProperties": False,
    },
}

# Served by the agent-network socket (host.runtime.agent_network.api).
AGENT_NETWORK_TOOLS: tuple[JSONObject, ...] = (
    LIST_NETWORK_INTEGRATIONS_TOOL,
    RECENT_NETWORK_DENIALS_TOOL,
)

NETWORK_TOOL_NAMES = frozenset(tool["name"] for tool in AGENT_NETWORK_TOOLS)
