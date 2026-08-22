"""xAI managed integration: static contract.

Opens the xAI OAuth path and a narrow allowlist of chat-proxy endpoints, pinned
to the configured account by the bearer token's account claim. Every Grok
server-side tool is denied — web search, X search, hosted browsing, code
execution, and remote MCP servers alike — and the integration exposes no
options.

The chat proxy also serves blob storage, remote session registration and
search, workspace sync, and cloud sandboxes. Those routes would move agent
files and conversation history off the host for reasons unrelated to inference,
so the integration allows only the endpoints a login plus inference needs.

Subscription inference runs against ``cli-chat-proxy.grok.com``. The metered
developer API (``api.x.ai``) is deliberately not opened: it bills per token
against a console credit balance rather than the operator's Grok subscription,
so allowing it would let a misconfigured runtime silently spend money.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from host.network_integrations.base import (
    DenialReason,
    IntegrationConfigError,
    IntegrationManifest,
    reject_extra,
)

MANIFEST = IntegrationManifest(
    integration_id="xai",
    display_name="xAI",
    description=(
        "Grok runtime access to the xAI CLI chat proxy under the pinned account, plus the "
        "xAI OAuth login path. The metered developer API is not opened, so inference draws "
        "on the operator's Grok subscription. Every Grok server-side tool is denied, web "
        "search included: Grok's searches and browses live pages as one capability, from "
        "xAI's infrastructure rather than this host, so it has no shape this host can "
        "safely allow. The integration has no options."
    ),
    owned_apexes=("x.ai", "grok.com"),
    denial_reasons=(
        DenialReason(
            "xai_account_unavailable",
            "The pinned xAI account identity is not available yet (the Grok login has not "
            "completed on this host), so chat proxy requests fail closed. Complete the Grok "
            "login or ask the operator to check the agent provider status.",
        ),
        DenialReason(
            "xai_token_account_mismatch",
            "The request did not carry exactly one Bearer token whose claims identify the "
            "configured xAI account.",
        ),
        DenialReason(
            "xai_body_undecodable",
            "The request body's Content-Encoding could not be decoded for inspection, so the "
            "request failed closed. Send the request uncompressed.",
        ),
        DenialReason(
            "xai_body_not_json",
            "The request body looked like JSON but did not parse, or was nested too deeply to "
            "inspect, so it could not be checked for server-tool declarations. Send valid JSON "
            "without deeply nested structures.",
        ),
        DenialReason(
            "xai_web_search_denied",
            "Grok server-side web search is not available on this host and there is no "
            "option to enable it. It searches and browses live pages as one capability, "
            "fetching model-chosen URLs -- with arbitrary chosen data in their parameters -- "
            "from xAI's infrastructure rather than through this host's network policy. Use "
            "the agent's own tools for anything on the web.",
        ),
        DenialReason(
            "xai_server_tool_denied",
            "Grok server-side tools that reach external sources or run code off-box (X search, "
            "hosted browsing, code interpreter, collections search, media generation) are always "
            "denied on this host, and no operator setting enables any of them. Remove the tool "
            "declaration.",
        ),
        DenialReason(
            "xai_remote_mcp_denied",
            "Remote MCP servers make xAI call an external server with request data and are "
            "always denied on this host. Remove the remote MCP tool declaration.",
        ),
    ),
)


@dataclass(frozen=True)
class XaiIntegration:
    """When enabled, the Grok runtime reaches the xAI CLI chat proxy under the
    pinned account. There are no options: every Grok server-side tool — web
    search included — is denied, so enablement is the whole configuration.

    Web search is not offered because Grok's has no shape narrow enough to
    offer. It searches and browses live pages in one indivisible capability,
    the page fetch happens on xAI's infrastructure rather than here, and the
    URL fetched is the model's choice — so allowing it would open an egress
    path this host's network policy cannot see, and there is no cache-backed
    mode (as OpenAI has) to allow instead."""

    enabled: bool

    def to_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


def parse(raw: dict[str, Any]) -> XaiIntegration:
    if not raw:
        return XaiIntegration(False)
    context = "network_integrations.xai"
    reject_extra(raw, {"enabled"}, context)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise IntegrationConfigError(f"{context}.enabled must be true or false")
    return XaiIntegration(enabled=enabled)
