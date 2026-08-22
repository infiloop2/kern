"""Read-only TwitterAPI.io public-post search tool."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
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
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError, int_field, schema
from host.tools.shared.oauth2 import now
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
MAX_QUERY_CHARS = 512
MAX_WIRE_QUERY_CHARS = 1_024
MAX_POSTS = 20
DEFAULT_MAX_RESULTS = 10
DEFAULT_LOOKBACK_HOURS = 7 * 24
MAX_LOOKBACK_HOURS = 30 * 24
MAX_EXCLUDED_USERNAMES = 10
MAX_POST_TEXT_CHARS = 25_000
QUERY_TYPES = frozenset({"Latest", "Top"})
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
EXCLUDE_REPLY_RE = re.compile(r"(?<!\S)-(?:is:reply|filter:replies)(?!\S)")
EXCLUDE_RETWEET_RE = re.compile(r"(?<!\S)-(?:is:retweet|filter:nativeretweets)(?!\S)")

# The provider's per-post counters, the result field each becomes, and what it
# counts. One list feeds the normalization and the declared schema.
POST_METRIC_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("viewCount", "impression_count", "Views."),
    ("likeCount", "like_count", "Likes."),
    ("replyCount", "reply_count", "Replies."),
    ("retweetCount", "repost_count", "Reposts."),
    ("quoteCount", "quote_count", "Quote posts."),
    ("bookmarkCount", "bookmark_count", "Bookmarks."),
)
POST_METRIC_PROPERTIES: JSONObject = {
    result_key: outputs.integer(description) for _, result_key, description in POST_METRIC_FIELDS
}

OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many posts were returned, out of how many the provider sent."),
        "provider": outputs.text("Always twitterapi_io; names the source, since the official X tool returns a different shape."),
        "query": outputs.text("The search string as sent to TwitterAPI.io, including any filters Kern appended."),
        "query_type": outputs.text("Latest or Top, as sent to the provider."),
        "provider_posts_returned": outputs.integer("How many posts the provider returned before local filtering."),
        "billable_post_reads": outputs.integer("Post reads billed for this call; the provider bills at least one."),
        "locally_filtered_posts": outputs.integer("Posts dropped here as malformed, replies, or reposts."),
        "locally_truncated_posts": outputs.integer("Eligible posts dropped to honour max_results."),
        "posts": outputs.array_of(
            outputs.obj(
                {
                    "id": outputs.text("Numeric post id."),
                    "text": outputs.text("Post text, clipped."),
                    "url": outputs.text("Public x.com link to the post."),
                    "created_at": outputs.text("Publication time as the provider formats it."),
                    "lang": outputs.text("Language code the provider detected."),
                    "conversation_id": outputs.text("Id of the conversation root, empty when the provider omits it."),
                    "author_id": outputs.text("Numeric id of the author, empty when the provider omits it."),
                    "author_username": outputs.text("Author handle without the @, empty when the provider omits it."),
                    "author_name": outputs.text("Author display name."),
                    "public_metrics": outputs.obj(
                        POST_METRIC_PROPERTIES,
                        description="Metrics the provider returned; a metric it omits has no entry.",
                    ),
                    "is_reply": outputs.boolean("The post replies to another post."),
                    "is_retweet": outputs.boolean("The post is a repost of another post."),
                },
                ["id", "text", "url", "created_at", "lang", "conversation_id", "author_id", "author_username", "author_name", "public_metrics", "is_reply", "is_retweet"],
            ),
            "Up to max_results posts that survived local filtering.",
        ),
    },
    ["message", "provider", "query", "query_type", "provider_posts_returned", "billable_post_reads", "locally_filtered_posts", "locally_truncated_posts", "posts"],
)

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
                "Accepts queries up to 512 characters, defaults to the last seven days, and "
                "returns a caller-selected maximum of 1-20 posts from one provider page."
            ),
            data_policy=(
                "Sends the guarded search query (at most 512 characters), Latest/Top selection, "
                "structured recency and exclusion controls, and the configured API key to "
                "TwitterAPI.io. One read-only request can return and bill up to 20 public X posts; "
                "max_results only bounds what Kern returns to the agent. Runs directly with no approval."
            ),
            input_schema=schema(
                {
                    "query": {
                        "type": "string",
                        "description": (
                            "Public X advanced-search expression, including operators such as "
                            "from:, lang:, quotes, OR, and exclusions; 1-512 characters. "
                            "Kern translates -is:reply and -is:retweet to provider syntax."
                        ),
                    },
                    "query_type": {
                        "type": "string",
                        "description": "Latest (default) or Top.",
                    },
                    "max_results": {
                        "type": "string",
                        "description": (
                            "1-20; maximum posts Kern returns (default 10). The provider can still return "
                            "and bill up to 20 posts for its one fixed-size page."
                        ),
                    },
                    "lookback_hours": {
                        "type": "string",
                        "description": (
                            "0-720; only return posts this many hours old or newer "
                            "(default 168; 0 disables). "
                            "Kern sends the provider-supported since_time operator."
                        ),
                    },
                    "exclude_replies": {
                        "type": "boolean",
                        "description": "Exclude replies in the provider query and defensively from its response.",
                    },
                    "exclude_retweets": {
                        "type": "boolean",
                        "description": "Exclude native retweets in the provider query and defensively from its response.",
                    },
                    "exclude_usernames": {
                        "type": "array",
                        "maxItems": MAX_EXCLUDED_USERNAMES,
                        "items": {"type": "string"},
                        "description": "Up to 10 X usernames to exclude with provider -from: filters.",
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
            "Each action makes exactly one fixed-endpoint request. The provider can return and bill "
            "up to 20 posts; Kern returns at most max_results and exposes the provider read count. "
            "There is no agent-controlled cursor or pagination loop."
        ),
        (
            "Base search text is rejected above 512 characters and passes the host parameter "
            "guard before Kern appends strictly validated typed filters."
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
                            "The trimmed query text (1-512 characters) passes once as free text through "
                            "the full default host parameter guard before Kern translates supported "
                            "operators and appends strictly validated recency and exclusion controls: "
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
        "Use this tool for inexpensive public-post discovery when it is enabled. For parity with "
        "official X discovery, pass the full query (up to 512 characters), set max_results, choose "
        "the narrowest useful lookback_hours, and use the structured reply/retweet/username "
        "exclusions. It returns one provider page only; provider_posts_returned and "
        "billable_post_reads can exceed the displayed posts because TwitterAPI.io has no page-size "
        "parameter. Use the official X tool for connected-account reads, profile counts, or actions "
        "this tool does not provide."
    ),
)


@dataclass(frozen=True)
class SearchRequest:
    parameters: dict[str, str]
    max_results: int
    exclude_replies: bool
    exclude_retweets: bool


def _boolean_field(tool_input: JSONObject, key: str) -> bool:
    value = tool_input.get(key, False)
    if not isinstance(value, bool):
        raise ToolInputValidationError(f"TwitterAPI.io tool_input.{key} must be a boolean.")
    return value


def _excluded_usernames(tool_input: JSONObject, api: HostAPI) -> list[str]:
    value = tool_input.get("exclude_usernames", [])
    if not isinstance(value, list) or len(value) > MAX_EXCLUDED_USERNAMES:
        raise ToolInputValidationError(
            f"TwitterAPI.io tool_input.exclude_usernames must contain at most {MAX_EXCLUDED_USERNAMES} usernames."
        )
    usernames: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ToolInputValidationError(
                "TwitterAPI.io tool_input.exclude_usernames contains an invalid X username."
            )
        username = _username(api.outbound.guard_request_parameter_string(item))
        if not username:
            raise ToolInputValidationError(
                "TwitterAPI.io tool_input.exclude_usernames contains an invalid X username."
            )
        lowercase = username.lower()
        if lowercase not in usernames:
            usernames.append(lowercase)
    return usernames


def _search_request(tool_input: JSONObject, api: HostAPI) -> SearchRequest:
    supported = {
        "query",
        "query_type",
        "max_results",
        "lookback_hours",
        "exclude_replies",
        "exclude_retweets",
        "exclude_usernames",
    }
    extra = set(tool_input) - supported
    if extra:
        raise ToolInputValidationError(
            "TwitterAPI.io search tool input contains unsupported fields."
        )
    raw_query = tool_input.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise ToolInputValidationError("TwitterAPI.io query is required.")
    query = raw_query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputValidationError(
            f"TwitterAPI.io query must be at most {MAX_QUERY_CHARS} characters."
        )
    query = api.outbound.guard_request_parameter_string(query)
    query_type = tool_input.get("query_type", "Latest")
    if not isinstance(query_type, str) or query_type not in QUERY_TYPES:
        raise ToolInputValidationError("TwitterAPI.io query_type must be Latest or Top.")
    max_results = int_field(
        tool_input,
        "max_results",
        provider="TwitterAPI.io",
        default=DEFAULT_MAX_RESULTS,
        low=1,
        high=MAX_POSTS,
    )
    lookback_hours = int_field(
        tool_input,
        "lookback_hours",
        provider="TwitterAPI.io",
        default=DEFAULT_LOOKBACK_HOURS,
        low=0,
        high=MAX_LOOKBACK_HOURS,
    )
    exclude_replies = _boolean_field(tool_input, "exclude_replies") or bool(
        EXCLUDE_REPLY_RE.search(query)
    )
    exclude_retweets = _boolean_field(tool_input, "exclude_retweets") or bool(
        EXCLUDE_RETWEET_RE.search(query)
    )
    query = re.sub(r"(?<!\S)-is:reply(?!\S)", "-filter:replies", query)
    query = re.sub(r"(?<!\S)-is:retweet(?!\S)", "-filter:nativeretweets", query)
    suffixes: list[str] = []
    if exclude_replies and "-filter:replies" not in query.split():
        suffixes.append("-filter:replies")
    if exclude_retweets and "-filter:nativeretweets" not in query.split():
        suffixes.append("-filter:nativeretweets")
    suffixes.extend(f"-from:{username}" for username in _excluded_usernames(tool_input, api))
    if lookback_hours and not re.search(r"(?<!\S)since_time:\d+(?!\S)", query):
        suffixes.append(f"since_time:{now() - lookback_hours * 3600}")
    provider_query = " ".join((query, *suffixes))
    if len(provider_query) > MAX_WIRE_QUERY_CHARS:
        raise ToolInputValidationError(
            f"TwitterAPI.io query plus structured filters must be at most {MAX_WIRE_QUERY_CHARS} characters."
        )
    return SearchRequest(
        parameters={
            "query": provider_query,
            "queryType": query_type,
        },
        max_results=max_results,
        exclude_replies=exclude_replies,
        exclude_retweets=exclude_retweets,
    )


def _search_parameters(tool_input: JSONObject, api: HostAPI) -> dict[str, str]:
    return _search_request(tool_input, api).parameters


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
    for provider_key, result_key, _ in POST_METRIC_FIELDS:
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
        "is_reply": value.get("isReply") is True or bool(_numeric_id(value.get("inReplyToId"))),
        "is_retweet": value.get("type") == "retweet"
        or isinstance(value.get("retweeted_tweet"), dict)
        or text.startswith("RT @"),
    }
    return result


def _normalized_posts(
    response: dict[str, Any],
    *,
    max_results: int,
    exclude_replies: bool,
    exclude_retweets: bool,
) -> tuple[list[JSONObject], int, int, int]:
    raw_posts = response.get("tweets")
    if not isinstance(raw_posts, list):
        raise RuntimeError("TwitterAPI.io returned an invalid search response.")
    provider_posts = raw_posts[:MAX_POSTS]
    eligible: list[JSONObject] = []
    filtered = 0
    for value in provider_posts:
        normalized = _normalized_post(value)
        if normalized is None:
            filtered += 1
            continue
        if exclude_replies and normalized["is_reply"] is True:
            filtered += 1
            continue
        if exclude_retweets and normalized["is_retweet"] is True:
            filtered += 1
            continue
        eligible.append(normalized)
    truncated = max(0, len(eligible) - max_results)
    return eligible[:max_results], len(provider_posts), filtered, truncated


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
            request = _search_request(tool_input, api)
            response = _search(api.config["TWITTERAPI_IO_API_KEY"], request.parameters)
            posts, provider_posts, filtered, truncated = _normalized_posts(
                response,
                max_results=request.max_results,
                exclude_replies=request.exclude_replies,
                exclude_retweets=request.exclude_retweets,
            )
            result: JSONObject = {
                                "message": (
                    f"TwitterAPI.io returned {len(posts)} public post(s) from "
                    f"{provider_posts} provider result(s)."
                ),
                "provider": "twitterapi_io",
                "query": request.parameters["query"],
                "query_type": request.parameters["queryType"],
                "provider_posts_returned": provider_posts,
                "billable_post_reads": max(1, provider_posts),
                "locally_filtered_posts": filtered,
                "locally_truncated_posts": truncated,
                "posts": cast(list[JSONValue], posts),
            }
            return ActionExecuted(result)
        except UnmappedProviderError:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "TwitterAPI.io tool request failed.")


BUNDLED_TOOL = TwitterApiIoTool()
