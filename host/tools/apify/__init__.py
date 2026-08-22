"""Bounded local-business research through one fixed Apify Google Maps Actor."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any, cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.host_api import HostAPI
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
from host.tools.results import ActionExecuted, ActionFailed, ActionResult
from host.tools.shared.inputs import bounded_int, clip_text
from host.tools.shared import outputs
from host.tools.shared.web import (
    UnmappedProviderError,
    WebRequestError,
    known_provider_transport_error,
    request_bytes,
    unmapped_provider_error,
)
from host.tools.tool import Tool

API_ORIGIN = "https://api.apify.com"
ACTOR_ID = "compass~crawler-google-places"
SYNC_RUN_PATH = f"/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
API_TIMEOUT_SECONDS = 100
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_NORMALIZED_RESULT_BYTES = 60 * 1024
MAX_SEARCH_RESULTS = 20
MAX_DETAIL_REVIEWS = 5
MAX_DETAIL_IMAGES = 8
MAX_SEARCH_CHARS = 160
MAX_LOCATION_CHARS = 160
SEARCH_MAX_CHARGE_USD = "0.50"
DETAIL_MAX_CHARGE_USD = "0.25"
PLACE_ID_RE = re.compile(r"^(?:ChIJ|GhIJ)[A-Za-z0-9_-]{23}$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")

WEBSITE_FILTERS = {
    "all": "allPlaces",
    "with_website": "withWebsite",
    "without_website": "withoutWebsite",
}
MINIMUM_RATINGS = {
    "any": "",
    "2": "two",
    "2.5": "twoAndHalf",
    "3": "three",
    "3.5": "threeAndHalf",
    "4": "four",
    "4.5": "fourAndHalf",
}

# Every normalized field _summary and _details produce. Numbers arrive as
# rendered decimal strings ("4.5", "128"), so they are declared as the strings
# they are rather than as the numbers they describe.
SUMMARY_PROPERTIES: JSONObject = {
    "place_id": outputs.text("Google Maps place id; pass to get_business_details. Empty when the Actor returned a malformed id."),
    "name": outputs.text("Business name, up to 300 characters."),
    "description": outputs.text("Google Maps listing description, up to 1000 characters."),
    "category": outputs.text("Primary Google Maps category."),
    "categories": outputs.array_of({"type": "string"}, "Up to 12 categories the listing claims, including the primary one."),
    "address": outputs.text("Full street address as Google Maps renders it."),
    "neighborhood": outputs.text("Neighborhood name, empty when Google Maps has none."),
    "city": outputs.text("City or locality."),
    "postal_code": outputs.text("Postal code."),
    "country_code": outputs.text("ISO country code."),
    "latitude": outputs.text("Decimal latitude as a string, \"0\" when unavailable."),
    "longitude": outputs.text("Decimal longitude as a string, \"0\" when unavailable."),
    "phone": outputs.text("Public phone number in the listing, empty when absent."),
    "website": outputs.text("Public https website from the listing, empty when absent or not public."),
    "google_maps_url": outputs.text("Google Maps link built from place_id, empty when the id is missing."),
    "rating": outputs.text("Average review score 1-5 as a string, \"0\" when unrated."),
    "review_count": outputs.text("Number of Google reviews as a string."),
    "image_count": outputs.text("Number of listing images as a string."),
    "temporarily_closed": outputs.boolean("Google Maps marks the business temporarily closed."),
    "permanently_closed": outputs.boolean("Google Maps marks the business permanently closed."),
}

SEARCH_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many bounded business results Apify returned."),
        "businesses": outputs.array_of(
            outputs.obj(SUMMARY_PROPERTIES),
            "At most the requested limit, trimmed further if the normalized payload would exceed the host cap.",
        ),
    },
    ["message", "businesses"],
)

DETAIL_PROPERTIES: JSONObject = {
    **SUMMARY_PROPERTIES,
    "opening_hours": outputs.array_of(
        outputs.obj(
            {
                "day": outputs.text("Day name as Google Maps localizes it."),
                "hours": outputs.text("Opening hours for that day, or a closed marker."),
            }
        ),
        "Up to 14 day entries; empty when the listing publishes no hours.",
    ),
    "images": outputs.array_of(
        outputs.obj(
            {
                "url": outputs.text("Google-hosted image URL."),
                "author_name": outputs.text("Uploader name Google attributes the image to, empty when absent."),
                "author_url": outputs.text("Google profile URL for the uploader, empty when absent."),
                "uploaded_at": outputs.text("Upload timestamp Google reports, empty when absent."),
            }
        ),
        "Up to max_images deduplicated images with their source attribution.",
    ),
    "reviews": outputs.array_of(
        outputs.obj(
            {
                "text": outputs.text("Review body, up to 1200 characters; reviews without text are dropped."),
                "stars": outputs.text("Review score 1-5 as a string."),
                "published_at": outputs.text("Publication timestamp Google reports."),
                "likes": outputs.text("Helpful-vote count as a string."),
                "owner_response": outputs.text("The owner's public reply, empty when there is none."),
            }
        ),
        "Up to max_reviews reviews. Reviewer identities are never requested.",
    ),
    "additional_info": outputs.array_of(
        outputs.obj(
            {
                "label": outputs.text("Attribute group, e.g. Accessibility or Service options."),
                "value": outputs.text("Flattened attributes in that group, e.g. \"Wheelchair accessible: yes\"."),
            }
        ),
        "Service attributes Google Maps lists for the business.",
    ),
}

DETAIL_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("Confirmation that one business profile was retrieved."),
        "business": outputs.obj(DETAIL_PROPERTIES, description="One business, trimmed from the end if it would exceed the host payload cap."),
    },
    ["message", "business"],
)

MANIFEST = ToolManifest(
    tool_id="apify",
    display_name="Apify Business Data",
    description=(
        "Lets your agent find local businesses and retrieve bounded public business details through "
        "This tool always uses the fixed Apify Actor compass/crawler-google-places."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="search_businesses",
            description=(
                "Search one public-business category in one named area and return at most 20 normalized "
                "business summaries with place ids, public contact details, ratings, counts, categories, "
                "closure status, and location. The fixed Apify Actor runs immediately and can spend up to "
                "$0.50; it cannot run arbitrary Actors, webhooks, or code."
            ),
            data_policy=(
                "Sends the search term, location, result limit, language, selected website/rating/closed filters, "
                "and the deployment API token to Apify's compass/crawler-google-places Actor. Apify then queries "
                "Google Maps. Kern forces reviewer data, reviews, images, employee leads, social-profile enrichment, "
                "directories, and competitor analysis off for this action, normalizes at most 20 public business "
                "summaries, and returns them to active model context. The call runs directly, incurs provider charges, "
                "and has a provider-side $0.50 maximum charge."
            ),
            input_schema={
                "type": "object",
                "required": ["query", "location"],
                "properties": {
                    "query": {"type": "string", "description": "One business category or service, up to 160 characters; do not put the area here."},
                    "location": {"type": "string", "description": "One city, borough, or area plus country, up to 160 characters."},
                    "limit": {"type": "string", "description": "Maximum businesses returned and charged, 1-20 (default 10)."},
                    "language": {"type": "string", "description": "Apify-supported language code such as en or pt-BR (default en)."},
                    "minimum_rating": {"type": "string", "enum": list(MINIMUM_RATINGS), "description": "Optional provider-side minimum: any, 2, 2.5, 3, 3.5, 4, or 4.5. A non-any filter adds a per-place provider charge."},
                    "website_filter": {"type": "string", "enum": list(WEBSITE_FILTERS), "description": "all, with_website, or without_website (default all). A non-all filter adds a per-place provider charge."},
                    "skip_closed": {"type": "boolean", "description": "Skip temporarily or permanently closed places (default true). This adds a per-place provider filter charge."},
                },
                "additionalProperties": False,
            },
            output_schema=SEARCH_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_business_details",
            description=(
                "Retrieve one known Google Maps place by place_id with its listing details, opening hours, service "
                "attributes, up to 5 reviews without reviewer identities, and up to 8 image references with source "
                "attribution. The fixed Apify Actor runs immediately and can spend up to $0.25."
            ),
            data_policy=(
                "Sends one validated Google Maps place_id, bounded review/image counts, language, and the deployment "
                "API token to the fixed Apify Actor compass/crawler-google-places. The agent cannot choose or run "
                "another Actor. Apify queries Google Maps. Kern always disables "
                "business-website contact extraction, reviewer identities, employee leads, social-profile enrichment, "
                "competitor analysis, directories, ordering widgets, and unlimited collection, and it drops question "
                "and answer data plus every other unlisted Actor field from the result. The Actor's place-detail page "
                "may still retrieve those discarded Google Maps fields at Apify. The normalized single-business result "
                "enters active model context. This direct action incurs provider charges and has a provider-side $0.25 "
                "maximum charge."
            ),
            input_schema={
                "type": "object",
                "required": ["place_id"],
                "properties": {
                    "place_id": {"type": "string", "description": "Google Maps place_id returned by search_businesses; exactly 27 URL-safe characters beginning ChIJ or GhIJ."},
                    "language": {"type": "string", "description": "Apify-supported language code such as en (default en)."},
                    "max_reviews": {"type": "string", "description": "Reviews to return, 0-5 (default 3); never requests all reviews."},
                    "max_images": {"type": "string", "description": "Image URLs to return, 0-8 (default 6); image attribution is always requested."},
                },
                "additionalProperties": False,
            },
            output_schema=DETAIL_OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(
            key="APIFY_API_TOKEN",
            description="A limited-scope, expiring Apify API token used only for the fixed Actor compass/crawler-google-places.",
        ),
    ),
    protections=(
        "The Apify token stays in write-only tool config, is sent only in the Authorization header, and is never returned to or read by the agent.",
        "This tool always uses the fixed Apify Actor compass/crawler-google-places. Agents cannot choose or run another Actor, task, dataset, webhook, build, proxy, callback, or arbitrary URL.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        "Kern uses Apify's synchronous Actor endpoint with no retries, redirect following, webhooks, or caller-controlled endpoint. The request fixes clean JSON output, a run timeout below the agent-tool timeout, maxItems, and maxTotalChargeUsd. A timed-out provider run may finish at Apify, but the dollar cap still applies.",
        "Google Maps content is untrusted third-party data. Kern clips text and arrays, derives Google Maps links from validated place ids, permits only structurally public listing websites, and permits image or attribution URLs only on expected Google hosts.",
        "The tool returns image URLs and reviews for research and drafting; scraping does not grant a copyright or marketing license. Confirm ownership or permission with the business before publishing photos or review text, preserve required source attribution, and use licensed stock or placeholders when rights are unclear.",
        PARAM_GUARD_TECHNICAL_DETAIL,
    ),
    setup_steps=(
        SetupStep(
            title="Create an Apify account and check pricing",
            description="Create an Apify account, then use the linked page to confirm the current price for compass/crawler-google-places and set an account spending limit. You do not need to review the Actor's code. This tool cannot run any other Actor.",
            link_url="https://apify.com/compass/crawler-google-places",
            link_label="Check current Actor pricing",
        ),
        SetupStep(
            title="Accept the data and terms boundary",
            description="Apify is a scraping processor, not Google Maps. Review Apify's DPA and privacy terms, Google's Maps terms, and the law that applies to prospecting. Collect only data needed for a documented purpose; public availability is not permission to republish or market to a person.",
            link_url="https://docs.apify.com/legal/data-processing-addendum",
            link_label="Read Apify's Data Processing Addendum",
        ),
        SetupStep(
            title="Create a narrow, expiring API token",
            description=(
                "In Apify Console create a separate scoped token with resource-specific Run permission for only "
                "compass/crawler-google-places and access to its default run storage, choose Restricted access for "
                "the Actor's run token, and set an expiry and account spending limit."
            ),
            link_url="https://docs.apify.com/integrations/api",
            link_label="View Apify token guidance",
        ),
        SetupStep(
            title="Configure and enable Apify Business Data",
            description="Save the token as APIFY_API_TOKEN, then enable the tool.",
            show_config=True,
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                description=(
                    "Business input sent from the host is either a search term and area, or one Google place id. "
                    "The search term and area first pass the host parameter guard (see Technical notes); place ids must "
                    "match Google's place-id format. Kern also sends the Apify token in the authorization header and "
                    "fixed bounded run settings."
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Apify and Compass", text="Kern sends requests only to api.apify.com. The fixed Actor compass/crawler-google-places lists Compass as its developer and is labeled Maintained by Apify; its developer may process the Actor's inputs and outputs to provide the service."),
                    DataSummaryPoint(label="Google Maps", text="The Compass Actor makes upstream requests to Google Maps. Kern cannot inspect or constrain those provider-side requests."),
                ),
            ),
            DataSummaryCard(
                title="What Apify can do with it",
                description=(
                    "Apify may process and store the request and results to run and support the service, prevent abuse, "
                    "and comply with law. The Actor's developer may process its inputs and outputs as needed to provide "
                    "the Actor. The terms do not grant Apify or the developer a right to publish this data or use it for "
                    "unrelated model training. Apify may use infrastructure subprocessors."
                ),
                links=(
                    DataSummaryLink(label="Apify DPA", url="https://docs.apify.com/legal/data-processing-addendum"),
                    DataSummaryLink(label="Apify privacy policy", url="https://docs.apify.com/legal/privacy-policy"),
                    DataSummaryLink(label="Google Maps terms", url="https://maps.google.com/help/terms_maps/"),
                ),
            ),
            DataSummaryCard(
                title="How long Apify retains it",
                description=(
                    "Apify says run storage follows the account plan's retention settings; on the free plan the 10 most "
                    "recent runs are retained for four months. This integration does not delete provider run records. "
                    "Set the shortest available Apify retention and delete runs in Apify Console when no longer needed."
                ),
                links=(
                    DataSummaryLink(label="Apify storage and retention", url="https://docs.apify.com/storage"),
                ),
            ),
        ),
    ),
    agent_notes=(
        "Use search_businesses for short candidate lists, then get_business_details only for selected place_ids. "
        "Use model web search or Brave separately when website research is needed; this tool does not crawl business sites. "
        "Do not claim that scraped images or reviews are licensed for publication: before putting them on a prospect "
        "website, confirm ownership or permission and keep source attribution; otherwise use licensed stock or placeholders. "
        "Treat phone numbers as personal data where applicable."
    ),
)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}


def _values(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _first(*values: object) -> object:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: object, *, limit: int = 300) -> str:
    return clip_text(value.strip(), limit) if isinstance(value, str) else ""


def _number(value: object) -> str:
    if isinstance(value, bool):
        return "0"
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (ValueError, OverflowError):
            return "0"
        if math.isfinite(number):
            rendered = str(int(number)) if number.is_integer() else str(number)
            return rendered if len(rendered) <= 40 else "0"
    if isinstance(value, str):
        try:
            return _number(float(value.replace(",", "").strip()))
        except (ValueError, OverflowError):
            pass
    return "0"


def _public_url(value: object, *, allowed_hosts: tuple[str, ...] | None = None) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2_048 or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    if (
        parsed.scheme not in {"http", "https"}
        or "." not in host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return ""
    if allowed_hosts is not None and not any(host == item or host.endswith("." + item) for item in allowed_hosts):
        return ""
    return url


def _google_url(value: object) -> str:
    url = _public_url(value, allowed_hosts=("google.com", "googleusercontent.com", "ggpht.com"))
    return url if url.startswith("https://") else ""


def _place_id(value: JSONValue | None) -> str:
    if not isinstance(value, str) or not PLACE_ID_RE.fullmatch(value.strip()):
        raise ValueError("place_id must be exactly 27 URL-safe characters beginning ChIJ or GhIJ.")
    return value.strip()


def _language(value: JSONValue | None) -> str:
    language = "en" if value in {None, ""} else _text(value, limit=20)
    if not LANGUAGE_RE.fullmatch(language):
        raise ValueError("language must be a short language code such as en or pt-BR.")
    return language


def _free_text(value: JSONValue | None, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{name} must be at most {limit} characters.")
    return text


def _bool(value: JSONValue | None, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false.")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _provider_items(raw: bytes) -> list[Mapping[str, Any]]:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Apify returned an invalid Google Maps response.") from exc
    if isinstance(decoded, dict):
        if "error" in decoded:
            raise RuntimeError("Apify rejected the bounded Google Maps Actor run.")
        possible = decoded.get("items") or decoded.get("data")
        decoded = possible if isinstance(possible, list) else [decoded]
    if not isinstance(decoded, list):
        raise RuntimeError("Apify returned an invalid Google Maps response.")
    return [_mapping(item) for item in decoded if isinstance(item, dict)]


def _run_actor(api_token: str, actor_input: JSONObject, *, max_items: int, max_charge: str) -> list[Mapping[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "clean": "true",
            "format": "json",
            "limit": str(max_items),
            "maxItems": str(max_items),
            "maxTotalChargeUsd": max_charge,
            "timeout": str(API_TIMEOUT_SECONDS),
        }
    )
    payload = json.dumps(actor_input, separators=(",", ":"), allow_nan=False).encode("utf-8")
    raw = request_bytes(
        "POST",
        f"{API_ORIGIN}{SYNC_RUN_PATH}?{params}",
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        data=payload,
        failure_message="Apify Google Maps Actor request failed.",
        timeout=API_TIMEOUT_SECONDS + 5,
        max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
    )
    return _provider_items(raw)


def _strings(value: object, *, limit: int, item_limit: int = 300) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        text = _text(candidate, limit=item_limit)
        if text and text not in seen:
            seen.add(text)
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _summary(raw: Mapping[str, Any]) -> JSONObject:
    place_id = _text(_first(raw.get("placeId"), raw.get("place_id")), limit=256)
    if not PLACE_ID_RE.fullmatch(place_id):
        place_id = ""
    location = _mapping(raw.get("location"))
    maps_url = (
        "https://www.google.com/maps/search/?api=1&query=Google&query_place_id="
        + urllib.parse.quote(place_id, safe="")
        if place_id
        else ""
    )
    return {
        "place_id": place_id,
        "name": _text(_first(raw.get("title"), raw.get("name")), limit=300),
        "description": _text(raw.get("description"), limit=1_000),
        "category": _text(_first(raw.get("categoryName"), raw.get("category")), limit=200),
        "categories": cast(list[JSONValue], _strings(raw.get("categories"), limit=12, item_limit=160)),
        "address": _text(raw.get("address"), limit=500),
        "neighborhood": _text(raw.get("neighborhood"), limit=200),
        "city": _text(raw.get("city"), limit=200),
        "postal_code": _text(_first(raw.get("postalCode"), raw.get("postal_code")), limit=40),
        "country_code": _text(_first(raw.get("countryCode"), raw.get("country_code")), limit=10),
        "latitude": _number(_first(location.get("lat"), raw.get("latitude"))),
        "longitude": _number(_first(location.get("lng"), raw.get("longitude"))),
        "phone": _text(_first(raw.get("phoneUnformatted"), raw.get("phone")), limit=80),
        "website": _public_url(raw.get("website")),
        "google_maps_url": maps_url,
        "rating": _number(_first(raw.get("totalScore"), raw.get("rating"))),
        "review_count": _number(_first(raw.get("reviewsCount"), raw.get("reviewCount"))),
        "image_count": _number(_first(raw.get("imagesCount"), raw.get("imageCount"))),
        "temporarily_closed": bool(raw.get("temporarilyClosed") is True),
        "permanently_closed": bool(raw.get("permanentlyClosed") is True),
    }


def _opening_hours(raw: Mapping[str, Any]) -> list[JSONObject]:
    output: list[JSONObject] = []
    for value in _values(raw.get("openingHours"))[:14]:
        item = _mapping(value)
        day = _text(item.get("day"), limit=30)
        hours = _text(_first(item.get("hours"), item.get("time")), limit=120)
        if day or hours:
            output.append({"day": day, "hours": hours})
    return output


def _images(raw: Mapping[str, Any], *, limit: int) -> list[JSONObject]:
    if limit == 0:
        return []
    detailed = _values(raw.get("images"))
    if not detailed:
        detailed = [{"imageUrl": value} for value in _values(raw.get("imageUrls"))]
    output: list[JSONObject] = []
    seen: set[str] = set()
    for value in detailed:
        item = _mapping(value)
        url = _google_url(_first(item.get("imageUrl"), item.get("url")))
        if not url or url in seen:
            continue
        seen.add(url)
        output.append(
            {
                "url": url,
                "author_name": _text(item.get("authorName"), limit=160),
                "author_url": _google_url(item.get("authorUrl")),
                "uploaded_at": _text(item.get("uploadedAt"), limit=80),
            }
        )
        if len(output) >= limit:
            break
    return output


def _reviews(raw: Mapping[str, Any], *, limit: int) -> list[JSONObject]:
    if limit == 0:
        return []
    output: list[JSONObject] = []
    for value in _values(raw.get("reviews")):
        item = _mapping(value)
        text = _text(_first(item.get("text"), item.get("textTranslated")), limit=1_200)
        if not text:
            continue
        output.append({
            "text": text,
            "stars": _number(_first(item.get("stars"), item.get("rating"))),
            "published_at": _text(_first(item.get("publishedAtDate"), item.get("publishAt")), limit=100),
            "likes": _number(item.get("likesCount")),
            "owner_response": _text(item.get("responseFromOwnerText"), limit=1_000),
        })
        if len(output) >= limit:
            break
    return output


def _additional_info(raw: Mapping[str, Any]) -> list[JSONObject]:
    output: list[JSONObject] = []
    value = raw.get("additionalInfo")
    if isinstance(value, dict):
        iterable: list[tuple[object, object]] = list(value.items())
    else:
        iterable = []
        for entry in _values(value):
            item = _mapping(entry)
            iterable.append((_first(item.get("key"), item.get("label"), item.get("name")), _first(item.get("value"), item.get("values"))))
    for label_value, detail_value in iterable:
        label = _text(label_value, limit=160)
        if isinstance(detail_value, list):
            parts: list[str] = []
            for item in detail_value[:10]:
                if isinstance(item, dict):
                    for key, nested_value in list(item.items())[:8]:
                        nested_text = "yes" if nested_value is True else "no" if nested_value is False else _text(nested_value, limit=120)
                        key_text = _text(key, limit=80)
                        if key_text and nested_text:
                            parts.append(f"{key_text}: {nested_text}")
                else:
                    item_text = _text(item, limit=120)
                    if item_text:
                        parts.append(item_text)
            detail = ", ".join(parts)
        elif isinstance(detail_value, dict):
            detail = ", ".join(f"{_text(key, limit=80)}: {_text(item, limit=120)}" for key, item in list(detail_value.items())[:8])
        else:
            detail = _text(detail_value, limit=500)
        if label and detail:
            output.append({"label": label, "value": detail})
        if len(output) >= 25:
            break
    return output


def _details(raw: Mapping[str, Any], *, max_reviews: int, max_images: int) -> JSONObject:
    summary = _summary(raw)
    summary.update(
        {
            "opening_hours": cast(list[JSONValue], _opening_hours(raw)),
            "images": cast(list[JSONValue], _images(raw, limit=max_images)),
            "reviews": cast(list[JSONValue], _reviews(raw, limit=max_reviews)),
            "additional_info": cast(list[JSONValue], _additional_info(raw)),
        }
    )
    return summary


def _serialized_size(value: JSONValue) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _bounded_businesses(businesses: list[JSONObject]) -> list[JSONObject]:
    output: list[JSONObject] = []
    for business in businesses:
        candidate = [*output, business]
        if _serialized_size(cast(list[JSONValue], candidate)) > MAX_NORMALIZED_RESULT_BYTES:
            break
        output.append(business)
    return output


def _bounded_detail(business: JSONObject) -> JSONObject:
    """Keep a maliciously maximal provider record below the host payload cap."""
    for key in ("additional_info", "reviews", "images"):
        values = business.get(key)
        if not isinstance(values, list):
            continue
        while values and _serialized_size(business) > MAX_NORMALIZED_RESULT_BYTES:
            values.pop()
    categories = business.get("categories")
    if isinstance(categories, list):
        while categories and _serialized_size(business) > MAX_NORMALIZED_RESULT_BYTES:
            categories.pop()
    if _serialized_size(business) > MAX_NORMALIZED_RESULT_BYTES:
        for key in ("description", "address", "website", "name"):
            business[key] = _text(business.get(key), limit=200)
    return business


def _search_input(tool_input: JSONObject, api: HostAPI) -> tuple[JSONObject, int]:
    query = api.outbound.guard_request_parameter_string(
        _free_text(tool_input.get("query"), name="query", limit=MAX_SEARCH_CHARS)
    )
    location = api.outbound.guard_request_parameter_string(
        _free_text(tool_input.get("location"), name="location", limit=MAX_LOCATION_CHARS)
    )
    limit = bounded_int(tool_input.get("limit"), name="limit", default=10, minimum=1, maximum=MAX_SEARCH_RESULTS)
    minimum_rating = tool_input.get("minimum_rating", "any")
    website_filter = tool_input.get("website_filter", "all")
    if not isinstance(minimum_rating, str) or minimum_rating not in MINIMUM_RATINGS:
        raise ValueError("minimum_rating must be any, 2, 2.5, 3, 3.5, 4, or 4.5.")
    if not isinstance(website_filter, str) or website_filter not in WEBSITE_FILTERS:
        raise ValueError("website_filter must be all, with_website, or without_website.")
    actor_input: JSONObject = {
        "searchStringsArray": [query],
        "locationQuery": location,
        "maxCrawledPlacesPerSearch": limit,
        "language": _language(tool_input.get("language")),
        "website": WEBSITE_FILTERS[website_filter],
        "skipClosedPlaces": _bool(tool_input.get("skip_closed"), name="skip_closed", default=True),
        "scrapePlaceDetailPage": False,
        "scrapeTableReservationProvider": False,
        "scrapeOrderOnline": False,
        "includeWebResults": False,
        "scrapeDirectories": False,
        "scrapeContacts": False,
        "scrapeSocialMediaProfiles": {"facebooks": False, "instagrams": False, "youtubes": False, "tiktoks": False, "twitters": False},
        "maximumLeadsEnrichmentRecords": 0,
        "verifyLeadsEnrichmentEmails": False,
        "maxReviews": 0,
        "scrapeReviewsPersonalData": False,
        "maxImages": 0,
        "scrapeImageAuthors": False,
        "enableCompetitorAnalysis": False,
        "maxCompetitorsToAnalyze": 0,
    }
    if MINIMUM_RATINGS[minimum_rating]:
        actor_input["placeMinimumStars"] = MINIMUM_RATINGS[minimum_rating]
    return actor_input, limit


def _detail_input(tool_input: JSONObject) -> tuple[JSONObject, int, int]:
    place_id = _place_id(tool_input.get("place_id"))
    max_reviews = bounded_int(tool_input.get("max_reviews"), name="max_reviews", default=3, minimum=0, maximum=MAX_DETAIL_REVIEWS)
    max_images = bounded_int(tool_input.get("max_images"), name="max_images", default=6, minimum=0, maximum=MAX_DETAIL_IMAGES)
    actor_input: JSONObject = {
        "placeIds": [place_id],
        "maxCrawledPlacesPerSearch": 1,
        "language": _language(tool_input.get("language")),
        "skipClosedPlaces": False,
        "scrapePlaceDetailPage": True,
        "scrapeTableReservationProvider": False,
        "scrapeOrderOnline": False,
        "includeWebResults": False,
        "scrapeDirectories": False,
        "maxQuestions": 0,
        "scrapeContacts": False,
        "scrapeSocialMediaProfiles": {"facebooks": False, "instagrams": False, "youtubes": False, "tiktoks": False, "twitters": False},
        "maximumLeadsEnrichmentRecords": 0,
        "verifyLeadsEnrichmentEmails": False,
        "maxReviews": max_reviews,
        "reviewsSort": "mostRelevant",
        "reviewsOrigin": "google",
        "scrapeReviewsPersonalData": False,
        "maxImages": max_images,
        "scrapeImageAuthors": max_images > 0,
        "enableCompetitorAnalysis": False,
        "maxCompetitorsToAnalyze": 0,
    }
    return actor_input, max_reviews, max_images


class ApifyTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            api_token = api.config["APIFY_API_TOKEN"]
            if action == "search_businesses":
                actor_input, limit = _search_input(tool_input, api)
                items = _run_actor(api_token, actor_input, max_items=limit, max_charge=SEARCH_MAX_CHARGE_USD)
                businesses = [_summary(item) for item in items[:limit]]
                businesses = [item for item in businesses if item.get("place_id")]
                businesses = _bounded_businesses(businesses)
                return ActionExecuted(
                    {
                                                "message": f"Apify returned {len(businesses)} bounded public business result(s).",
                        "businesses": cast(list[JSONValue], businesses),
                    }
                )
            if action == "get_business_details":
                actor_input, max_reviews, max_images = _detail_input(tool_input)
                items = _run_actor(api_token, actor_input, max_items=1, max_charge=DETAIL_MAX_CHARGE_USD)
                if not items:
                    return ActionFailed("Apify returned no business for that place_id.")
                business = _bounded_detail(
                    _details(
                        items[0],
                        max_reviews=max_reviews,
                        max_images=max_images,
                    )
                )
                if not business.get("place_id"):
                    return ActionFailed("Apify returned no valid business for that place_id.")
                return ActionExecuted(
                    {
                                                "message": "Retrieved one bounded public business profile through Apify.",
                        "business": business,
                    }
                )
            return ActionFailed("Unsupported Apify Business Data action.")
        except WebRequestError as exc:
            if exc.status in {401, 403}:
                message = "Apify rejected the configured API key or Actor access."
            elif exc.status == 402:
                message = "Apify requires billing or sufficient account credit for this Actor run."
            elif exc.status == 429:
                message = "Apify request capacity or account limits were exhausted."
            elif exc.status == 408:
                message = "The bounded Apify Actor run did not finish before its timeout; do not retry automatically because the original run may still incur charges."
            elif exc.status:
                message = f"Apify returned HTTP {exc.status} for the bounded Google Maps Actor run."
            else:
                message = known_provider_transport_error(exc)
                if not message:
                    raise unmapped_provider_error("Apify", "Google Maps Actor run", exc) from None
            return ActionFailed(message)
        except UnmappedProviderError:
            raise
        except (ValueError, RuntimeError) as exc:
            return ActionFailed(str(exc) or "Apify Business Data request failed.")
        except Exception:
            return ActionFailed("Apify Business Data request failed.")


BUNDLED_TOOL = ApifyTool()
