"""Operator-approved IndexNow notifications for public owned sites."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import ipaddress
import json
import re
import urllib.parse
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL, ParamGuardDenied
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    SetupStep,
    ToolManifest,
    protect_inputs,
)
from host.tools.results import ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalResult
from host.tools.shared.inputs import ToolInputValidationError, clip_text, decoded_url_component_values, guard_url_parameter_string, schema
from host.tools.shared.web import WebRequestError, known_provider_transport_error, request_bytes, unmapped_provider_error
from host.tools.tool import Tool


ENDPOINT = "https://api.indexnow.org/indexnow"
DOCS = "https://www.indexnow.org/documentation"
FAQ = "https://www.indexnow.org/faq"
TERMS = "https://www.indexnow.org/terms"
MAX_URLS = 100
MAX_URL_BYTES = 2_048
# Match the documented host approval JSON limit, including all binding metadata.
MAX_APPROVAL_BYTES = 64 * 1024
KEY_PATTERN = re.compile(r"[A-Za-z0-9-]{8,128}\Z")
# One kern-tools process serves concurrent handler threads on each host.
_BINDING_LOCK = threading.Lock()

SUBMIT_INPUT = schema(
    {
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_URLS,
            "description": "One to 100 distinct HTTPS URLs on one host that were added, materially changed or deleted; at most 2,048 UTF-8 bytes each. Complete compact UTF-8 approval JSON, including host and key-binding metadata, must fit 65,536 bytes. Full paths and queries must pass the parameter guard.",
        },
    },
    ["urls"],
)

MANIFEST = ToolManifest(
    tool_id="indexnow",
    display_name="IndexNow",
    description="Notify participating search engines about changed URLs on a domain with a deployed IndexNow key file.",
    connection="enable_only",
    actions=protect_inputs(
        (
            ActionSpec(
                id="submit_urls",
                description="Queue one to 100 changed URLs on one host for an approved IndexNow notification.",
                data_policy="Before queuing approval, Kern structurally validates and parameter-guards every exact URL, including decoded paths and query strings, locally. Kern sends nothing to IndexNow before approval. After approval, Kern sends the exact URLs, host and configured key to the fixed IndexNow endpoint. The endpoint may share notifications with participating search engines. Receipt does not guarantee crawling or indexing. IndexNow notifications are free, subject to provider rate limits; normal Kern runtime costs are separate.",
                input_schema=SUBMIT_INPUT,
                approval="operator",
            ),
        ),
        {},
    ),
    config=(ConfigRequirement("INDEXNOW_KEY", "An 8–128 character IndexNow key also deployed as a public key file on each submitted host."),),
    protections=(
        PARAM_GUARD_PROTECTION,
        "Every exact URL and every nested-decoding view of its path and query are guarded before approval and again before submission. Only the longer-text tier is allowed, under the stricter 2,048-byte URL bound; no identifier or machine-token exceptions apply.",
        "Every submitted URL must use HTTPS, a public named host, and the same exact hostname; credentials, IP addresses, fragments and duplicate URLs are rejected.",
        "The fixed global endpoint receives at most 100 URLs per approved batch. Kern never fetches a caller-supplied URL.",
        "The IndexNow key stays in write-only host configuration and is omitted from approval records and agent results. A key change invalidates a pending approval.",
    ),
    technical_details=(
        PARAM_GUARD_TECHNICAL_DETAIL,
        "Every exact URL and every nested-decoding view of its path and query are guarded before approval and again before submission. Only the longer-text tier is allowed, under the stricter 2,048-byte URL bound; no identifier or machine-token exceptions apply.",
        "The action accepts only urls, a flat array of 1–100 strings. Unknown fields and nested arrays/objects are rejected. Complete compact UTF-8 approval JSON, including the exact URL list, host and key-binding metadata, must fit the host's 65,536-byte bound; oversized batches fail before queuing and must be split. Approval records contain the complete exact batch, host and a host-secret HMAC key fingerprint; Home > Approvals > View exact request shows all paths and queries. Execution revalidates structure, guards and key binding. The configured key is never treated as caller input.",
        "IndexNow notifications are free and reach Bing and other participating engines, not Google. No key configuration, site deployment or URL submission happens until the operator sets up and uses the integration.",
        "IndexNow verifies host ownership by fetching https://<host>/<key>.txt, which must contain the exact key. Deploy that file on every new domain; no search engine dashboard registration is needed for this notification flow.",
        "Kern makes one redirect-free JSON POST to api.indexnow.org after approval. HTTP 200 means received; HTTP 202 means received with key validation pending. Neither is proof of indexing.",
    ),
    setup_steps=(
        SetupStep("Generate a key", "Create a random 8–128 character alphanumeric or hyphenated key. Keep a copy for your site deployments. This integration uses the same configured key across your domains.", DOCS, "IndexNow protocol"),
        SetupStep("Deploy the key file", "For each domain you want to notify, publish a UTF-8 text file at https://<domain>/<key>.txt whose only content is the key. Add it to each site's deployment template so new domains need no dashboard registration.", FAQ, "Key file setup"),
        SetupStep("Save the key and enable", "Open IndexNow under Home > Integrations, save the same key below, and enable the integration. Submit only changed URLs; every batch asks for operator approval.", show_config=True),
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard("What leaves this host", "After operator approval, the configured key, one public host and up to 100 changed public URLs go to IndexNow. The full exact approval batch can be sent to the configured host approval-assessment provider before a decision. Approval outcomes become available to the agent and its selected model provider."),
        DataSummaryCard("Where it can go", "Kern calls only the fixed IndexNow global endpoint. IndexNow may share valid URL notifications with participating search engines.", links=(DataSummaryLink("IndexNow documentation", DOCS),)),
        DataSummaryCard("What search engines can do with it", "Participating engines may recrawl submitted URLs and decide independently whether to index them. Notifications do not change Google indexing or guarantee ranking.", links=(DataSummaryLink("IndexNow FAQ", FAQ),)),
        DataSummaryCard("How long providers retain it", "Search engines control retention of received notifications and crawl records. IndexNow's published privacy terms cover its website and do not specify retention for submitted URLs. Disabling the integration stops future notifications but cannot retract submitted URLs.", links=(DataSummaryLink("IndexNow website privacy terms", TERMS),)),
    )),
    agent_notes="Use only for URLs added, materially updated or deleted on a host with its IndexNow key file already deployed. Group URLs by exact hostname. A 200 or 202 confirms receipt, not indexing; continue using Search Console for Google measurements. The key configured here must match the public key file on each host.",
)


def _key(api: HostAPI) -> str:
    value = api.config["INDEXNOW_KEY"]
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value):
        raise ToolInputValidationError("IndexNow key is invalid. Replace it in Home > Integrations.")
    return value


def _key_fingerprint(key: str, api: HostAPI, *, create: bool = False) -> str:
    # A private, persistent salt prevents the approval-risk provider from
    # recovering short configured keys by offline dictionary attack.
    with _BINDING_LOCK:
        private = api.secrets.load() or {}
        salt = private.get("fingerprint_salt")
        if salt is None and create:
            salt = secrets.token_hex(32)
            api.secrets.save({"fingerprint_salt": salt})
        if not isinstance(salt, str) or not re.fullmatch(r"[0-9a-f]{64}", salt):
            raise ToolInputValidationError("IndexNow key binding is unavailable. Submit a new request.")
        return hmac.new(bytes.fromhex(salt), key.encode(), hashlib.sha256).hexdigest()


def _urls(tool_input: JSONObject, api: HostAPI) -> tuple[str, list[str]]:
    if set(tool_input) != {"urls"}:
        raise ToolInputValidationError("IndexNow submission requires only urls.")
    raw = tool_input.get("urls")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_URLS:
        raise ToolInputValidationError("IndexNow requires one to 100 URLs.")
    urls: list[str] = []
    host = ""
    for value in raw:
        try:
            byte_length = len(value.encode("utf-8")) if isinstance(value, str) else 0
        except UnicodeEncodeError as exc:
            raise ToolInputValidationError("IndexNow URLs must be valid UTF-8 text.") from exc
        if not isinstance(value, str) or not value or byte_length > MAX_URL_BYTES or any(ch.isspace() for ch in value):
            raise ToolInputValidationError("IndexNow URLs must be bounded public HTTPS URLs.")
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ToolInputValidationError("IndexNow URLs must be public HTTPS URLs.") from exc
        hostname = parsed.hostname or ""
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ToolInputValidationError("IndexNow URLs must use public named hosts.")
        paths = decoded_url_component_values(parsed.path, plus=False)
        if (
            parsed.scheme != "https" or port not in {None, 443} or "." not in hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or hostname == "localhost" or hostname.endswith(".") or hostname.startswith(".")
            or ".." in hostname or not re.fullmatch(r"[a-z0-9.-]+", hostname)
            or any("\\" in path or any(part in {".", ".."} for part in path.split("/")) for path in paths)
        ):
            raise ToolInputValidationError("IndexNow URLs must be public HTTPS URLs without credentials, fragments or traversal.")
        if host and hostname != host:
            raise ToolInputValidationError("All IndexNow URLs in one batch must have the same hostname.")
        host = hostname
        guard_url_parameter_string(value, api, allow_longer_text=True)
        urls.append(value)
    if len(set(urls)) != len(urls):
        raise ToolInputValidationError("IndexNow URLs must be unique within a batch.")
    return host, urls


def _validate_approval_size(payload: JSONObject) -> None:
    if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_APPROVAL_BYTES:
        raise ToolInputValidationError("IndexNow approval JSON exceeds 65,536 UTF-8 bytes including host/key binding. Split the batch into smaller requests.")


def _failure(exc: WebRequestError) -> ActionFailed:
    if exc.status == 400:
        return ActionFailed("IndexNow rejected the URL batch format. Check the URLs and retry.")
    if exc.status in {403, 422}:
        return ActionFailed("IndexNow could not verify the key file or host for these URLs. Check the deployed key file and exact hostname.")
    if exc.status == 429:
        return ActionFailed("IndexNow rate-limited submissions. Wait before sending more changed URLs.")
    if exc.status in {500, 502, 503, 504}:
        return ActionFailed("IndexNow is temporarily unavailable. Retry later.")
    known = known_provider_transport_error(exc)
    if known:
        return ActionFailed(known)
    raise unmapped_provider_error("IndexNow", "submission", exc)


class IndexNowTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "submit_urls":
            return ActionFailed("Unsupported IndexNow action.")
        try:
            key = _key(api)
            host, urls = _urls(tool_input, api)
            payload: JSONObject = {
                "tool_id": MANIFEST.tool_id,
                "action": action,
                "host": host,
                "urls": cast(list[JSONValue], urls),
                "key_fingerprint": _key_fingerprint(key, api, create=True),
            }
            _validate_approval_size(payload)
            approval = api.approvals.request(
                action_id=action,
                summary=f"Notify IndexNow about {len(urls)} changed URL(s) on {clip_text(host, 200)}.",
                payload=payload,
            )
            return ActionPendingApproval(approval.approval_id, approval.summary)
        except (ToolInputValidationError, ParamGuardDenied, RuntimeError) as exc:
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            payload = approval.payload
            _validate_approval_size(payload)
            if payload.get("tool_id") != MANIFEST.tool_id or payload.get("action") != "submit_urls":
                return ActionFailed("IndexNow approval payload is invalid.")
            urls = payload.get("urls")
            if not isinstance(urls, list):
                return ActionFailed("IndexNow approval payload is invalid.")
            host, exact_urls = _urls({"urls": cast(list[JSONValue], urls)}, api)
            if payload.get("host") != host:
                return ActionFailed("IndexNow approval payload is invalid.")
            key = _key(api)
            if not isinstance(payload.get("key_fingerprint"), str) or not hmac.compare_digest(cast(str, payload["key_fingerprint"]), _key_fingerprint(key, api)):
                return ActionFailed("IndexNow key changed after approval was queued. Submit a new request.")
            body = json.dumps({"host": host, "key": key, "urlList": exact_urls}, separators=(",", ":")).encode()
            request_bytes(
                "POST", ENDPOINT,
                headers={"Content-Type": "application/json; charset=utf-8"},
                data=body,
                failure_message="IndexNow submission failed.",
                max_bytes=16_384,
            )
            return ApprovalExecuted(f"IndexNow received {len(exact_urls)} changed URL(s) on {clip_text(host, 200)}. Indexing is not guaranteed.")
        except WebRequestError as exc:
            return _failure(exc)
        except (ToolInputValidationError, ParamGuardDenied, RuntimeError) as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = IndexNowTool()
