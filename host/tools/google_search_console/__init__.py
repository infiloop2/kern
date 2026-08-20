"""Google Search Console bundled tool package.

The provider surface is deliberately narrower than the underlying API:
properties, analytics, sitemaps, and indexed-URL inspection.  Property
creation/deletion is omitted, sitemap submission is approval-gated, and every
property-scoped call first resolves the requested property against the
connected account's live Search Console property list.
"""

from __future__ import annotations

from datetime import date
import re
import urllib.parse
from typing import Mapping, cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    DataSummaryPoint,
    SetupStep,
    ToolManifest,
)
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalResult,
)
from host.tools.shared.google import (
    GoogleCredentialStore,
    IntegrationReconnectRequired,
    google_json_request,
    google_oauth_setup_steps,
)
from host.tools.shared.inputs import (
    ToolInputValidationError,
    clip_text,
    guard_url_parameter_string,
    int_field,
    schema,
)
from host.tools.shared.web import UnmappedProviderError
from host.tools.tool import CredentialFlow


SEARCH_CONSOLE_API_BASE_URL = "https://www.googleapis.com/webmasters/v3"
URL_INSPECTION_ENDPOINT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
GOOGLE_OAUTH_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/webmasters",
)
REQUIRED_SEARCH_CONSOLE_SCOPES = frozenset(
    {"https://www.googleapis.com/auth/webmasters"}
)
SEARCH_CONSOLE_RECONNECT_MESSAGE = (
    "Google Search Console is no longer connected. Please reconnect Search Console."
)
MAX_PROPERTIES = 250
MAX_ANALYTICS_ROWS = 100
MAX_ANALYTICS_DIMENSIONS = 3
MAX_SITEMAPS = 100
MAX_INSPECTION_ITEMS = 20
MAX_URL_BYTES = 2_048
MAX_INT64 = 9_223_372_036_854_775_807
ANALYTICS_DIMENSIONS = frozenset(
    {"country", "date", "device", "hour", "page", "query", "searchAppearance"}
)
SEARCH_TYPES = frozenset({"discover", "googleNews", "image", "news", "video", "web"})
AGGREGATION_TYPES = frozenset({"auto", "byPage", "byProperty"})
DATA_STATES = frozenset({"all", "final", "hourly_all"})
READ_PERMISSION_LEVELS = frozenset(
    {"siteOwner", "siteFullUser", "siteRestrictedUser"}
)
SITEMAP_PERMISSION_LEVELS = frozenset({"siteOwner", "siteFullUser"})
LANGUAGE_CODE_RE = re.compile(
    r"^[A-Za-z]{2,3}(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?$"
)
GRANDFATHERED_LANGUAGE_CODES = frozenset(
    {
        "art-lojban",
        "cel-gaulish",
        "en-gb-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "no-bok",
        "no-nyn",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    }
)

RESULT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="google_search_console",
    display_name="Google Search Console",
    description=(
        "Connect your Google account so your agent can read Search Console properties, search analytics, "
        "sitemaps, and indexed-URL status, and submit a sitemap with your approval."
    ),
    connection="oauth",
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(
                        label="Reads",
                        text=(
                            "The selected property, typed date and grouping options, or one guarded URL goes to Google. "
                            "OAuth tokens authenticate requests but never reach the agent."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Sitemap submission",
                        text=(
                            "Only the exact property and sitemap URL shown in an approval are sent when you approve it."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                description=(
                    "Requests go only to Google's fixed Search Console and URL Inspection API endpoints. Each scoped "
                    "request is limited to a property currently present in the connected Google account."
                ),
            ),
            DataSummaryCard(
                title="What Google can do with it",
                description=(
                    "Google handles the request and Search Console account data under the Google Privacy Policy. "
                    "URL Inspection reports the version in Google's index; it does not fetch or test a live page."
                ),
                links=(
                    DataSummaryLink(label="Google Privacy Policy", url="https://policies.google.com/privacy"),
                    DataSummaryLink(
                        label="Search Console API reference",
                        url="https://developers.google.com/webmaster-tools/v1/api_reference_index",
                    ),
                ),
            ),
            DataSummaryCard(
                title="How long Google retains it",
                description=(
                    "Search Console data and submitted sitemaps remain governed by your Google account and Google's "
                    "retention practices. Disconnecting removes Kern's local OAuth tokens but does not delete Google data."
                ),
                links=(
                    DataSummaryLink(
                        label="Google data retention policy",
                        url="https://policies.google.com/technologies/retention",
                    ),
                ),
            ),
        ),
    ),
    actions=(
        ActionSpec(
            id="list_properties",
            description="List properties available to the connected Search Console account.",
            data_policy=(
                "Reads the connected account's Search Console property URLs and permission levels from Google. "
                "No agent-supplied data leaves the host. Runs directly with no approval."
            ),
            input_schema=schema({}),
            output_schema=RESULT_SCHEMA,
        ),
        ActionSpec(
            id="query_search_analytics",
            description=(
                "Query bounded clicks, impressions, CTR, and average position for one listed property and date range."
            ),
            data_policy=(
                "Sends one property already listed by the connected account plus typed dates, dimensions, result type, "
                "aggregation, freshness, and pagination values to Google. Runs directly with no approval."
            ),
            input_schema=schema(
                {
                    "site_url": {
                        "type": "string",
                        "description": "Exact property URL returned by list_properties, including sc-domain: or trailing slash.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Inclusive start date in YYYY-MM-DD form (Search Console uses Pacific Time).",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Inclusive end date in YYYY-MM-DD form, on or after start_date.",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": cast(list[JSONValue], sorted(ANALYTICS_DIMENSIONS)),
                        },
                        "maxItems": MAX_ANALYTICS_DIMENSIONS,
                        "description": "Optional grouping dimensions, in result key order; at most three unique values.",
                    },
                    "search_type": {
                        "type": "string",
                        "enum": cast(list[JSONValue], sorted(SEARCH_TYPES)),
                        "description": "Search result type; defaults to web.",
                    },
                    "aggregation_type": {
                        "type": "string",
                        "enum": cast(list[JSONValue], sorted(AGGREGATION_TYPES)),
                        "description": "Aggregation mode; defaults to auto.",
                    },
                    "data_state": {
                        "type": "string",
                        "enum": cast(list[JSONValue], sorted(DATA_STATES)),
                        "description": "final for settled data, all for fresh partial data, or hourly_all with the hour dimension.",
                    },
                    "row_limit": {
                        "type": "string",
                        "description": f"Maximum rows to return, from 1 to {MAX_ANALYTICS_ROWS}; defaults to 25.",
                    },
                    "start_row": {
                        "type": "string",
                        "description": "Zero-based result offset from 0 to 25000; defaults to 0.",
                    },
                },
                ["site_url", "start_date", "end_date"],
            ),
            output_schema=RESULT_SCHEMA,
        ),
        ActionSpec(
            id="list_sitemaps",
            description="List bounded sitemap status records for one listed property.",
            data_policy=(
                "Sends only one exact property already listed by the connected account to Google. "
                "Runs directly with no approval."
            ),
            input_schema=schema(
                {
                    "site_url": {
                        "type": "string",
                        "description": "Exact property URL returned by list_properties.",
                    }
                },
                ["site_url"],
            ),
            output_schema=RESULT_SCHEMA,
        ),
        ActionSpec(
            id="inspect_url",
            description=(
                "Inspect Google's indexed version of one URL under a listed property; this is not a live test or indexing request."
            ),
            data_policy=(
                "Sends one exact property and one guarded URL structurally confined to that property to Google's fixed "
                "URL Inspection endpoint. Runs directly with no approval."
            ),
            input_schema=schema(
                {
                    "site_url": {
                        "type": "string",
                        "description": "Exact property URL returned by list_properties.",
                    },
                    "inspection_url": {
                        "type": "string",
                        "description": "Fully qualified HTTP(S) page URL under site_url to inspect in Google's index.",
                    },
                    "language_code": {
                        "type": "string",
                        "description": (
                            "Optional issue-message language such as en-US: a two- or three-letter "
                            "language with optional script and region; defaults to en-US."
                        ),
                    },
                },
                ["site_url", "inspection_url"],
            ),
            output_schema=RESULT_SCHEMA,
        ),
        ActionSpec(
            id="submit_sitemap",
            description="Queue approval to submit one sitemap URL for a listed Search Console property.",
            data_policy=(
                "Validates the property and sitemap locally and against the connected account, then holds the exact "
                "property and sitemap URL. Nothing is submitted to Google until the operator approves it."
            ),
            input_schema=schema(
                {
                    "site_url": {
                        "type": "string",
                        "description": "Exact property URL returned by list_properties.",
                    },
                    "sitemap_url": {
                        "type": "string",
                        "description": "Fully qualified HTTP(S) sitemap URL under site_url to submit.",
                    },
                },
                ["site_url", "sitemap_url"],
            ),
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(
            key="GOOGLE_OAUTH_CLIENT_ID",
            description="Google OAuth client id for the hosting deployment.",
        ),
        ConfigRequirement(
            key="GOOGLE_OAUTH_CLIENT_SECRET",
            description="Google OAuth client secret for the hosting deployment.",
        ),
    ),
    protections=(
        "OAuth tokens stay in the host credential store and are never exposed to the agent.",
        "Every property-scoped action rechecks the requested property against the connected account's live property list.",
        "Property creation and deletion are not exposed, and sitemap submission waits for explicit operator approval.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        "Property URLs are percent-encoded only after an exact match against Google's live sites.list response. URL-prefix and sc-domain ownership rules are enforced locally before inspection or sitemap proposal.",
        PARAM_GUARD_TECHNICAL_DETAIL,
    ),
    setup_steps=google_oauth_setup_steps(
        project_step_description=(
            "Open Google Cloud Console, choose the project picker, and create a dedicated project if needed. "
            "You can reuse the project and Web OAuth client configured for Gmail or Google Calendar."
        ),
        enable_api_step=SetupStep(
            title="Enable the Search Console API",
            description=(
                "Open APIs and Services > Library, search for Google Search Console API, open it, and choose Enable."
            ),
            link_url="https://console.cloud.google.com/apis/library/searchconsole.googleapis.com",
            link_label="Open the Search Console API library page",
        ),
        scopes_step=SetupStep(
            title="Declare Search Console permissions",
            description=(
                "Under Google Auth Platform > Data Access, add openid, email, and "
                "https://www.googleapis.com/auth/webmasters. The full Search Console scope is needed because the "
                "tool can submit a sitemap; all other writes remain absent and sitemap submission is approval-gated."
            ),
            link_url="https://developers.google.com/webmaster-tools/v1/how-tos/authorizing",
            link_label="Review Search Console authorization",
        ),
        connect_step_description=(
            "Open Google Search Console under Home > Integrations, save the Web application client ID and secret under "
            "the two configuration keys below, enable the tool, and choose Connect. Approve the displayed Google "
            "permissions and confirm the expected email. Run list_properties first; submit_sitemap should create an "
            "approval rather than changing Google immediately."
        ),
        include_images=False,
    ),
    agent_notes=(
        "Use list_properties before property-scoped actions and pass its site_url back exactly. inspect_url only reports "
        "Google's indexed version and cannot request indexing or test a live page. Use submit_sitemap for Google discovery."
    ),
)


SEARCH_CONSOLE_CREDENTIALS = GoogleCredentialStore(
    tool_id=MANIFEST.tool_id,
    scopes=GOOGLE_OAUTH_SCOPES,
    required_scopes=REQUIRED_SEARCH_CONSOLE_SCOPES,
    reconnect_message=SEARCH_CONSOLE_RECONNECT_MESSAGE,
)


def _text(value: object, *, limit: int = 500) -> str:
    return clip_text(value.strip(), limit) if isinstance(value, str) else ""


def _url_text(value: object) -> str:
    return _text(value, limit=MAX_URL_BYTES)


def _nonnegative_int64(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise RuntimeError("Search Console returned an invalid sitemap count.")
    if not 0 <= parsed <= MAX_INT64:
        raise RuntimeError("Search Console returned an invalid sitemap count.")
    return parsed


def _safe_url_path(path: str) -> bool:
    """Reject path forms whose effective segments depend on decoding."""
    lower = path.lower()
    if "\\" in path or any(encoded in lower for encoded in ("%2f", "%5c", "%25")):
        return False
    decoded = urllib.parse.unquote(path)
    return "\\" not in decoded and not any(
        segment in {".", ".."} for segment in decoded.split("/")
    )


def _property_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > MAX_URL_BYTES:
        return ""
    if candidate.startswith("sc-domain:"):
        domain = candidate.removeprefix("sc-domain:")
        return candidate if domain and "." in domain and "/" not in domain else ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.query
        or parsed.fragment
        or not _safe_url_path(parsed.path)
    ):
        return ""
    return candidate


def _parse_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ToolInputValidationError(
            f"Search Console tool_input.{field} must be a YYYY-MM-DD date."
        )
    candidate = value.strip()
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise ToolInputValidationError(
            f"Search Console tool_input.{field} must be a YYYY-MM-DD date."
        ) from exc
    return parsed.isoformat()


def _language_code(value: object) -> str:
    if not isinstance(value, str):
        raise ToolInputValidationError(
            "Search Console tool_input.language_code must be a supported language tag."
        )
    candidate = value.strip()
    if len(candidate) > 35:
        raise ToolInputValidationError(
            "Search Console tool_input.language_code must be a supported language tag."
        )
    if candidate.lower() in GRANDFATHERED_LANGUAGE_CODES:
        return candidate
    if LANGUAGE_CODE_RE.fullmatch(candidate) is None:
        raise ToolInputValidationError(
            "Search Console tool_input.language_code must be a supported language tag."
        )
    return candidate


def _enum_value(
    tool_input: JSONObject, field: str, allowed: frozenset[str], *, default: str
) -> str:
    value = tool_input.get(field)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(
            f"Search Console tool_input.{field} must be one of: {', '.join(sorted(allowed))}."
        )
    return value


def _properties(
    access_token: str, *, max_results: int | None = MAX_PROPERTIES
) -> list[JSONObject]:
    response = google_json_request(
        "GET",
        f"{SEARCH_CONSOLE_API_BASE_URL}/sites",
        access_token,
        failure_message="Search Console property listing failed.",
        invalid_response_message="Search Console returned an invalid property listing.",
    )
    entries = response.get("siteEntry")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise RuntimeError("Search Console returned an invalid property listing.")
    output: list[JSONObject] = []
    for value in entries:
        if not isinstance(value, dict):
            continue
        site_url = _property_url(value.get("siteUrl"))
        permission = _text(value.get("permissionLevel"), limit=80)
        if not site_url or permission not in READ_PERMISSION_LEVELS:
            continue
        output.append({"site_url": site_url, "permission_level": permission})
        if max_results is not None and len(output) >= max_results:
            break
    return output


def _property(access_token: str, requested: object) -> JSONObject:
    if not isinstance(requested, str) or not requested.strip():
        raise ToolInputValidationError(
            "Search Console tool_input.site_url must be an exact property URL returned by list_properties."
        )
    candidate = requested.strip()
    for item in _properties(access_token, max_results=None):
        if item.get("site_url") == candidate:
            return item
    raise ToolInputValidationError(
        "Search Console site_url is not available to the connected account. Run list_properties and use an exact site_url."
    )


def _sitemap_property(access_token: str, requested: object) -> JSONObject:
    item = _property(access_token, requested)
    if item.get("permission_level") not in SITEMAP_PERMISSION_LEVELS:
        raise ToolInputValidationError(
            "Search Console sitemap submission requires owner or full-user permission for the selected property."
        )
    return item


def _parsed_page_url(value: object, field: str) -> tuple[str, urllib.parse.SplitResult]:
    message = f"Search Console tool_input.{field} must be a fully qualified HTTP(S) URL."
    if not isinstance(value, str):
        raise ToolInputValidationError(message)
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > MAX_URL_BYTES:
        raise ToolInputValidationError(message)
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ToolInputValidationError(message) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.fragment
        or not _safe_url_path(parsed.path)
    ):
        raise ToolInputValidationError(message)
    return candidate, parsed


def _url_under_property(value: object, site_url: str, field: str) -> str:
    candidate, parsed = _parsed_page_url(value, field)
    if site_url.startswith("sc-domain:"):
        domain = site_url.removeprefix("sc-domain:").lower().rstrip(".")
        hostname = (parsed.hostname or "").lower().rstrip(".")
        belongs = bool(domain) and (hostname == domain or hostname.endswith(f".{domain}"))
    else:
        site = urllib.parse.urlsplit(site_url)
        candidate_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        site_port = site.port or (443 if site.scheme == "https" else 80)
        candidate_path = parsed.path or "/"
        site_path = site.path or "/"
        belongs = (
            parsed.scheme == site.scheme
            and (parsed.hostname or "").lower().rstrip(".")
            == (site.hostname or "").lower().rstrip(".")
            and candidate_port == site_port
            and candidate_path.startswith(site_path)
        )
    if not belongs:
        raise ToolInputValidationError(
            f"Search Console tool_input.{field} must be under the selected site_url property."
        )
    return candidate


def _site_path(site_url: str) -> str:
    return urllib.parse.quote(site_url, safe="")


def _analytics_input(tool_input: JSONObject, access_token: str) -> tuple[str, JSONObject]:
    site_url = cast(str, _property(access_token, tool_input.get("site_url"))["site_url"])
    start_date = _parse_date(tool_input.get("start_date"), "start_date")
    end_date = _parse_date(tool_input.get("end_date"), "end_date")
    if start_date > end_date:
        raise ToolInputValidationError(
            "Search Console tool_input.end_date must be on or after start_date."
        )
    dimensions_value = tool_input.get("dimensions", [])
    if not isinstance(dimensions_value, list):
        raise ToolInputValidationError(
            "Search Console tool_input.dimensions must be an array."
        )
    dimensions: list[JSONValue] = []
    for value in dimensions_value:
        if not isinstance(value, str) or value not in ANALYTICS_DIMENSIONS:
            raise ToolInputValidationError(
                "Search Console dimensions contain an unsupported value."
            )
        if value in dimensions:
            raise ToolInputValidationError("Search Console dimensions must be unique.")
        dimensions.append(value)
    if len(dimensions) > MAX_ANALYTICS_DIMENSIONS:
        raise ToolInputValidationError(
            f"Search Console dimensions support at most {MAX_ANALYTICS_DIMENSIONS} values."
        )
    data_state = _enum_value(tool_input, "data_state", DATA_STATES, default="final")
    if data_state == "hourly_all" and "hour" not in dimensions:
        raise ToolInputValidationError(
            "Search Console data_state hourly_all requires the hour dimension."
        )
    if "hour" in dimensions and data_state != "hourly_all":
        raise ToolInputValidationError(
            "Search Console hour dimension requires data_state hourly_all."
        )
    search_type = _enum_value(tool_input, "search_type", SEARCH_TYPES, default="web")
    aggregation_type = _enum_value(
        tool_input, "aggregation_type", AGGREGATION_TYPES, default="auto"
    )
    if aggregation_type == "byProperty" and "page" in dimensions:
        raise ToolInputValidationError(
            "Search Console aggregation_type byProperty cannot be used with the page dimension."
        )
    if search_type in {"discover", "googleNews"} and "query" in dimensions:
        raise ToolInputValidationError(
            "Search Console discover and googleNews search types cannot use the query dimension."
        )
    if aggregation_type == "byProperty" and search_type in {"discover", "googleNews"}:
        raise ToolInputValidationError(
            "Search Console aggregation_type byProperty is not supported for "
            "discover or googleNews search types."
        )
    body: JSONObject = {
        "startDate": start_date,
        "endDate": end_date,
        "type": search_type,
        "aggregationType": aggregation_type,
        "dataState": data_state,
        "rowLimit": int_field(
            tool_input,
            "row_limit",
            provider="Search Console",
            default=25,
            low=1,
            high=MAX_ANALYTICS_ROWS,
        ),
        "startRow": int_field(
            tool_input,
            "start_row",
            provider="Search Console",
            default=0,
            low=0,
            high=25_000,
        ),
    }
    if dimensions:
        body["dimensions"] = dimensions
    return site_url, body


def _analytics_rows(response: JSONObject) -> list[JSONObject]:
    rows = response.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise RuntimeError("Search Console returned invalid analytics rows.")
    output: list[JSONObject] = []
    for value in rows[:MAX_ANALYTICS_ROWS]:
        if not isinstance(value, dict):
            continue
        raw_keys = value.get("keys")
        keys: list[JSONValue] = []
        if isinstance(raw_keys, list):
            keys = [_text(item, limit=240) for item in raw_keys[:MAX_ANALYTICS_DIMENSIONS]]
        row: JSONObject = {"keys": keys}
        for key in ("clicks", "impressions", "ctr", "position"):
            number = value.get(key)
            if isinstance(number, (int, float)) and not isinstance(number, bool):
                row[key] = number
        output.append(row)
    return output


def _sitemaps(access_token: str, site_url: str) -> list[JSONObject]:
    response = google_json_request(
        "GET",
        f"{SEARCH_CONSOLE_API_BASE_URL}/sites/{_site_path(site_url)}/sitemaps",
        access_token,
        failure_message="Search Console sitemap listing failed.",
        invalid_response_message="Search Console returned an invalid sitemap listing.",
    )
    values = response.get("sitemap")
    if values is None:
        return []
    if not isinstance(values, list):
        raise RuntimeError("Search Console returned an invalid sitemap listing.")
    output: list[JSONObject] = []
    for value in values[:MAX_SITEMAPS]:
        if not isinstance(value, dict):
            continue
        item: JSONObject = {
            "path": _url_text(value.get("path")),
            "type": _text(value.get("type"), limit=80),
            "last_submitted": _text(value.get("lastSubmitted"), limit=80),
            "is_pending": bool(value.get("isPending") is True),
            "is_sitemaps_index": bool(value.get("isSitemapsIndex") is True),
            "last_downloaded": _text(value.get("lastDownloaded"), limit=80),
            "warnings": _nonnegative_int64(value.get("warnings", "0")),
            "errors": _nonnegative_int64(value.get("errors", "0")),
        }
        contents = value.get("contents")
        normalized_contents: list[JSONObject] = []
        if isinstance(contents, list):
            for content in contents[:10]:
                if not isinstance(content, dict):
                    continue
                normalized_contents.append(
                    {
                        "type": _text(content.get("type"), limit=80),
                        "submitted": _nonnegative_int64(content.get("submitted", "0")),
                        "indexed": _nonnegative_int64(content.get("indexed", "0")),
                    }
                )
        item["contents"] = cast(list[JSONValue], normalized_contents)
        output.append(item)
    return output


def _issue_list(value: object, *, message_key: str = "message") -> list[JSONObject]:
    if not isinstance(value, list):
        return []
    output: list[JSONObject] = []
    for issue in value[:MAX_INSPECTION_ITEMS]:
        if not isinstance(issue, dict):
            continue
        output.append(
            {
                "type": _text(issue.get("issueType"), limit=120),
                "severity": _text(issue.get("severity"), limit=40),
                "message": _text(issue.get(message_key), limit=500),
            }
        )
    return output


def _inspection_result(response: JSONObject, inspection_url: str) -> JSONObject:
    raw = response.get("inspectionResult")
    if not isinstance(raw, dict):
        raise RuntimeError("Search Console returned an invalid URL inspection result.")
    index = raw.get("indexStatusResult")
    index = index if isinstance(index, dict) else {}
    normalized_index: JSONObject = {}
    field_map = {
        "verdict": "verdict",
        "coverageState": "coverage_state",
        "robotsTxtState": "robots_txt_state",
        "indexingState": "indexing_state",
        "lastCrawlTime": "last_crawl_time",
        "pageFetchState": "page_fetch_state",
        "googleCanonical": "google_canonical",
        "userCanonical": "user_canonical",
        "crawledAs": "crawled_as",
    }
    for provider_key, output_key in field_map.items():
        normalized_index[output_key] = _text(index.get(provider_key), limit=MAX_URL_BYTES)
    for provider_key, output_key in (("sitemap", "sitemaps"), ("referringUrls", "referring_urls")):
        values = index.get(provider_key)
        normalized_index[output_key] = cast(
            list[JSONValue],
            [_url_text(item) for item in values[:MAX_INSPECTION_ITEMS]]
            if isinstance(values, list)
            else [],
        )
    mobile = raw.get("mobileUsabilityResult")
    mobile = mobile if isinstance(mobile, dict) else {}
    rich = raw.get("richResultsResult")
    rich = rich if isinstance(rich, dict) else {}
    detected: list[JSONObject] = []
    raw_detected = rich.get("detectedItems")
    if isinstance(raw_detected, list):
        for group in raw_detected[:10]:
            if not isinstance(group, dict):
                continue
            items: list[JSONObject] = []
            raw_items = group.get("items")
            if isinstance(raw_items, list):
                for item in raw_items[:10]:
                    if not isinstance(item, dict):
                        continue
                    items.append(
                        {
                            "name": _text(item.get("name"), limit=300),
                            "issues": cast(
                                list[JSONValue],
                                _issue_list(item.get("issues"), message_key="issueMessage"),
                            ),
                        }
                    )
            detected.append(
                {
                    "type": _text(group.get("richResultType"), limit=120),
                    "items": cast(list[JSONValue], items),
                }
            )
    return {
        "inspection_url": inspection_url,
        "result_link": _url_text(raw.get("inspectionResultLink")),
        "index_status": normalized_index,
        "mobile_usability": {
            "verdict": _text(mobile.get("verdict"), limit=40),
            "issues": cast(list[JSONValue], _issue_list(mobile.get("issues"))),
        },
        "rich_results": {
            "verdict": _text(rich.get("verdict"), limit=40),
            "detected_items": cast(list[JSONValue], detected),
        },
    }


def _submit_sitemap(access_token: str, site_url: str, sitemap_url: str) -> None:
    google_json_request(
        "PUT",
        f"{SEARCH_CONSOLE_API_BASE_URL}/sites/{_site_path(site_url)}/sitemaps/{urllib.parse.quote(sitemap_url, safe='')}",
        access_token,
        failure_message="Search Console sitemap submission failed.",
        invalid_response_message="Search Console returned an invalid sitemap submission response.",
    )


class GoogleSearchConsoleTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return SEARCH_CONSOLE_CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            access_token = SEARCH_CONSOLE_CREDENTIALS.access_token(api)
            if action == "list_properties":
                properties = _properties(access_token)
                return ActionExecuted(
                    {
                        "status": "success_executed",
                        "message": f"Loaded {len(properties)} Search Console propert(ies).",
                        "properties": cast(list[JSONValue], properties),
                    }
                )
            if action == "query_search_analytics":
                site_url, body = _analytics_input(tool_input, access_token)
                response = google_json_request(
                    "POST",
                    f"{SEARCH_CONSOLE_API_BASE_URL}/sites/{_site_path(site_url)}/searchAnalytics/query",
                    access_token,
                    body=body,
                    failure_message="Search Console analytics query failed.",
                    invalid_response_message="Search Console returned an invalid analytics response.",
                )
                rows = _analytics_rows(response)
                result: JSONObject = {
                    "status": "success_executed",
                    "message": f"Loaded {len(rows)} bounded Search Console analytics row(s).",
                    "site_url": site_url,
                    "rows": cast(list[JSONValue], rows),
                    "response_aggregation_type": _text(
                        response.get("responseAggregationType"), limit=80
                    ),
                }
                metadata = response.get("metadata")
                if isinstance(metadata, dict):
                    result["metadata"] = {
                        "first_incomplete_date": _text(
                            metadata.get("first_incomplete_date"), limit=20
                        ),
                        "first_incomplete_hour": _text(
                            metadata.get("first_incomplete_hour"), limit=80
                        ),
                    }
                return ActionExecuted(result)
            if action == "list_sitemaps":
                site_url = cast(
                    str, _property(access_token, tool_input.get("site_url"))["site_url"]
                )
                sitemaps = _sitemaps(access_token, site_url)
                return ActionExecuted(
                    {
                        "status": "success_executed",
                        "message": f"Loaded {len(sitemaps)} bounded sitemap record(s).",
                        "site_url": site_url,
                        "sitemaps": cast(list[JSONValue], sitemaps),
                    }
                )
            if action == "inspect_url":
                site_url = cast(
                    str, _property(access_token, tool_input.get("site_url"))["site_url"]
                )
                inspection_url = _url_under_property(
                    tool_input.get("inspection_url"), site_url, "inspection_url"
                )
                inspection_url = guard_url_parameter_string(inspection_url, api)
                language = _language_code(tool_input.get("language_code", "en-US"))
                response = google_json_request(
                    "POST",
                    URL_INSPECTION_ENDPOINT,
                    access_token,
                    body={
                        "inspectionUrl": inspection_url,
                        "siteUrl": site_url,
                        "languageCode": language,
                    },
                    failure_message="Search Console URL inspection failed.",
                    invalid_response_message="Search Console returned an invalid URL inspection response.",
                )
                return ActionExecuted(
                    {
                        "status": "success_executed",
                        "message": "Loaded Google's indexed URL inspection result.",
                        "inspection": _inspection_result(response, inspection_url),
                    }
                )
            if action == "submit_sitemap":
                site_url = cast(
                    str,
                    _sitemap_property(access_token, tool_input.get("site_url"))["site_url"],
                )
                sitemap_url = _url_under_property(
                    tool_input.get("sitemap_url"), site_url, "sitemap_url"
                )
                account = SEARCH_CONSOLE_CREDENTIALS.refresh_identity(api, access_token)
                payload: JSONObject = {
                    "tool_id": MANIFEST.tool_id,
                    "action": action,
                    "search_console_account": {"sub": account["id"], "email": account["label"]},
                    "site_url": site_url,
                    "sitemap_url": sitemap_url,
                }
                approval = api.approvals.request(
                    action_id=action,
                    summary=(
                        f"Submit Search Console sitemap {clip_text(sitemap_url, 220)} for "
                        f"{clip_text(site_url, 180)}."
                    ),
                    payload=payload,
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported Google Search Console action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except UnmappedProviderError:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Google Search Console request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            payload = approval.payload
            if payload.get("action") != "submit_sitemap":
                return ActionFailed("Search Console approval payload is invalid.")
            site_url = payload.get("site_url")
            sitemap_url = payload.get("sitemap_url")
            approved_account = payload.get("search_console_account")
            if (
                not isinstance(site_url, str)
                or not isinstance(sitemap_url, str)
                or not isinstance(approved_account, dict)
            ):
                return ActionFailed("Search Console approval payload is invalid.")
            access_token = SEARCH_CONSOLE_CREDENTIALS.access_token(api)
            current_account = SEARCH_CONSOLE_CREDENTIALS.refresh_identity(api, access_token)
            if approved_account.get("sub") != current_account["id"]:
                return ActionFailed(
                    "Google Search Console account changed after approval. Queue a new approval."
                )
            # Recheck both the live property membership and structural scope at
            # execution time; an approval cannot outlive access to the site.
            exact_site = cast(str, _sitemap_property(access_token, site_url)["site_url"])
            exact_sitemap = _url_under_property(sitemap_url, exact_site, "sitemap_url")
            _submit_sitemap(access_token, exact_site, exact_sitemap)
            return ApprovalExecuted(
                f"Submitted sitemap {clip_text(exact_sitemap, 300)} to Google Search Console."
            )
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except UnmappedProviderError:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Search Console sitemap submission failed.")


BUNDLED_TOOL = GoogleSearchConsoleTool()
