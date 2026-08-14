"""Official Reddit Data API tool for one legacy personal-use script app."""

from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Mapping
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI
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
from host.tools.shared.inputs import ToolInputValidationError, clip_text, int_field, schema as _schema
from host.tools.shared.web import (
    ProviderWarning,
    WebRequestError,
    encode_query,
    is_public_https_url,
    json_request,
    known_provider_transport_error,
    provider_warning,
    unmapped_provider_error,
)

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE_URL = "https://oauth.reddit.com"
MAX_QUERY_CHARS = 512
MAX_LISTING_LIMIT = 25
MAX_COMMENT_LIMIT = 50
MAX_POST_TITLE_CHARS = 300
MAX_POST_BODY_CHARS = 40_000
MAX_COMMENT_BODY_CHARS = 10_000
SUMMARY_MAX_BYTES = 500

SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
POST_ID_RE = re.compile(r"^[A-Za-z0-9]{1,13}$")
FULLNAME_RE = re.compile(r"^t[1-6]_[A-Za-z0-9]{1,32}$")
COMMENT_PARENT_RE = re.compile(r"^t[13]_[A-Za-z0-9]{1,32}$")
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

LISTING_SORTS = frozenset({"hot", "new", "top", "rising"})
FEED_SORTS = LISTING_SORTS | {"best"}
SEARCH_SORTS = frozenset({"relevance", "hot", "top", "new", "comments"})
TIME_FILTERS = frozenset({"hour", "day", "week", "month", "year", "all"})

REDDIT_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}

REDDIT_READ_POLICY = (
    "Read-only. Sends only the listed identifiers, filters, pagination values, or search query "
    "to Reddit's official Data API as the connected account and returns Reddit content to active "
    "model context. Runs directly with no approval and cannot vote, save, subscribe, message, "
    "or moderate."
)

REDDIT_WRITE_POLICY = (
    "Creates content as the connected Reddit account only after explicit operator approval. "
    "Nothing is published while approval is pending. The approval is bound to the exact account, "
    "subreddit or parent fullname, and content; Kern rechecks the connected Reddit identity before "
    "executing it."
)


MANIFEST = ToolManifest(
    tool_id="reddit",
    display_name="Reddit",
    description=(
        "Use one existing Reddit personal-use script app to let your agent read "
        "your home feed, browse and search posts, open discussions, and—with your explicit "
        "approval—publish text or link posts and comments."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="get_profile",
            description=(
                "Read the connected Reddit account's username, stable id, karma totals, and account "
                "creation time. This cannot inspect another account."
            ),
            data_policy=(
                "Read-only. Sends an authenticated identity request to Reddit and returns a bounded "
                "account summary to active model context. Runs directly with no approval."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=REDDIT_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_home_feed",
            description=(
                "Read one page of the connected account's Reddit home feed, sorted by best, hot, new, "
                "top, or rising. Use next_cursor to continue."
            ),
            data_policy=REDDIT_READ_POLICY,
            input_schema=_schema(
                {
                    "sort": {"type": "string", "description": "best, hot, new, top, or rising (default best)."},
                    "time_filter": {"type": "string", "description": "For top only: hour, day, week, month, year, or all (default day)."},
                    "limit": {"type": "string", "description": "1-25 posts (default 10)."},
                    "after": {"type": "string", "description": "Opaque next_cursor from an earlier Reddit listing result."},
                }
            ),
            output_schema=REDDIT_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_subreddit_posts",
            description=(
                "Read one page of posts from a named public or account-accessible subreddit, sorted "
                "by hot, new, top, or rising. Use next_cursor to continue."
            ),
            data_policy=REDDIT_READ_POLICY,
            input_schema=_schema(
                {
                    "subreddit": {"type": "string", "description": "Subreddit name without r/."},
                    "sort": {"type": "string", "description": "hot, new, top, or rising (default hot)."},
                    "time_filter": {"type": "string", "description": "For top only: hour, day, week, month, year, or all (default day)."},
                    "limit": {"type": "string", "description": "1-25 posts (default 10)."},
                    "after": {"type": "string", "description": "Opaque next_cursor from an earlier Reddit listing result."},
                },
                ["subreddit"],
            ),
            output_schema=REDDIT_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="search_posts",
            description=(
                "Search Reddit posts globally or within one subreddit. Returns bounded post summaries "
                "and a next_cursor when another page is available."
            ),
            data_policy=REDDIT_READ_POLICY,
            input_schema=_schema(
                {
                    "query": {"type": "string", "description": "Reddit search query, up to 512 characters."},
                    "subreddit": {"type": "string", "description": "Optional subreddit name without r/."},
                    "sort": {"type": "string", "description": "relevance, hot, top, new, or comments (default relevance)."},
                    "time_filter": {"type": "string", "description": "hour, day, week, month, year, or all (default all)."},
                    "limit": {"type": "string", "description": "1-25 posts (default 10)."},
                    "after": {"type": "string", "description": "Opaque next_cursor from an earlier Reddit search result."},
                },
                ["query"],
            ),
            output_schema=REDDIT_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="read_post",
            description=(
                "Read one Reddit post and up to 50 top discussion comments. Pass the base-36 post id "
                "returned by another Reddit action or found in a Reddit post URL."
            ),
            data_policy=REDDIT_READ_POLICY,
            input_schema=_schema(
                {
                    "post_id": {"type": "string", "description": "Reddit post id, with or without the t3_ prefix."},
                    "comment_sort": {"type": "string", "description": "confidence, top, new, controversial, old, random, or qa (default confidence)."},
                    "comment_limit": {"type": "string", "description": "1-50 comments (default 20)."},
                },
                ["post_id"],
            ),
            output_schema=REDDIT_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="create_post",
            description=(
                "Queue approval to publish either a text post or a public HTTPS link post to one "
                "subreddit as the connected Reddit account."
            ),
            data_policy=REDDIT_WRITE_POLICY,
            input_schema=_schema(
                {
                    "subreddit": {"type": "string", "description": "Destination subreddit without r/."},
                    "title": {"type": "string", "description": "Post title, up to 300 characters."},
                    "kind": {"type": "string", "description": "self for a text post or link for a link post."},
                    "text": {"type": "string", "description": "Required body for a self post, up to 40,000 characters."},
                    "url": {"type": "string", "description": "Required public HTTPS URL for a link post."},
                },
                ["subreddit", "title", "kind"],
            ),
            output_schema=REDDIT_OUTPUT_SCHEMA,
            approval="operator",
        ),
        ActionSpec(
            id="create_comment",
            description=(
                "Queue approval to comment on a Reddit post or reply to a Reddit comment as the "
                "connected account."
            ),
            data_policy=REDDIT_WRITE_POLICY,
            input_schema=_schema(
                {
                    "parent_id": {
                        "type": "string",
                        "description": "Target post (t3_...) or comment (t1_...) fullname.",
                    },
                    "text": {"type": "string", "description": "Comment body, up to 10,000 characters."},
                },
                ["parent_id", "text"],
            ),
            output_schema=REDDIT_OUTPUT_SCHEMA,
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(
            key="REDDIT_CLIENT_ID",
            description="Client id shown under the name of an existing Reddit personal-use script app.",
        ),
        ConfigRequirement(
            key="REDDIT_CLIENT_SECRET",
            description="Secret for the existing Reddit personal-use script app.",
        ),
        ConfigRequirement(
            key="REDDIT_USERNAME",
            description="Reddit username listed as a developer of the personal-use script app.",
        ),
        ConfigRequirement(
            key="REDDIT_PASSWORD",
            description="Password for that Reddit account. Kern stores it encrypted and write-only.",
        ),
    ),
    protections=(
        "The package exposes no vote, save, subscribe, private-message, edit, delete, or moderation action even though Reddit's script token is not scope-limited.",
        "Post and comment writes are approval-gated and bound to the exact connected account, destination, and content.",
        "Every result is bounded: at most 25 listing posts or 50 discussion comments enter model context per call.",
        "The Reddit username, password, client secret, and other configured values stay in Kern's encrypted, write-only tool-config store and never enter agent context.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        "For each action, Kern exchanges the configured personal-use script credentials through Reddit's password grant for a short-lived bearer token, sends an automatically generated Kern User-Agent naming the configured Reddit account, and calls only oauth.reddit.com for data. Kern does not retain Reddit bearer tokens between calls or disclose its installed version in the User-Agent.",
        PARAM_GUARD_TECHNICAL_DETAIL,
    ),
    agent_notes=(
        "Use search_posts for topic discovery and get_subreddit_posts for a known community. "
        "Pagination cursors are provider-generated fullnames and may only be passed back to Reddit actions. "
        "create_post and create_comment always require operator approval. Voting, saving, subscribing, private "
        "messaging, editing, deleting, and moderation are not supported."
    ),
    setup_steps=(
        SetupStep(
            title="Confirm you have an existing script app",
            description=(
                "This integration supports only an existing Reddit app whose type is shown as ‘personal use "
                "script.’ It does not create a new Reddit app and does not support web-app OAuth or installed "
                "apps. Reddit currently directs new apps to Devvit and limits new legacy Data API requests."
            ),
            link_url="https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy",
            link_label="Review Reddit's Responsible Builder Policy",
        ),
        SetupStep(
            title="Verify the developer account",
            description=(
                "Open Reddit's app preferences, expand the existing personal-use script app, and confirm the "
                "Reddit account you will configure is listed under Developers. Script apps can authenticate "
                "only their listed developer accounts."
            ),
            link_url="https://www.reddit.com/prefs/apps",
            link_label="Open Reddit app preferences",
        ),
        SetupStep(
            title="Store the four credentials",
            description=(
                "Copy the short client id shown below the app name into REDDIT_CLIENT_ID and the app secret "
                "into REDDIT_CLIENT_SECRET. Set REDDIT_USERNAME and REDDIT_PASSWORD to the listed developer "
                "account, then enable Reddit. Kern automatically sends a descriptive User-Agent in the form "
                "script:kern:client (by /u/your_username); it does not include the installed Kern version."
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
                        label="Reads",
                        text=(
                            "Account identity requests, feed and subreddit selectors, post ids, listing cursors, "
                            "sorts, limits, and search text go directly to Reddit. Search text first passes Kern's "
                            "request-parameter guard, which denies secret- or credential-shaped values."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Authentication",
                        text=(
                            "Reddit receives the configured script client id and secret, Reddit username and password, "
                            "and Kern's generated account-specific User-Agent whenever Kern requests a short-lived bearer token. These "
                            "credentials and tokens never enter agent context."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Approved writes",
                        text=(
                            "After approval, the exact post subreddit, title, body or public link, or the exact comment "
                            "parent fullname and body go directly to Reddit."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(
                        label="Reddit only",
                        text=(
                            "The tool uses Reddit's personal-script password grant at the official token endpoint "
                            "to obtain a short-lived bearer token, then sends Data API requests only to Reddit's "
                            "official oauth.reddit.com hostname. This is not a browser OAuth flow. Approved posts "
                            "and comments become visible "
                            "on Reddit according to the destination community's visibility and rules."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="What Reddit can do with it",
                description=(
                    "Reddit processes the configured account credentials, account activity, searches, requested content, "
                    "User-Agent, and request metadata under its Privacy Policy, Data API Terms, and Developer Terms."
                ),
                links=(
                    DataSummaryLink(label="Reddit Privacy Policy", url="https://www.reddit.com/policies/privacy-policy"),
                    DataSummaryLink(label="Reddit Data API Terms", url="https://redditinc.com/policies/data-api-terms"),
                ),
            ),
            DataSummaryCard(
                title="How long Reddit retains it",
                description=(
                    "Reddit retains account, security, API, and service records under its policies rather than one "
                    "fixed period. Disabling the tool stops new calls but does not delete Reddit's own records or "
                    "content; delete the stored tool configuration in Kern to remove its encrypted local copy."
                ),
                links=(
                    DataSummaryLink(label="Reddit Privacy Policy", url="https://www.reddit.com/policies/privacy-policy"),
                ),
            ),
        ),
    ),
)


def _user_agent(api: HostAPI) -> str:
    return f"script:kern:client (by /u/{_username(api)})"


def _basic_auth_header(api: HostAPI) -> str:
    client_id = api.config["REDDIT_CLIENT_ID"]
    client_secret = api.config["REDDIT_CLIENT_SECRET"]
    if not client_id or len(client_id) > 256 or ":" in client_id:
        raise RuntimeError("REDDIT_CLIENT_ID must be a non-empty Reddit script app client id.")
    if not client_secret or len(client_secret) > 512:
        raise RuntimeError("REDDIT_CLIENT_SECRET must be a non-empty Reddit script app secret.")
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def _username(api: HostAPI) -> str:
    value = api.config["REDDIT_USERNAME"].strip()
    if not USERNAME_RE.fullmatch(value):
        raise RuntimeError("REDDIT_USERNAME must be a valid Reddit username without u/.")
    return value


def _script_access_token(api: HostAPI) -> str:
    password = api.config["REDDIT_PASSWORD"]
    if not password or len(password) > 512:
        raise RuntimeError("REDDIT_PASSWORD must be a non-empty Reddit account password.")
    try:
        response = json_request(
            "POST",
            REDDIT_TOKEN_URL,
            headers={"authorization": _basic_auth_header(api), "user-agent": _user_agent(api)},
            form={
                "grant_type": "password",
                "username": _username(api),
                "password": password,
            },
            failure_message="Reddit script authentication failed.",
            invalid_response_message="Reddit script authentication returned an invalid response.",
        )
    except WebRequestError as exc:
        if exc.status in {400, 401, 403}:
            raise provider_warning(
                "Reddit",
                "script authentication",
                exc,
                "Reddit rejected the personal-use script credentials. Check the app type, client id, "
                "client secret, username, password, and that the username is listed as a developer.",
            ) from exc
        if exc.status:
            raise provider_warning(
                "Reddit",
                "script authentication",
                exc,
                f"Reddit returned HTTP {exc.status} during script authentication.",
            ) from exc
        known = known_provider_transport_error(exc)
        if known:
            raise RuntimeError(known) from exc
        raise unmapped_provider_error("Reddit", "script authentication", exc) from None
    error = response.get("error")
    access_token = response.get("access_token")
    if isinstance(error, str) and error:
        raise RuntimeError(
            "Reddit rejected the personal-use script credentials. Check the app type, client id, "
            "client secret, username, password, and developer account."
        )
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Reddit script authentication returned no access token.")
    return access_token


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status == 401:
        message = (
            f"Reddit rejected the {what} request as unauthorized. Check the configured personal-use "
            "script credentials."
        )
    elif exc.status == 429:
        message = "Reddit's API rate limit was reached. Retry after Reddit's rate-limit window resets."
    elif exc.status == 403:
        message = (
            f"Reddit declined the {what} request (HTTP 403). Confirm that Reddit approved this app's "
            "Data API access and that the connected account may view the requested content."
        )
    elif exc.status:
        message = f"Reddit returned HTTP {exc.status} for the {what} request."
    else:
        known = known_provider_transport_error(exc)
        if known:
            return RuntimeError(known)
        return unmapped_provider_error("Reddit", what, exc)
    return provider_warning("Reddit", what, exc, message)


def _fetch_me(access_token: str, api: HostAPI) -> JSONObject:
    try:
        return json_request(
            "GET",
            f"{REDDIT_API_BASE_URL}/api/v1/me",
            headers={"authorization": f"Bearer {access_token}", "user-agent": _user_agent(api)},
            failure_message="Reddit profile lookup failed.",
            invalid_response_message="Reddit profile lookup returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, "profile lookup") from exc


def _account_from_me(response: JSONObject) -> ConnectionAccount:
    account_id = response.get("id")
    username = response.get("name")
    if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
        raise RuntimeError("Reddit did not return a stable account id.")
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise RuntimeError("Reddit did not return a valid account username.")
    return {"id": account_id, "label": f"u/{username}", "scopes": ["*"]}


def _script_identity(access_token: str, api: HostAPI) -> ConnectionAccount:
    account = _account_from_me(_fetch_me(access_token, api))
    configured = _username(api)
    if str(account["label"])[2:].casefold() != configured.casefold():
        raise RuntimeError(
            "Reddit authenticated a different account than REDDIT_USERNAME. Check the script credentials."
        )
    return account


def _api_get(access_token: str, api: HostAPI, path_and_query: str, *, what: str) -> JSONObject:
    try:
        return json_request(
            "GET",
            f"{REDDIT_API_BASE_URL}{path_and_query}",
            headers={"authorization": f"Bearer {access_token}", "user-agent": _user_agent(api)},
            failure_message=f"Reddit {what} request failed.",
            invalid_response_message=f"Reddit {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc


def _api_post(
    access_token: str, api: HostAPI, path: str, *, form: Mapping[str, str], what: str
) -> JSONObject:
    try:
        return json_request(
            "POST",
            f"{REDDIT_API_BASE_URL}{path}",
            headers={"authorization": f"Bearer {access_token}", "user-agent": _user_agent(api)},
            form=form,
            failure_message=f"Reddit {what} request failed.",
            invalid_response_message=f"Reddit {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc


def _choice(tool_input: JSONObject, key: str, allowed: frozenset[str], default: str) -> str:
    value = tool_input.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(f"Reddit tool_input.{key} must be one of {', '.join(sorted(allowed))}.")
    return value


def _subreddit(value: object) -> str:
    if not isinstance(value, str):
        raise ToolInputValidationError("Reddit tool_input.subreddit is required.")
    cleaned = value.strip()
    if cleaned.lower().startswith("r/"):
        cleaned = cleaned[2:]
    if not SUBREDDIT_RE.fullmatch(cleaned):
        raise ToolInputValidationError("Reddit tool_input.subreddit must be a valid subreddit name without spaces.")
    return cleaned


def _guarded_subreddit(value: object, api: HostAPI) -> str:
    return api.outbound.guard_request_parameter_string(_subreddit(value))


def _after(tool_input: JSONObject) -> str:
    value = tool_input.get("after")
    if value is None:
        return ""
    if not isinstance(value, str) or not FULLNAME_RE.fullmatch(value.strip()):
        raise ToolInputValidationError("Reddit tool_input.after must be a next_cursor returned by Reddit.")
    return value.strip()


def _listing_params(tool_input: JSONObject, *, default_limit: int = 10) -> dict[str, str]:
    params = {"limit": str(int_field(tool_input, "limit", provider="Reddit", default=default_limit, low=1, high=MAX_LISTING_LIMIT)), "raw_json": "1"}
    after = _after(tool_input)
    if after:
        params["after"] = after
    return params


def _safe_post_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("/r/"):
        return f"https://www.reddit.com{value}"
    return value if is_public_https_url(value) else ""


def _post_summary(data: JSONObject) -> JSONObject:
    score = data.get("score")
    comments = data.get("num_comments")
    created = data.get("created_utc")
    post_id = data.get("id")
    return {
        "id": post_id if isinstance(post_id, str) and POST_ID_RE.fullmatch(post_id) else "",
        "fullname": str(data.get("name") or "") if FULLNAME_RE.fullmatch(str(data.get("name") or "")) else "",
        "title": clip_text(str(data.get("title") or ""), 500),
        "body": clip_text(str(data.get("selftext") or ""), 4_000),
        "author": clip_text(str(data.get("author") or ""), 64),
        "subreddit": clip_text(str(data.get("subreddit") or ""), 64),
        "score": score if isinstance(score, int) and not isinstance(score, bool) else 0,
        "comment_count": comments if isinstance(comments, int) and not isinstance(comments, bool) else 0,
        "created_utc": created if isinstance(created, (int, float)) and not isinstance(created, bool) else None,
        "permalink": _safe_post_url(data.get("permalink")),
        "outbound_url": _safe_post_url(data.get("url")),
        "over_18": data.get("over_18") is True,
    }


def _listing(response: JSONObject, *, limit: int) -> tuple[list[JSONValue], str]:
    data = response.get("data")
    children = data.get("children") if isinstance(data, dict) else None
    posts: list[JSONValue] = []
    for child in (children if isinstance(children, list) else []):
        child_data = child.get("data") if isinstance(child, dict) else None
        if isinstance(child_data, dict) and len(posts) < limit:
            posts.append(_post_summary(cast(JSONObject, child_data)))
    raw_after = data.get("after") if isinstance(data, dict) else None
    next_cursor = raw_after if isinstance(raw_after, str) and FULLNAME_RE.fullmatch(raw_after) else ""
    return posts, next_cursor


def _get_profile(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    if tool_input:
        raise ToolInputValidationError("Reddit get_profile takes no tool input.")
    access_token = _script_access_token(api)
    response = _fetch_me(access_token, api)
    account = _account_from_me(response)
    link_karma = response.get("link_karma")
    comment_karma = response.get("comment_karma")
    created = response.get("created_utc")
    return {
        "status": "success_executed",
        "message": f"Loaded Reddit profile {account['label']}.",
        "profile": {
            "id": account["id"],
            "username": str(account["label"])[2:],
            "link_karma": link_karma if isinstance(link_karma, int) and not isinstance(link_karma, bool) else 0,
            "comment_karma": comment_karma if isinstance(comment_karma, int) and not isinstance(comment_karma, bool) else 0,
            "created_utc": created if isinstance(created, (int, float)) and not isinstance(created, bool) else None,
            "is_gold": response.get("is_gold") is True,
            "is_mod": response.get("is_mod") is True,
        },
    }


def _get_home_feed(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"sort", "time_filter", "limit", "after"}
    if extra:
        raise ToolInputValidationError("Reddit home feed only supports sort, time_filter, limit, and after.")
    sort = _choice(tool_input, "sort", FEED_SORTS, "best")
    params = _listing_params(tool_input)
    if sort == "top":
        params["t"] = _choice(tool_input, "time_filter", TIME_FILTERS, "day")
    elif "time_filter" in tool_input:
        raise ToolInputValidationError("Reddit time_filter is only valid when sort is top.")
    access_token = _script_access_token(api)
    response = _api_get(access_token, api, f"/{sort}?{encode_query(params)}", what="home feed")
    posts, cursor = _listing(response, limit=int(params["limit"]))
    return {
        "status": "success_executed",
        "message": f"Reddit returned {len(posts)} home-feed post(s).",
        "posts": posts,
        "next_cursor": cursor,
    }


def _get_subreddit_posts(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"subreddit", "sort", "time_filter", "limit", "after"}
    if extra:
        raise ToolInputValidationError("Reddit subreddit listing only supports subreddit, sort, time_filter, limit, and after.")
    subreddit = _guarded_subreddit(tool_input.get("subreddit"), api)
    sort = _choice(tool_input, "sort", LISTING_SORTS, "hot")
    params = _listing_params(tool_input)
    if sort == "top":
        params["t"] = _choice(tool_input, "time_filter", TIME_FILTERS, "day")
    elif "time_filter" in tool_input:
        raise ToolInputValidationError("Reddit time_filter is only valid when sort is top.")
    access_token = _script_access_token(api)
    response = _api_get(
        access_token, api, f"/r/{subreddit}/{sort}?{encode_query(params)}", what="subreddit listing"
    )
    posts, cursor = _listing(response, limit=int(params["limit"]))
    return {
        "status": "success_executed",
        "message": f"Reddit returned {len(posts)} post(s) from r/{subreddit}.",
        "subreddit": subreddit,
        "posts": posts,
        "next_cursor": cursor,
    }


def _search_posts(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"query", "subreddit", "sort", "time_filter", "limit", "after"}
    if extra:
        raise ToolInputValidationError("Reddit search only supports query, subreddit, sort, time_filter, limit, and after.")
    query = tool_input.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolInputValidationError("Reddit tool_input.query is required.")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputValidationError(f"Reddit search query must be at most {MAX_QUERY_CHARS} characters.")
    params = _listing_params(tool_input)
    params.update(
        {
            "q": api.outbound.guard_request_parameter_string(query),
            "sort": _choice(tool_input, "sort", SEARCH_SORTS, "relevance"),
            "t": _choice(tool_input, "time_filter", TIME_FILTERS, "all"),
            "type": "link",
        }
    )
    subreddit_value = tool_input.get("subreddit")
    if subreddit_value is None:
        path = "/search"
    else:
        subreddit = _guarded_subreddit(subreddit_value, api)
        params["restrict_sr"] = "true"
        path = f"/r/{subreddit}/search"
    access_token = _script_access_token(api)
    response = _api_get(access_token, api, f"{path}?{encode_query(params)}", what="post search")
    posts, cursor = _listing(response, limit=int(params["limit"]))
    return {
        "status": "success_executed",
        "message": f"Reddit search returned {len(posts)} post(s).",
        "posts": posts,
        "next_cursor": cursor,
    }


def _comment_summaries(listing: JSONObject, *, limit: int) -> list[JSONValue]:
    output: list[JSONValue] = []

    def visit(children: object, depth: int) -> None:
        if not isinstance(children, list) or len(output) >= limit:
            return
        for child in children:
            if len(output) >= limit:
                return
            if not isinstance(child, dict) or child.get("kind") != "t1":
                continue
            data = child.get("data")
            if not isinstance(data, dict):
                continue
            comment_id = data.get("id")
            score = data.get("score")
            created = data.get("created_utc")
            output.append(
                {
                    "id": comment_id if isinstance(comment_id, str) and POST_ID_RE.fullmatch(comment_id) else "",
                    "parent_id": str(data.get("parent_id") or "") if FULLNAME_RE.fullmatch(str(data.get("parent_id") or "")) else "",
                    "author": clip_text(str(data.get("author") or ""), 64),
                    "body": clip_text(str(data.get("body") or ""), 2_000),
                    "score": score if isinstance(score, int) and not isinstance(score, bool) else 0,
                    "created_utc": created if isinstance(created, (int, float)) and not isinstance(created, bool) else None,
                    "depth": depth,
                    "permalink": _safe_post_url(data.get("permalink")),
                }
            )
            replies = data.get("replies")
            reply_data = replies.get("data") if isinstance(replies, dict) else None
            visit(reply_data.get("children") if isinstance(reply_data, dict) else None, depth + 1)

    data = listing.get("data")
    visit(data.get("children") if isinstance(data, dict) else None, 0)
    return output


def _read_post(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"post_id", "comment_sort", "comment_limit"}
    if extra:
        raise ToolInputValidationError("Reddit read_post only supports post_id, comment_sort, and comment_limit.")
    post_id = tool_input.get("post_id")
    if not isinstance(post_id, str):
        raise ToolInputValidationError("Reddit tool_input.post_id is required.")
    post_id = post_id.strip()
    if post_id.lower().startswith("t3_"):
        post_id = post_id[3:]
    if not POST_ID_RE.fullmatch(post_id):
        raise ToolInputValidationError("Reddit tool_input.post_id must be a base-36 post id.")
    comment_sort = _choice(
        tool_input,
        "comment_sort",
        frozenset({"confidence", "top", "new", "controversial", "old", "random", "qa"}),
        "confidence",
    )
    comment_limit = int_field(
        tool_input, "comment_limit", provider="Reddit", default=20, low=1, high=MAX_COMMENT_LIMIT
    )
    params = encode_query(
        {"article": f"t3_{post_id}", "sort": comment_sort, "limit": str(comment_limit), "depth": "2", "raw_json": "1"}
    )
    access_token = _script_access_token(api)
    response = _api_get(access_token, api, f"/comments/{post_id}?{params}", what="post discussion")
    items = response.get("items")
    post: JSONObject | None = None
    comments: list[JSONValue] = []
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            posts, _ = _listing(cast(JSONObject, first), limit=1)
            if posts and isinstance(posts[0], dict):
                post = cast(JSONObject, posts[0])
        if len(items) > 1 and isinstance(items[1], dict):
            comments = _comment_summaries(cast(JSONObject, items[1]), limit=comment_limit)
    return {
        "status": "success_executed",
        "message": "Reddit post was not found." if post is None else f"Loaded Reddit post with {len(comments)} comment(s).",
        "post": post,
        "comments": comments,
    }


def _required_text(tool_input: JSONObject, key: str, *, maximum: int, label: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputValidationError(f"Reddit tool_input.{key} is required.")
    value = value.strip()
    if len(value) > maximum:
        raise ToolInputValidationError(f"Reddit {label} must be at most {maximum} characters.")
    return value


def _post_proposal(tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"subreddit", "title", "kind", "text", "url"}
    if extra:
        raise ToolInputValidationError(
            "Reddit create_post only supports subreddit, title, kind, text, and url."
        )
    subreddit = _subreddit(tool_input.get("subreddit"))
    title = _required_text(
        tool_input, "title", maximum=MAX_POST_TITLE_CHARS, label="post title"
    )
    kind = _choice(tool_input, "kind", frozenset({"self", "link"}), "self")
    if kind == "self":
        if "url" in tool_input:
            raise ToolInputValidationError("Reddit self posts cannot include tool_input.url.")
        text = _required_text(
            tool_input, "text", maximum=MAX_POST_BODY_CHARS, label="self-post body"
        )
        return {"subreddit": subreddit, "title": title, "kind": kind, "text": text}
    if "text" in tool_input:
        raise ToolInputValidationError("Reddit link posts cannot include tool_input.text.")
    url = tool_input.get("url")
    if not isinstance(url, str) or not is_public_https_url(url.strip()):
        raise ToolInputValidationError(
            "Reddit link posts require a public HTTPS tool_input.url with no embedded credentials."
        )
    return {"subreddit": subreddit, "title": title, "kind": kind, "url": url.strip()}


def _comment_proposal(tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"parent_id", "text"}
    if extra:
        raise ToolInputValidationError("Reddit create_comment only supports parent_id and text.")
    parent_id = tool_input.get("parent_id")
    if not isinstance(parent_id, str) or not COMMENT_PARENT_RE.fullmatch(parent_id.strip()):
        raise ToolInputValidationError(
            "Reddit tool_input.parent_id must be a post (t3_...) or comment (t1_...) fullname."
        )
    text = _required_text(
        tool_input, "text", maximum=MAX_COMMENT_BODY_CHARS, label="comment body"
    )
    return {"parent_id": parent_id.strip(), "text": text}


def _write_summary(action: str, proposal: JSONObject, account_label: str) -> str:
    label = clip_text(account_label, 80)
    if action == "create_post":
        destination = f"r/{proposal.get('subreddit')}"
        content = str(proposal.get("text") or proposal.get("url") or "")
        title = str(proposal.get("title") or "")
        for title_maximum, content_maximum in ((120, 160), (80, 100), (40, 40), (20, 20)):
            summary = (
                f"Publish {proposal.get('kind')} post to {destination} as {label}: "
                f"\"{clip_text(title, title_maximum)}\" — "
                f"\"{clip_text(content, content_maximum)}\""
            )
            if len(summary.encode("utf-8")) <= SUMMARY_MAX_BYTES:
                return summary
        return summary
    else:
        destination = str(proposal.get("parent_id") or "")
        content = str(proposal.get("text") or "")
        prefix = f"Comment on {destination} as {label}: "
    for maximum in (220, 140, 80):
        summary = f"{prefix}\"{clip_text(content, maximum)}\""
        if len(summary.encode("utf-8")) <= SUMMARY_MAX_BYTES:
            return summary
    return summary


def _verify_subreddit(access_token: str, api: HostAPI, subreddit: str) -> None:
    response = _api_get(access_token, api, f"/r/{subreddit}/about", what="subreddit lookup")
    data = response.get("data")
    display_name = data.get("display_name") if isinstance(data, dict) else None
    if not isinstance(display_name, str) or display_name.casefold() != subreddit.casefold():
        raise RuntimeError(f"Reddit could not verify r/{subreddit} as the post destination.")


def _verify_comment_parent(access_token: str, api: HostAPI, parent_id: str) -> None:
    response = _api_get(
        access_token,
        api,
        f"/api/info?{encode_query({'id': parent_id, 'raw_json': '1'})}",
        what="comment target lookup",
    )
    data = response.get("data")
    children = data.get("children") if isinstance(data, dict) else None
    found = False
    if isinstance(children, list):
        for child in children:
            child_data = child.get("data") if isinstance(child, dict) else None
            if isinstance(child_data, dict) and child_data.get("name") == parent_id:
                found = True
                break
    if not found:
        raise RuntimeError(
            "Reddit could not verify the approved post or comment target. Queue a new approval "
            "after confirming the parent still exists and is accessible."
        )


def _reddit_write_error(response: JSONObject, what: str) -> str:
    json_payload = response.get("json")
    errors = json_payload.get("errors") if isinstance(json_payload, dict) else None
    if not isinstance(errors, list) or not errors:
        return ""
    first = errors[0]
    code = first[0] if isinstance(first, list) and first and isinstance(first[0], str) else ""
    messages = {
        "RATELIMIT": "Reddit rate-limited this account's content submission. Retry after the indicated wait period.",
        "TOO_LONG": f"Reddit rejected the {what} because one of its fields is too long.",
        "NO_TEXT": f"Reddit rejected the {what} because required text is missing.",
        "BAD_URL": "Reddit rejected the link post URL.",
        "SUBREDDIT_NOEXIST": "Reddit rejected the post because the subreddit does not exist or is inaccessible.",
        "SUBREDDIT_NOTALLOWED": "Reddit does not allow this account to submit to that subreddit.",
    }
    return messages.get(
        code,
        f"Reddit rejected the {what}. Check the account's eligibility and the destination community's rules.",
    )


def _write_result(response: JSONObject, what: str, expected_kind: str) -> tuple[str, str]:
    error = _reddit_write_error(response, what)
    if error:
        raise RuntimeError(error)
    json_payload = response.get("json")
    data = json_payload.get("data") if isinstance(json_payload, dict) else None
    fullname = data.get("name") if isinstance(data, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    things = data.get("things") if isinstance(data, dict) else None
    if (not isinstance(fullname, str) or not fullname) and isinstance(things, list) and things:
        first_data = things[0].get("data") if isinstance(things[0], dict) else None
        if isinstance(first_data, dict):
            fullname = first_data.get("name")
            url = first_data.get("permalink") or first_data.get("url") or url
    if not isinstance(fullname, str) or not fullname.startswith(f"{expected_kind}_"):
        raise RuntimeError(f"Reddit {what} returned no valid created-content id.")
    if not FULLNAME_RE.fullmatch(fullname):
        raise RuntimeError(f"Reddit {what} returned an invalid created-content id.")
    return fullname, _safe_post_url(url)


def _create_post(access_token: str, api: HostAPI, proposal: JSONObject) -> tuple[str, str]:
    form = {
        "api_type": "json",
        "kind": str(proposal["kind"]),
        "sr": str(proposal["subreddit"]),
        "title": str(proposal["title"]),
        "raw_json": "1",
        "resubmit": "true",
        "sendreplies": "true",
    }
    if proposal["kind"] == "self":
        form["text"] = str(proposal["text"])
    else:
        form["url"] = str(proposal["url"])
    response = _api_post(access_token, api, "/api/submit", form=form, what="post submission")
    return _write_result(response, "post submission", "t3")


def _create_comment(access_token: str, api: HostAPI, proposal: JSONObject) -> tuple[str, str]:
    response = _api_post(
        access_token,
        api,
        "/api/comment",
        form={
            "api_type": "json",
            "thing_id": str(proposal["parent_id"]),
            "text": str(proposal["text"]),
            "raw_json": "1",
        },
        what="comment submission",
    )
    return _write_result(response, "comment submission", "t1")


class RedditTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "get_profile":
                return ActionExecuted(_get_profile(api, tool_input))
            if action == "get_home_feed":
                return ActionExecuted(_get_home_feed(api, tool_input))
            if action == "get_subreddit_posts":
                return ActionExecuted(_get_subreddit_posts(api, tool_input))
            if action == "search_posts":
                return ActionExecuted(_search_posts(api, tool_input))
            if action == "read_post":
                return ActionExecuted(_read_post(api, tool_input))
            if action in {"create_post", "create_comment"}:
                proposal = (
                    _post_proposal(tool_input)
                    if action == "create_post"
                    else _comment_proposal(tool_input)
                )
                access_token = _script_access_token(api)
                account = _script_identity(access_token, api)
                payload: JSONObject = {
                    "action": action,
                    "tool_id": MANIFEST.tool_id,
                    "reddit_account": {"id": account["id"], "label": account["label"]},
                    "proposal": proposal,
                }
                approval = api.approvals.request(
                    action_id=action,
                    summary=_write_summary(action, proposal, account["label"]),
                    payload=payload,
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported Reddit action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except ProviderWarning:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Reddit tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            if approval.action_id not in {"create_post", "create_comment"}:
                return ActionFailed("Reddit approval payload is invalid.")
            payload = approval.payload
            if payload.get("action") != approval.action_id or payload.get("tool_id") != MANIFEST.tool_id:
                return ActionFailed("Reddit approval payload is invalid.")
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                return ActionFailed("Reddit approval payload is invalid.")
            proposal_object = cast(JSONObject, proposal)
            validated = (
                _post_proposal(proposal_object)
                if approval.action_id == "create_post"
                else _comment_proposal(proposal_object)
            )
            if validated != proposal_object:
                return ActionFailed("Reddit approval payload is invalid.")

            approved_account = payload.get("reddit_account")
            if not isinstance(approved_account, dict) or not isinstance(
                approved_account.get("id"), str
            ):
                return ActionFailed("Reddit approval payload is invalid.")
            access_token = _script_access_token(api)
            current_account = _script_identity(access_token, api)
            if approved_account.get("id") != current_account["id"]:
                return ActionFailed(
                    "Reddit account changed after approval. Please queue a new approval."
                )

            if approval.action_id == "create_post":
                _verify_subreddit(access_token, api, str(validated["subreddit"]))
                fullname, url = _create_post(access_token, api, validated)
                suffix = f" at {url}" if url else ""
                return ApprovalExecuted(
                    f"Published Reddit post {fullname} to r/{validated['subreddit']} as "
                    f"{current_account['label']}{suffix}."
                )

            _verify_comment_parent(access_token, api, str(validated["parent_id"]))
            fullname, url = _create_comment(access_token, api, validated)
            suffix = f" at {url}" if url else ""
            return ApprovalExecuted(
                f"Published Reddit comment {fullname} as {current_account['label']}{suffix}."
            )
        except ToolInputValidationError:
            return ActionFailed("Reddit approval payload is invalid.")
        except ProviderWarning:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Reddit write failed after approval.")


BUNDLED_TOOL = RedditTool()
