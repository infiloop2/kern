"""xAI managed integration: pinned Grok inference and private S3 video output.

Storage credentials live separately in the encrypted admin database. Network
configuration remains enablement-only; images require no storage configuration.
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
        "Grok chat, X search and Imagine images under the pinned OAuth account. "
        "Videos require operator-configured private S3 storage; Kern supplies signed "
        "upload/download URLs without exposing AWS credentials to Grok. Hosted web "
        "search, remote MCP, code execution, external media URLs, and other developer "
        "API routes remain blocked."
    ),
    owned_apexes=("x.ai", "grok.com"),
    denial_reasons=(
        DenialReason("xai_video_response_invalid", "The video result could not be decoded or its storage URL could not be verified. Check that storage was not changed during generation and the upload URL has not expired; inspect the provider job error before starting another video."),
        DenialReason("xai_video_storage_required", "Configure Video storage under Home > Grok with a private S3 bucket, region and IAM access key pair. Images do not require this. Keep coding-data retention opted out."),
        DenialReason("xai_media_input_denied", "Use the supported Imagine request fields and inline media references. External media URLs cannot be fetched through xAI."),
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
            "This Grok server-side tool is not one of the narrowly approved xAI/X-hosted "
            "shapes: X search with X-only filters, text-to-image generation, or the bare "
            "reserved video-generation declaration. Hosted browsing, code interpreter, "
            "collections search, unknown tools, and media declarations that could name an "
            "external input remain denied. Remove or narrow the tool declaration.",
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
