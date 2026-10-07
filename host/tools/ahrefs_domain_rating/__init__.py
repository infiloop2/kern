"""Single-domain reads from Ahrefs' free Domain Rating endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import math
import re
import unicodedata

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL, ParamGuardDenied
from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink,
    SetupStep, ToolManifest, guarded_input, protect_inputs, validated_input,
)
from host.tools.results import ActionExecuted, ActionFailed, ActionResult
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError, schema
from host.tools.shared.web import (
    WebRequestError, encode_query, json_request, known_provider_transport_error,
    unmapped_provider_error,
)
from host.tools.tool import Tool

ENDPOINT = "https://api.ahrefs.com/v3/public/domain-rating-free"
LICENSE_URL = "https://ahrefs.com/legal/domain-rating-license"
ATTRIBUTION_TEXT = "Domain Rating by Ahrefs"
ATTRIBUTION_URL = "https://ahrefs.com/"
PRIVACY_URL = "https://ahrefs.com/legal/privacy-policy"
INVALID_RESPONSE = "Ahrefs returned an invalid Domain Rating response."
INVALID_DOMAIN = (
    "Ahrefs domain must be a bare public DNS hostname, at most 253 ASCII bytes after IDNA normalization. "
    "URLs, paths, credentials, ports, IP addresses and local names are not accepted."
)
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Match the host's public-media hostname exclusions, plus other explicitly
# local/special namespaces. This is offline syntax validation, not DNS lookup.
_LOCAL_TLDS = frozenset({
    "localhost", "local", "internal", "lan", "home", "invalid", "test",
    "localdomain", "corp", "private", "intranet", "onion", "arpa", "example", "alt",
})

MANIFEST = ToolManifest(
    tool_id="ahrefs_domain_rating",
    display_name="Ahrefs Domain Rating",
    description="Read the current Ahrefs Domain Rating for one public domain.",
    connection="enable_only",
    reports_cost=False,
    config=(ConfigRequirement(
        key="AHREFS_API_KEY",
        description="Dedicated Ahrefs API v3 key for the free Domain Rating endpoint.",
    ),),
    actions=protect_inputs((ActionSpec(
        id="get_domain_rating",
        description="Look up one domain's current backlink-profile strength on Ahrefs' 0–100 scale.",
        cost_description="One free Ahrefs Domain Rating request; no paid endpoint is used.",
        data_policy=(
            "Read-only, direct, with no per-call approval. Sends only the queried domain, dr3/dr4 version, "
            "fixed JSON output option and configured API key to Ahrefs' fixed free endpoint. "
            "The domain passes the strict host parameter guard. Returns the score and required attribution."
        ),
        input_schema=schema({
            "domain": {"type": "string", "description": "One bare public DNS hostname; case and lossless IDNs are normalized. Maximum 253 ASCII bytes after normalization. No URL, path, port, IP or local name."},
            "version": {"type": "string", "enum": ["dr3", "dr4"], "description": "Ahrefs Domain Rating version. Omit to use dr3 explicitly. dr3 and dr4 are not interchangeable historical measurements."},
        }, ["domain"]),
        output_schema=outputs.obj({
            "domain": outputs.text("The normalized lowercase ASCII hostname sent to Ahrefs."),
            "version": {"type": "string", "enum": ["dr3", "dr4"], "description": "The requested Domain Rating version."},
            "domain_rating": outputs.number("Finite current backlink-profile score from 0 to 100; zero is a real provider score."),
            "retrieved_at": outputs.text("UTC ISO 8601 retrieval time, not Ahrefs' index update time."),
            "attribution": outputs.obj({
                "text": {"type": "string", "enum": [ATTRIBUTION_TEXT], "description": "Display visibly adjacent to the score."},
                "url": {"type": "string", "enum": [ATTRIBUTION_URL], "description": "Link the attribution to Ahrefs."},
            }, ["text", "url"]),
            "license_url": {"type": "string", "enum": [LICENSE_URL], "description": "Canonical Ahrefs Domain Rating license terms."},
        }, ["domain", "version", "domain_rating", "retrieved_at", "attribution", "license_url"]),
    ),), {"get_domain_rating": {
        "domain": guarded_input(),
        "version": validated_input("Only dr3 or dr4; omitted version is dr3."),
    }}),
    data_summary=DataSummary(cards=(
        DataSummaryCard(
            title="What leaves this host",
            description="Only the queried public domain, selected version, fixed JSON output option and API-key authentication. The key remains in write-only host config and is never returned to the agent.",
        ),
        DataSummaryCard(
            title="Where it can go",
            description="One request to Ahrefs' fixed free Domain Rating API endpoint. Kern does not resolve or contact the queried website. Requests never follow redirects or fall back to a paid endpoint.",
        ),
        DataSummaryCard(
            title="What Ahrefs can do with it",
            description="Ahrefs receives the queried domain and can associate API activity with the key's account. Its privacy policy governs processing. Display the score with visible adjacent attribution, Domain Rating by Ahrefs, linked to https://ahrefs.com/. The license prohibits competitive repackaging and competitive bulk harvesting.",
            links=(DataSummaryLink("Ahrefs privacy policy", PRIVACY_URL), DataSummaryLink("Domain Rating license", LICENSE_URL)),
        ),
        DataSummaryCard(
            title="How long Ahrefs retains it",
            description="Ahrefs does not specify a fixed retention period for these API queries. Its privacy policy describes purpose-based retention, with longer retention for legitimate business or legal needs.",
            links=(DataSummaryLink("Ahrefs privacy policy", PRIVACY_URL),),
        ),
    )),
    protections=(
        "Single-domain reads run directly after configuration and enablement; they do not change third-party state.",
        "The configured API key is sent only to the fixed Ahrefs endpoint; redirect following is disabled.",
        "Domain Rating is a current backlink-profile proxy, not a Google score or a measurement of traffic or conversions. dr3 and dr4 are not interchangeable history.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        PARAM_GUARD_TECHNICAL_DETAIL,
        "Hostname validation is offline. IDNs must round-trip through Python's IDNA codec without changing their Unicode identity; lossy mappings are rejected. The original, decoded and ASCII domain forms are guarded with all exception flags false.",
        "One GET /v3/public/domain-rating-free with target, explicit version (default dr3) and output=json. Standard 30-second timeout and 8 MiB response cap; no retries. Missing, nonfinite or out-of-range scores fail instead of becoming zero. License links use the verified canonical constant.",
    ),
    setup_steps=(
        SetupStep("Create a free Ahrefs account", "Create or use a free Ahrefs account; this endpoint and API v3 keys are free.", link_url="https://ahrefs.com/", link_label="Open Ahrefs"),
        SetupStep("Generate a dedicated API v3 key", "As the workspace owner or admin, open Account settings > API keys and generate a dedicated key. Keys expire after one year; replace expired or revoked keys here.", link_url="https://docs.ahrefs.com/en/api/docs/api-keys-creation-and-management", link_label="Ahrefs API key instructions"),
        SetupStep("Configure and enable Ahrefs Domain Rating", "Under Home > Integrations, save AHREFS_API_KEY in the write-only configuration below and enable the integration. There is no OAuth step. Try one domain lookup and inspect Tool audit for its result.", show_config=True),
    ),
    agent_notes=(
        "Use get_domain_rating for one current public domain only. No batch, history, keyword or backlink-list action exists. "
        "Domain Rating measures relative backlink-profile strength, not Google's score, traffic or conversions. "
        "Keep the selected version: dr3 and dr4 are not interchangeable history. retrieved_at is retrieval time, not index freshness. "
        "Whenever displaying or publishing a score, place visible adjacent 'Domain Rating by Ahrefs' attribution linked to https://ahrefs.com/. "
        "Respect the returned license; do not repackage a competing data product or harvest competitively in bulk. "
        "Only the queried domain, version, fixed output format and authentication leave the host for the fixed Ahrefs endpoint. "
        "Do not work around parameter-guard denials or loop on rate limits."
    ),
)


def _normalized_domain(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value or len(value) > 1_024 or value != value.strip():
        raise ToolInputValidationError(INVALID_DOMAIN)
    try:
        name = value.encode("idna").decode("ascii").lower()
        decoded = name.encode("ascii").decode("idna")
        # IDNA2003's lossy mappings (e.g. sharp-s or joiner removal) must not
        # silently query a different domain. Validate ACE labels by round-trip.
        if decoded.encode("idna").decode("ascii").lower() != name:
            raise ValueError
        if not value.isascii() and unicodedata.normalize("NFC", value.lower()) != decoded:
            raise ValueError
    except (UnicodeError, ValueError):
        raise ToolInputValidationError(INVALID_DOMAIN) from None
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2
            or not all(_LABEL_RE.fullmatch(label) for label in labels)
            or labels[-1] in _LOCAL_TLDS
            or not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]+)", labels[-1])):
        raise ToolInputValidationError(INVALID_DOMAIN)
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name, decoded
    raise ToolInputValidationError(INVALID_DOMAIN)


class AhrefsDomainRatingTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "get_domain_rating":
            return ActionFailed("Unsupported Ahrefs Domain Rating action.")
        try:
            if set(tool_input) - {"domain", "version"}:
                raise ToolInputValidationError("Ahrefs get_domain_rating accepts only domain and version.")
            version = tool_input.get("version", "dr3")
            if version not in ("dr3", "dr4"):
                raise ToolInputValidationError("Ahrefs version must be dr3 or dr4.")
            domain, decoded = _normalized_domain(tool_input.get("domain"))
            # Guard before case normalization too: secret patterns can be case
            # sensitive. Check decoded ACE labels as well as the wire value.
            api.outbound.guard_request_parameter_string(str(tool_input["domain"]))
            api.outbound.guard_request_parameter_string(decoded)
            domain = api.outbound.guard_request_parameter_string(domain)
            api_key = api.config.get("AHREFS_API_KEY", "")
            if not api_key or not api_key.strip():
                return ActionFailed("AHREFS_API_KEY is not set. Set it under Home > Integrations, then enable Ahrefs Domain Rating.")
            response = json_request(
                "GET", ENDPOINT + "?" + encode_query({"target": domain, "version": str(version), "output": "json"}),
                headers={"Authorization": "Bearer " + api_key},
                failure_message="Ahrefs Domain Rating API request failed.",
                invalid_response_message=INVALID_RESPONSE,
            )
            record = response.get("domain_rating")
            score = record.get("domain_rating") if isinstance(record, dict) else None
            if (isinstance(score, bool) or not isinstance(score, (int, float))
                    or not 0 <= score <= 100 or not math.isfinite(score)):
                return ActionFailed(INVALID_RESPONSE)
            return ActionExecuted({
                "domain": domain, "version": version, "domain_rating": score,
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "attribution": {"text": ATTRIBUTION_TEXT, "url": ATTRIBUTION_URL},
                # Never trust a provider-supplied link, including license.
                "license_url": LICENSE_URL,
            })
        except (ToolInputValidationError, ParamGuardDenied) as exc:
            return ActionFailed(str(exc))
        except WebRequestError as exc:
            if exc.status in {401, 403}:
                return ActionFailed("Ahrefs rejected AHREFS_API_KEY. Replace the expired, revoked or invalid API v3 key under Home > Integrations.")
            if exc.status == 429:
                return ActionFailed("Ahrefs rate limit reached. Wait before another lookup; this call was not retried.")
            if exc.status:
                return ActionFailed(f"Ahrefs Domain Rating API returned HTTP {exc.status}.")
            known = known_provider_transport_error(exc)
            if known:
                return ActionFailed(known)
            raise unmapped_provider_error("Ahrefs", action, exc) from None
        except Exception as exc:
            # Malformed JSON has a fixed message from the shared parser; never
            # return arbitrary exception text or a raw provider error body.
            return ActionFailed(INVALID_RESPONSE if isinstance(exc, RuntimeError) and str(exc) == INVALID_RESPONSE else "Ahrefs Domain Rating request failed.")


BUNDLED_TOOL = AhrefsDomainRatingTool()
