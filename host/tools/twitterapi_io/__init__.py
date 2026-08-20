"""Read-only TwitterAPI.io public-post search tool."""

from __future__ import annotations

import re
import urllib.parse
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
from host.tools.shared.inputs import ToolInputValidationError, schema
from host.tools.shared.web import (
    UnmappedProviderError,
    WebRequestError,
    encode_query,
    json_request,
    known_provider_transport_error,
    unmapped_provider_error,
)
from host.tools.tool import Tool

SEARCH_ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"
MAX_QUERY_CHARS = 100
MAX_POSTS = 20
MAX_POST_TEXT_CHARS = 25_000
QUERY_TYPES = frozenset({"Latest", "Top"})
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}

MANIFEST = ToolManifest(
    tool_id="twitterapi_io",
    display_name="TwitterAPI.io",
    description=(
        "Search public X posts through the independent TwitterAPI.io service. "
        "This is a read-only, lower-cost discovery path; it never uses or changes an X account."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="search_tweets",
            description=(
                "Run one bounded public-post search using X advanced-search syntax. "
                "The query is limited to 100 characters and one provider page of at most 20 posts."
            ),
            data_policy=(
                "Sends the guarded search query (at most 100 characters), Latest/Top selection, "
                "and the configured API key to TwitterAPI.io. Returns at most 20 public X posts "
                "from one read-only request; runs directly with no approval."
            ),
            input_schema=schema(
                {
                    "query": {
                        "type": "string",
                        "description": (
                            "Public X advanced-search expression, including operators such as "
                            "from:, lang:, quotes, OR, and exclusions; 1-100 characters."
                        ),
                    },
                    "query_type": {
                        "type": "string",
                        "description": "Latest (default) or Top.",
                    },
                },
                ["query"],
            ),
            output_schema=OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(
            key="TWITTERAPI_IO_API_KEY",
            description="TwitterAPI.io API key for this Kern deployment.",
        ),
    ),
    protections=(
        "Read-only: the tool cannot post, like, follow, message, or change an X account.",
        (
            "Each action makes exactly one fixed-endpoint request and returns at most 20 posts; "
            "there is no agent-controlled cursor or pagination loop."
        ),
        (
            "Search text is rejected above 100 characters and passes the host parameter guard "
            "before it leaves the host."
        ),
        "The API key remains in write-only host config and is never returned to the agent.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,),
    setup_steps=(
        SetupStep(
            title="Create a TwitterAPI.io account",
            description=(
                "Sign up with TwitterAPI.io and review its pricing, privacy policy, terms, and "
                "acceptable-use policy. It is an independent third party, not X."
            ),
            link_url="https://twitterapi.io/",
            link_label="Open TwitterAPI.io",
        ),
        SetupStep(
            title="Create and copy an API key",
            description=(
                "Create an API key in the TwitterAPI.io dashboard and copy it. Treat the key like "
                "a password; Kern stores it as write-only tool config."
            ),
            link_url="https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search",
            link_label="View the advanced-search API documentation",
        ),
        SetupStep(
            title="Configure and enable the tool",
            description=(
                "Open TwitterAPI.io under Home > Integrations, save the key below, and enable the "
                "tool. Run one narrow search and confirm it in Tool audit."
            ),
            show_config=True,
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(
                        label="Search",
                        text=(
                            "The trimmed query text (1-100 characters) passes once as free text "
                            "through the full default host parameter guard before leaving: "
                            "secret, credential, personal-identifier, encoded, and random-looking "
                            "shapes are denied. "
                            "The accepted query and strict Latest/Top selection leave in one request."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Authentication",
                        text=(
                            "The deployment API key is sent in the X-API-Key header. It stays in "
                            "write-only Kern config and is never exposed to the agent."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                description=(
                    "Requests go only to the fixed api.twitterapi.io advanced-search endpoint, "
                    "operated by Prism Digital, LLC. The provider obtains the matching public X "
                    "content; Kern does not authenticate to X for this tool."
                ),
                links=(
                    DataSummaryLink(
                        label="TwitterAPI.io API documentation",
                        url="https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search",
                    ),
                ),
            ),
            DataSummaryCard(
                title="What TwitterAPI.io can do with it",
                description=(
                    "Its privacy policy says it does not sell, share, or use customer data for "
                    "advertising. Its terms limit the service to public data and make the customer "
                    "responsible for lawful use and compliance with platform rules."
                ),
                links=(
                    DataSummaryLink(
                        label="TwitterAPI.io privacy policy",
                        url="https://twitterapi.io/privacy",
                    ),
                    DataSummaryLink(
                        label="TwitterAPI.io terms",
                        url="https://twitterapi.io/terms",
                    ),
                    DataSummaryLink(
                        label="TwitterAPI.io acceptable use",
                        url="https://twitterapi.io/acceptable-use",
                    ),
                ),
            ),
            DataSummaryCard(
                title="How long TwitterAPI.io retains it",
                description=(
                    "Its privacy policy states a maximum 48-hour retention period, automatic "
                    "deletion after that period, and no backups after deletion."
                ),
                links=(
                    DataSummaryLink(
                        label="TwitterAPI.io privacy policy",
                        url="https://twitterapi.io/privacy",
                    ),
                ),
            ),
        ),
    ),
    agent_notes=(
        "Use this tool for inexpensive public-post discovery when it is enabled. It returns one "
        "page only; narrow the query instead of trying to paginate. Use the official X tool for "
        "connected-account reads, profile counts, or actions this tool does not provide."
    ),
)


def _search_parameters(tool_input: JSONObject, api: HostAPI) -> dict[str, str]:
    raw_query = tool_input.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise ToolInputValidationError("TwitterAPI.io query is required.")
    query = raw_query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputValidationError(
            f"TwitterAPI.io query must be at most {MAX_QUERY_CHARS} characters."
        )
    query_type = tool_input.get("query_type", "Latest")
    if not isinstance(query_type, str) or query_type not in QUERY_TYPES:
        raise ToolInputValidationError("TwitterAPI.io query_type must be Latest or Top.")
    return {
        "query": api.outbound.guard_request_parameter_string(query),
        "queryType": query_type,
    }


def _search(api_key: str, parameters: dict[str, str]) -> dict[str, Any]:
    url = f"{SEARCH_ENDPOINT}?{encode_query(parameters)}"
    try:
        return json_request(
            "GET",
            url,
            headers={"X-API-Key": api_key},
            failure_message="TwitterAPI.io search request failed.",
            invalid_response_message="TwitterAPI.io returned an invalid search response.",
        )
    except WebRequestError as exc:
        if exc.status == 400:
            message = "TwitterAPI.io rejected the search query."
        elif exc.status in {401, 403}:
            message = "TwitterAPI.io rejected the configured API key."
        elif exc.status == 402:
            message = "TwitterAPI.io credits are exhausted."
        elif exc.status == 429:
            message = "TwitterAPI.io rate limit was reached."
        elif exc.status:
            raise unmapped_provider_error("TwitterAPI.io", "search", exc) from None
        else:
            message = known_provider_transport_error(exc)
            if not message:
                raise unmapped_provider_error("TwitterAPI.io", "search", exc) from None
        raise RuntimeError(message) from exc


def _clipped_string(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _numeric_id(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= 25 and text.isascii() and text.isdecimal() else ""


def _username(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if USERNAME_RE.fullmatch(text) else ""


def _normalized_post(value: object) -> JSONObject | None:
    if not isinstance(value, dict):
        return None
    post_id = _numeric_id(value.get("id"))
    if not post_id:
        return None
    text = _clipped_string(value.get("text"), MAX_POST_TEXT_CHARS)
    author_value = value.get("author")
    author = author_value if isinstance(author_value, dict) else {}
    author_username = _username(author.get("userName"))
    metrics: JSONObject = {}
    for provider_key, result_key in (
        ("viewCount", "impression_count"),
        ("likeCount", "like_count"),
        ("replyCount", "reply_count"),
        ("retweetCount", "repost_count"),
        ("quoteCount", "quote_count"),
        ("bookmarkCount", "bookmark_count"),
    ):
        count = _nonnegative_int(value.get(provider_key))
        if count is not None:
            metrics[result_key] = count
    result: JSONObject = {
        "id": post_id,
        "text": text,
        "url": (
            f"https://x.com/{author_username}/status/{post_id}"
            if author_username
            else f"https://x.com/i/status/{post_id}"
        ),
        "created_at": _clipped_string(value.get("createdAt"), 64),
        "lang": _clipped_string(value.get("lang"), 16),
        "conversation_id": _numeric_id(value.get("conversationId")),
        "author_id": _numeric_id(author.get("id")),
        "author_username": author_username,
        "author_name": _clipped_string(author.get("name"), 256),
        "public_metrics": metrics,
    }
    return result


def _normalized_posts(response: dict[str, Any]) -> list[JSONObject]:
    raw_posts = response.get("tweets")
    if not isinstance(raw_posts, list):
        raise RuntimeError("TwitterAPI.io returned an invalid search response.")
    posts: list[JSONObject] = []
    for value in raw_posts[:MAX_POSTS]:
        normalized = _normalized_post(value)
        if normalized is not None:
            posts.append(normalized)
    return posts


class TwitterApiIoTool(Tool):
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action != "search_tweets":
            return ActionFailed("Unsupported TwitterAPI.io action.")
        try:
            parameters = _search_parameters(tool_input, api)
            response = _search(api.config["TWITTERAPI_IO_API_KEY"], parameters)
            posts = _normalized_posts(response)
            result: JSONObject = {
                "status": "success_executed",
                "message": f"TwitterAPI.io returned {len(posts)} public post(s).",
                "query": parameters["query"],
                "query_type": parameters["queryType"],
                "posts": cast(list[JSONValue], posts),
            }
            return ActionExecuted(result)
        except UnmappedProviderError:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "TwitterAPI.io tool request failed.")


BUNDLED_TOOL = TwitterApiIoTool()
