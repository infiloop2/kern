"""Bounded Google PageSpeed Insights audits for public pages."""

from __future__ import annotations

import math
import ipaddress
import urllib.parse
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL, ParamGuardDenied
from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    SetupStep,
    ToolManifest,
    guarded_input,
    protect_inputs,
    validated_input,
)
from host.tools.results import ActionExecuted, ActionFailed, ActionResult
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError, guard_url_parameter_string, schema
from host.tools.shared.web import WebRequestError, json_request, known_provider_transport_error, unmapped_provider_error
from host.tools.tool import Tool


ENDPOINT = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
DOCS = "https://developers.google.com/speed/docs/insights/rest/v5/pagespeedapi/runpagespeed"
PRIVACY = "https://policies.google.com/privacy"
MAX_URL_BYTES = 2_048
MAX_AUDITS = 10
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
METRICS = (
    ("first-contentful-paint", "first_contentful_paint"),
    ("largest-contentful-paint", "largest_contentful_paint"),
    ("cumulative-layout-shift", "cumulative_layout_shift"),
    ("total-blocking-time", "total_blocking_time"),
    ("speed-index", "speed_index"),
    ("interactive", "time_to_interactive"),
)


SCORE_SCHEMA = outputs.obj(
    {
        "performance": outputs.nullable(outputs.number("Lighthouse performance score from 0 to 1."), "Null when not returned."),
        "accessibility": outputs.nullable(outputs.number("Lighthouse accessibility score from 0 to 1."), "Null when not returned."),
        "best_practices": outputs.nullable(outputs.number("Lighthouse best-practices score from 0 to 1."), "Null when not returned."),
        "seo": outputs.nullable(outputs.number("Lighthouse SEO score from 0 to 1."), "Null when not returned."),
    },
    ["performance", "accessibility", "best_practices", "seo"],
)

METRIC_SCHEMA = outputs.obj(
    {
        "id": outputs.text("Stable Kern metric id."),
        "title": outputs.text("Lighthouse metric title."),
        "display_value": outputs.text("Human-readable value returned by Lighthouse."),
        "numeric_value": outputs.nullable(outputs.number("Raw Lighthouse numeric value."), "Null when unavailable."),
        "numeric_unit": outputs.text("Unit for numeric_value, such as millisecond or unitless."),
        "score": outputs.nullable(outputs.number("Audit score from 0 to 1."), "Null when Lighthouse does not score it."),
    },
    ["id", "title", "display_value", "numeric_value", "numeric_unit", "score"],
)

AUDIT_SCHEMA = outputs.obj(
    {
        "id": outputs.text("Lighthouse audit id."),
        "title": outputs.text("Audit title."),
        "description": outputs.text("Bounded Lighthouse explanation."),
        "display_value": outputs.text("Human-readable measured value or estimated saving."),
        "score": outputs.nullable(outputs.number("Audit score from 0 to 1."), "Null when Lighthouse does not score it."),
    },
    ["id", "title", "description", "display_value", "score"],
)

INPUT_SCHEMA = schema(
    {
        "url": outputs.text("Public http or https page URL to audit, at most 2,048 UTF-8 bytes; credentials and IP-literal hosts are rejected."),
        "strategy": {
            "type": "string",
            "enum": ["mobile", "desktop"],
            "description": "Lighthouse device strategy; default mobile.",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATEGORIES)},
            "minItems": 1,
            "maxItems": 4,
            "description": "Unique Lighthouse categories to run; default all four supported categories.",
        },
    },
    ["url"],
)

OUTPUT_SCHEMA = outputs.obj(
    {
        "requested_url": outputs.text("URL submitted for analysis."),
        "final_url": outputs.text("Final page URL Lighthouse audited after redirects."),
        "strategy": outputs.text("Requested mobile or desktop strategy."),
        "analysis_time": outputs.text("Provider analysis timestamp."),
        "lighthouse_version": outputs.text("Lighthouse version used by Google."),
        "scores": SCORE_SCHEMA,
        "metrics": outputs.array_of(METRIC_SCHEMA, "Core lab metrics returned by Lighthouse."),
        "priority_audits": {
            "type": "array",
            "items": AUDIT_SCHEMA,
            "maxItems": MAX_AUDITS,
            "description": "Up to 10 lowest-scoring informative audits, ordered worst first.",
        },
    },
    ["requested_url", "final_url", "strategy", "analysis_time", "lighthouse_version", "scores", "metrics", "priority_audits"],
)


MANIFEST = ToolManifest(
    tool_id="pagespeed_insights",
    display_name="PageSpeed Insights",
    description="Audit any public page with Lighthouse performance, accessibility, best-practices and SEO diagnostics.",
    connection="enable_only",
    actions=protect_inputs(
        (
            ActionSpec(
                id="analyze_page",
                description="Run one bounded mobile or desktop Lighthouse audit and return core metrics plus the highest-priority findings.",
                data_policy="Sends the public page URL, selected strategy and categories to Google's PageSpeed Insights API. Google fetches the page from its infrastructure. The API key stays in a request credential and is never returned. Runs directly without approval. The provider API is free, subject to Google project quota; normal Kern runtime costs are separate.",
                input_schema=INPUT_SCHEMA,
                output_schema=OUTPUT_SCHEMA,
            ),
        ),
        {
            "analyze_page": {
                "url": guarded_input(allow_longer_text=True),
                "strategy": validated_input("One of mobile or desktop."),
                "categories": validated_input("A unique subset of four fixed Lighthouse category names."),
            }
        },
    ),
    config=(ConfigRequirement("PAGESPEED_INSIGHTS_API_KEY", "Google Cloud API key with the PageSpeed Insights API enabled."),),
    protections=(
        "Only public http or https URLs with named hosts are accepted; credentials, IP-literal hosts, fragments and non-web schemes are rejected.",
        "Each action performs one PageSpeed API request for at most four fixed categories and returns a bounded normalized result instead of the provider's large raw Lighthouse document.",
        "The Google API key remains in write-only host configuration and is never returned to the agent.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        "Kern sends one redirect-free GET to Google's fixed runPagespeed endpoint. Google, not this host, fetches the requested public page. Core lab metrics and at most ten low-scoring audits are normalized from lighthouseResult.",
        "PageSpeed Insights is appropriate for a new low-traffic domain because Lighthouse lab data does not require enough real-user traffic to qualify for Chrome UX Report data.",
        "The URL and nested-decoded path/query views use the longer-text guard tier under a stricter 2,048-byte URL limit, without identifier or machine-token exceptions. Unknown inputs are rejected; strategy and categories use fixed enums. This audits one page, not a site crawl or a complete technical SEO audit.",
        PARAM_GUARD_TECHNICAL_DETAIL,
    ),
    setup_steps=(
        SetupStep(
            "Enable the PageSpeed Insights API",
            "In a Google Cloud project, enable PageSpeed Insights API and create an API key. Restrict the key to that API where your Google Cloud policy permits it.",
            "https://developers.google.com/speed/docs/insights/v5/get-started",
            "PageSpeed API setup",
        ),
        SetupStep(
            "Save the API key",
            "Open PageSpeed Insights under Home > Integrations, save the key below, and enable the integration.",
            show_config=True,
        ),
        SetupStep(
            "Test one page",
            "Ask an agent to analyze a public page with mobile strategy. A successful call returns four category scores, lab metrics and bounded priority audits.",
            DOCS,
            "runPagespeed reference",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard("What leaves this host", "The page URL, device strategy, requested Lighthouse categories and configured API key go to Google. Google then fetches the public page. Returned findings become available to the agent and its selected model provider."),
            DataSummaryCard("Where it can go", "Requests go only to Google's fixed PageSpeed Insights API endpoint; the page itself is fetched by Google's service.", links=(DataSummaryLink("PageSpeed API reference", DOCS),)),
            DataSummaryCard("What Google can do with it", "Google processes the URL and page to run Lighthouse and may handle request data under its service terms and privacy policy. This integration does not grant Google access to private deployments.", links=(DataSummaryLink("Google Privacy Policy", PRIVACY),)),
            DataSummaryCard("How long Google retains it", "Google controls provider-side request and service telemetry retention. Disabling the integration stops future calls but does not delete data already processed by Google.", links=(DataSummaryLink("Google Privacy Policy", PRIVACY),)),
        )
    ),
    agent_notes="Use this for repeatable lab diagnostics on any public Infiverse domain, including brand-new sites. Mobile is the default. Treat scores as one measured run, not guaranteed user experience or ranking impact; compare like strategy and page over time.",
)


def _public_url(value: object, api: HostAPI) -> str:
    if not isinstance(value, str):
        raise ToolInputValidationError("PageSpeed Insights tool_input.url must be a public http or https URL.")
    candidate = value.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ToolInputValidationError("PageSpeed Insights tool_input.url must be a public http or https URL.") from exc
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ToolInputValidationError("PageSpeed Insights URLs must use named hosts.")
    try:
        byte_length = len(candidate.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ToolInputValidationError("PageSpeed Insights URL must be valid UTF-8 text.") from exc
    if (
        byte_length > MAX_URL_BYTES
        or parsed.scheme not in {"http", "https"}
        or "." not in hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or parsed.fragment
        or hostname.replace(".", "").isdigit()
        or hostname.lower() == "localhost"
    ):
        raise ToolInputValidationError("PageSpeed Insights tool_input.url must be a public http or https URL with a named host and no fragment.")
    return guard_url_parameter_string(candidate, api, allow_longer_text=True)


def _request_url(tool_input: JSONObject, api: HostAPI) -> tuple[str, str]:
    if set(tool_input) - {"url", "strategy", "categories"}:
        raise ToolInputValidationError("PageSpeed Insights request contains unsupported fields.")
    page_url = _public_url(tool_input.get("url"), api)
    strategy = tool_input.get("strategy", "mobile")
    if strategy not in {"mobile", "desktop"}:
        raise ToolInputValidationError("PageSpeed Insights strategy must be mobile or desktop.")
    raw_categories = tool_input.get("categories", list(CATEGORIES))
    if (
        not isinstance(raw_categories, list)
        or not 1 <= len(raw_categories) <= len(CATEGORIES)
        or any(not isinstance(value, str) or value not in CATEGORIES for value in raw_categories)
        or len(set(raw_categories)) != len(raw_categories)
    ):
        raise ToolInputValidationError("PageSpeed Insights categories must be a unique non-empty subset of performance, accessibility, best-practices and seo.")
    api_key = api.config["PAGESPEED_INSIGHTS_API_KEY"]
    params: list[tuple[str, str]] = [("url", page_url), ("strategy", cast(str, strategy))]
    params.extend(("category", cast(str, value)) for value in raw_categories)
    params.append(("key", api_key))
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}", page_url


def _text(value: object, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _audit_record(audit_id: str, raw: object, *, metric_id: str = "") -> JSONObject | None:
    if not isinstance(raw, dict):
        return None
    score = _finite_number(raw.get("score"))
    return {
        "id": metric_id or audit_id[:120],
        "title": _text(raw.get("title"), 240),
        "display_value": _text(raw.get("displayValue"), 240),
        "numeric_value": _finite_number(raw.get("numericValue")),
        "numeric_unit": _text(raw.get("numericUnit"), 80),
        "score": score,
    }


def _normalize(response: JSONObject, page_url: str, strategy: str) -> JSONObject:
    lighthouse = response.get("lighthouseResult")
    if not isinstance(lighthouse, dict):
        raise RuntimeError("PageSpeed Insights returned no Lighthouse result.")
    if lighthouse.get("runtimeError"):
        raise RuntimeError("PageSpeed Insights could not complete the Lighthouse audit. Confirm the page is public and reachable, then retry.")
    raw_categories = lighthouse.get("categories")
    raw_audits = lighthouse.get("audits")
    if not isinstance(raw_categories, dict) or not isinstance(raw_audits, dict):
        raise RuntimeError("PageSpeed Insights returned an invalid Lighthouse result.")
    scores: JSONObject = {}
    for category in CATEGORIES:
        value = raw_categories.get(category)
        scores[category.replace("-", "_")] = _finite_number(value.get("score")) if isinstance(value, dict) else None
    metrics: list[JSONValue] = []
    for provider_id, output_id in METRICS:
        item = _audit_record(provider_id, raw_audits.get(provider_id), metric_id=output_id)
        if item is not None:
            metrics.append(item)
    priority: list[tuple[float, JSONObject]] = []
    metric_provider_ids = {item[0] for item in METRICS}
    for audit_id, raw in raw_audits.items():
        if audit_id in metric_provider_ids or not isinstance(audit_id, str) or not isinstance(raw, dict):
            continue
        score = _finite_number(raw.get("score"))
        mode = raw.get("scoreDisplayMode")
        if score is None or score >= 0.9 or mode in {"notApplicable", "manual", "informative"}:
            continue
        priority.append((score, {
            "id": audit_id[:120],
            "title": _text(raw.get("title"), 240),
            "description": _text(raw.get("description"), 800),
            "display_value": _text(raw.get("displayValue"), 240),
            "score": score,
        }))
    priority.sort(key=lambda item: (item[0], cast(str, item[1]["id"])))
    return {
        "requested_url": _text(lighthouse.get("requestedUrl"), 2_048) or page_url,
        "final_url": _text(lighthouse.get("finalUrl"), 2_048) or _text(response.get("id"), 2_048) or page_url,
        "strategy": strategy,
        "analysis_time": _text(response.get("analysisUTCTimestamp"), 80) or _text(lighthouse.get("fetchTime"), 80),
        "lighthouse_version": _text(lighthouse.get("lighthouseVersion"), 80),
        "scores": scores,
        "metrics": metrics,
        "priority_audits": cast(list[JSONValue], [item[1] for item in priority[:MAX_AUDITS]]),
    }


def _failure(exc: WebRequestError) -> ActionFailed:
    if exc.status in {401, 403}:
        return ActionFailed("Google rejected the PageSpeed Insights API key or project access. Check the key and API enablement in Home > Integrations.")
    if exc.status == 429:
        return ActionFailed("Google PageSpeed Insights quota was reached. Wait or raise the API quota, then retry.")
    if exc.status == 400:
        return ActionFailed("Google could not analyze this URL. Confirm that the page is public and reachable, then retry.")
    if exc.status in {500, 502, 503, 504}:
        return ActionFailed("Google PageSpeed Insights is temporarily unavailable. Retry later.")
    known = known_provider_transport_error(exc)
    if known:
        return ActionFailed(known)
    raise unmapped_provider_error("PageSpeed Insights", "analysis", exc)


class PageSpeedInsightsTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "analyze_page":
            return ActionFailed("Unsupported PageSpeed Insights action.")
        try:
            request_url, page_url = _request_url(tool_input, api)
            strategy = cast(str, tool_input.get("strategy", "mobile"))
            response = json_request(
                "GET",
                request_url,
                failure_message="PageSpeed Insights request failed.",
                invalid_response_message="PageSpeed Insights returned invalid JSON.",
                timeout=60,
                max_bytes=MAX_RESPONSE_BYTES,
            )
            return ActionExecuted(_normalize(response, page_url, strategy))
        except WebRequestError as exc:
            return _failure(exc)
        except (ToolInputValidationError, ParamGuardDenied, RuntimeError) as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = PageSpeedInsightsTool()
