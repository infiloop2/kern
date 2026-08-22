"""X (Twitter) tool package."""

from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Mapping
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionResult,
)
from host.tools.tool import (
    ConnectionStatus,
    CredentialFlow,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
)
from host.tools.host_api import ConnectionAccount, HostAPI, StoredCredential
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError, clip_text, int_field, schema as _schema
from host.tools.shared.oauth2 import (
    IntegrationReconnectRequired,
    access_token_is_fresh,
    clear_if_still_loaded,
    now,
    pkce_verifier_and_challenge,
    require_scopes,
    save_if_still_connected,
    signed_state,
    verify_state,
)
from host.tools.shared.web import (
    ProviderWarning,
    WebRequestError,
    encode_query,
    json_request,
    known_provider_transport_error,
    provider_warning,
    unmapped_provider_error,
)

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
X_API_BASE_URL = "https://api.x.com/2"
X_OAUTH_SCOPES = ("tweet.read", "users.read", "offline.access")
# offline.access is required at connect time: without it X issues no refresh
# token, and the 2-hour access token would strand the connection.
REQUIRED_X_SCOPES = frozenset(X_OAUTH_SCOPES)
X_RECONNECT_MESSAGE = "X (Twitter) is no longer connected. Please reconnect the tool."
DEFAULT_TOKEN_LIFETIME_SECONDS = 7200
MAX_QUERY_CHARS = 512
TWEET_ID_RE = re.compile(r"^[0-9]{1,25}$")
USER_ID_RE = re.compile(r"^[0-9]{1,19}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{1,15}$")
WOEID_RE = re.compile(r"^[0-9]{1,10}$")
START_TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
WORLDWIDE_WOEID = "1"
TWEET_FIELDS = "created_at,author_id,public_metrics,conversation_id"
X_READ_POLICY = (
    "Read-only. Sends the listed query values to the X API authenticated as the "
    "connected account and returns public post data. Each call is billed to the "
    "deployment's X API pay-per-use credits. The result enters active model context. "
    "Runs directly with no approval."
)
X_PERSONALIZED_TRENDS_POLICY = (
    "Read-only. Sends an authenticated request for the connected account's personalized "
    "trend topics (not public post data) and returns them to active model context. Each "
    "call is billed to the deployment's X API pay-per-use credits and requires X Premium. "
    "Runs directly with no approval."
)


# X's public_metrics for a post: the fields _tweet_summary copies and what each
# one counts. One list feeds the read and its declared schema.
TWEET_METRIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("retweet_count", "Reposts."),
    ("reply_count", "Replies."),
    ("like_count", "Likes."),
    ("quote_count", "Quote posts."),
    ("bookmark_count", "Bookmarks."),
    ("impression_count", "Impressions."),
)
TWEET_METRIC_PROPERTIES: JSONObject = {
    field: outputs.integer(description) for field, description in TWEET_METRIC_FIELDS
}

TWEET_SCHEMA: JSONObject = outputs.obj(
    {
        "id": outputs.text("Post id; pass to read_tweet."),
        "text": outputs.text("Post text, up to 1200 characters."),
        "author_id": outputs.text("Numeric id of the author, empty when X does not expand it."),
        "author_username": outputs.text("Author handle without the @, empty when X does not expand it."),
        "created_at": outputs.text("ISO 8601 publication time."),
        "metrics": outputs.obj(
            TWEET_METRIC_PROPERTIES,
            description="Public metrics X returned; a metric X withheld has no entry, so an empty object means X sent none.",
        ),
    },
    ["id", "text", "author_id", "author_username", "created_at", "metrics"],
)

SEARCH_TWEETS_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many posts the search returned."),
        "tweets": outputs.array_of(TWEET_SCHEMA, "Up to max_results recent posts, newest first."),
    },
    ["message", "tweets"],
)
READ_TWEET_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("Whether the post was loaded or not found."),
        "tweet": outputs.nullable(TWEET_SCHEMA, "The post, or null when X returned nothing for that id."),
    },
    ["message", "tweet"],
)
USER_TWEETS_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many posts were returned, or that the user was not found."),
        "tweets": outputs.array_of(TWEET_SCHEMA, "Up to max_results of the user's recent posts; empty when the user was not found."),
    },
    ["message", "tweets"],
)
GET_TRENDS_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many trends were returned, and for which place."),
        "trends": outputs.array_of(
            outputs.obj(
                {
                    "trend_name": outputs.text("The trending topic as X names it."),
                    "tweet_count": outputs.nullable({"type": "integer"}, "Posts in the trend, null when X omits the count."),
                },
                ["trend_name", "tweet_count"],
            ),
            "Up to max_trends topics, in X's order.",
        ),
    },
    ["message", "trends"],
)
GET_PERSONALIZED_TRENDS_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("How many personalized trends were returned."),
        "trends": outputs.array_of(
            outputs.obj(
                {
                    "trend_name": outputs.text("The trending topic as X names it."),
                    "category": outputs.text("Category X assigns the trend, empty when it sends none."),
                    "post_count": outputs.nullable({"type": "integer"}, "Posts in the trend, null when X omits the count."),
                    "trending_since": outputs.text("How long it has been trending, as X phrases it."),
                },
                ["trend_name", "category", "post_count", "trending_since"],
            ),
            "Up to 50 For You trends for the connected account.",
        ),
    },
    ["message", "trends"],
)
LOOKUP_USER_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("Which handle resolved to which user id."),
        "user": outputs.obj(
            {
                "id": outputs.text("Numeric X user id."),
                "username": outputs.text("Handle without the @."),
                "name": outputs.text("Display name, up to 120 characters."),
                "followers_count": outputs.integer("Followers; absent when X withholds the metric."),
                "following_count": outputs.integer("Accounts followed; absent when X withholds the metric."),
                "tweet_count": outputs.integer("Posts published; absent when X withholds the metric."),
            },
            ["id", "username", "name"],
        ),
    },
    ["message", "user"],
)


MANIFEST = ToolManifest(
    tool_id="twitter",
    display_name="X (Twitter)",
    description=(
        "Connect your X account and let your agent search and read X posts, trends, and public "
        "profiles. Replies and direct messages are prepared as X links that you open, review, and "
        "send yourself."
    ),
    connection="oauth",
    actions=(
        ActionSpec(id="search_tweets",
            description="Search public X posts from the last seven days with X query syntax and return post text, author, timestamp, and metrics. Pass start_time or since_id on recurring searches so already-read posts are not billed again. Use get_trends to discover trend names first; reads are billed per post returned.",
            data_policy=X_READ_POLICY,
            input_schema=_schema(
                {
                    "query": {"type": "string", "description": "X search query (up to 512 chars)."},
                    "max_results": {"type": "string", "description": "10-100 (default 10). Reads are billed per post returned."},
                    "start_time": {"type": "string", "description": "Only return posts at or after this RFC3339 UTC second, e.g. 2026-08-20T12:00:00Z; must fall inside X's seven-day recent-search window. Mutually exclusive with since_id."},
                    "since_id": {"type": "string", "description": "Only return posts with a numeric post id greater than this one. Mutually exclusive with start_time."},
                },
                ["query"],
            ),
            output_schema=SEARCH_TWEETS_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="read_tweet",
            description="Read one public X post by numeric post id and return its text, author, timestamp, and metrics. This does not read a thread or timeline.",
            data_policy=X_READ_POLICY,
            input_schema=_schema({"tweet_id": {"type": "string", "description": "Numeric X post id from a URL or another X action result."}}, ["tweet_id"]),
            output_schema=READ_TWEET_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="user_tweets",
            description="Read one public user's recent posts and return text, timestamps, and metrics. Provide exactly one username or numeric user_id; this is that user's timeline, not the connected account's home feed.",
            data_policy=X_READ_POLICY,
            input_schema=_schema(
                {
                    "username": {"type": "string", "description": "X handle with or without @; mutually exclusive with user_id."},
                    "user_id": {"type": "string", "description": "Permanent numeric X user id; mutually exclusive with username."},
                    "max_results": {"type": "string", "description": "5-100 (default 10)."},
                }
            ),
            output_schema=USER_TWEETS_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_trends",
            description="Read public trending topic names and optional post counts for one geographic WOEID, worldwide by default. This returns topics, not posts; follow with search_tweets to find and rank posts about a trend.",
            data_policy=(
                "Read-only. Sends only the requested location id to the X API using the "
                "deployment's app Bearer token and returns public trending topic names "
                "and post counts. Each call is billed to the deployment's X API "
                "pay-per-use credits. The result enters active model context. Runs directly with no approval."
            ),
            input_schema=_schema(
                {
                    "woeid": {"type": "string", "description": "Where-On-Earth id of the place (default 1 = worldwide; e.g. 23424977 = United States, 23424848 = India)."},
                    "max_trends": {"type": "string", "description": "1-50 (default 20)."},
                }
            ),
            output_schema=GET_TRENDS_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_personalized_trends",
            description="Read the connected account's personalized For You trend names, categories, counts, and start times. Requires X Premium and returns topics, not posts; follow with search_tweets for matching public posts.",
            data_policy=X_PERSONALIZED_TRENDS_POLICY,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=GET_PERSONALIZED_TRENDS_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="lookup_user",
            description="Resolve one public X user to their permanent numeric id, handle, display name, and public follower/following/post counts. Provide exactly one username or user_id. This reads a profile, not its posts; use user_tweets for those.",
            data_policy=(
                "Read-only. Sends only the supplied username or user id to the X API "
                "authenticated as the connected account and returns that user's public id, "
                "handle, display name, and public profile counts. Each call is billed to the "
                "deployment's X API pay-per-use credits. The result enters active model "
                "context. Runs directly with no approval."
            ),
            input_schema=_schema(
                {
                    "username": {"type": "string", "description": "X handle with or without @; mutually exclusive with user_id."},
                    "user_id": {"type": "string", "description": "Permanent numeric X user id; mutually exclusive with username."},
                }
            ),
            output_schema=LOOKUP_USER_OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(key="X_OAUTH_CLIENT_ID", description="X developer app OAuth 2.0 client id."),
        ConfigRequirement(key="X_OAUTH_CLIENT_SECRET", description="X developer app OAuth 2.0 client secret (confidential client)."),
        ConfigRequirement(key="X_BEARER_TOKEN", description="X developer app Bearer Token (app-only auth; used by trends lookups, which do not accept user-context tokens)."),
    ),
    protections=(
        "This tool only reads. Nothing is published, sent, or changed on your X account through it, so no action needs your approval.",
        "A reply or direct message the agent prepares is a link: X receives it only when you open that link and send it yourself.",
        "Public reads consume X pay-per-use credits. Your X credentials stay encrypted in write-only tool config.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,),
    agent_notes=(
        "To help the operator post, reply, or send a direct message, draft the text and return a "
        "Markdown link they can open. Post: https://x.com/intent/tweet?text=<percent-encoded-post>. "
        "Reply: that same link plus &in_reply_to=<numeric-post-id>. Direct message: "
        "https://x.com/messages/compose?recipient_id=<numeric-user-id>&text=<percent-encoded-message>, "
        "resolving a handle to that id with lookup_user first. "
        "Percent-encode the text. Chat and generated Web Apps open x.com links in a new tab, so the "
        "operator reviews, edits, and sends the draft there."
    ),
    setup_steps=(
        SetupStep(
            title="Create an X developer project and app",
            description="Sign in to the X Developer Console, create or select the project, and create the app that will own this integration. Enable the current pay-per-use API access and add enough credits for recent search, profile lookups, and trends. Keep this app dedicated enough that its billing and credentials can be revoked without affecting unrelated systems.",
            link_url="https://developer.x.com/",
            link_label="Open the X Developer Portal",
        ),
        SetupStep(
            title="Configure user authentication",
            show_callback=True,
            description="Open the app's User authentication settings and choose Set up or Edit. Enable OAuth 2.0, leave App permissions at Read, and choose Web App, Automated App or Bot so X issues a confidential-client secret. Add the exact callback URI displayed in this guide. If X requires a Website URL, use your public Kern base URL: for a callback such as https://host.example/oauth/callback, use https://host.example. You do not need a separate website. Then save. Kern requests exactly tweet.read, users.read, and offline.access; it requests no write, post, or direct-message scope at all. offline.access lets X issue refresh tokens after the two-hour access token expires.",
            link_url="https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code",
            link_label="View X OAuth 2.0 authorization-code documentation",
        ),
        SetupStep(
            title="Copy all three app values",
            description="Open Keys and tokens for the same app. Copy the OAuth 2.0 Client ID and Client Secret, then copy or regenerate the app-only Bearer Token under Authentication Tokens. Regenerating any value invalidates the old one, so update Kern immediately. The Bearer Token is required for public trend endpoints that do not accept the connected user's token; do not substitute an OAuth 1.0a access-token pair.",
        ),
        SetupStep(
            title="Configure and connect Kern",
            show_config=True,
            description="Open X under Home > Integrations. Save the OAuth 2.0 values as X_OAUTH_CLIENT_ID and X_OAUTH_CLIENT_SECRET and the app-only token as X_BEARER_TOKEN. Enable the tool, choose Connect, sign in as the account the agent may read as, and approve the three displayed scopes. An existing connection keeps working, and you can disconnect and reconnect once to drop the direct-message permissions it was granted before. Confirm the page shows the expected @username. Replies and direct messages use X's official links in your own browser and do not use these credentials.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Reads", text="Search queries, post ids, usernames, trend locations, and paging values go to X directly. Query text is received and logged like any other request, so it is itself data sent to X. The search query first passes the host parameter guard (see Technical notes), which denies secret- or credential-shaped values before the request is sent."),
                    DataSummaryPoint(label="Reply and message drafts", text="Kern never sends a reply or direct message through the API. An agent may put a numeric post or user id and a percent-encoded draft in an official x.com reply-intent or message-compose link; X receives the draft only when you open that link in your browser."),
                    DataSummaryPoint(label="Profile lookups", text="A handle or user id you ask the agent to resolve goes to X and returns that account's public id, handle, display name, and public follower, following, and post counts. This is the same public lookup the X website performs and sends no message text."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="X", text="Reads and the OAuth connection stay within X's services under the connected account."),
                    DataSummaryPoint(label="The public internet", text="Nothing is published or sent by this tool. If you open a prepared link, X shows the draft for you to review, edit, and send yourself."),
                ),
            ),
            DataSummaryCard(
                title="What X can do with it",
                description=(
                    "X processes searches, retrieved content, API activity, and request metadata under its Privacy Policy and "
                    "developer terms: service operation, personalization, analytics, advertising, safety, and legal uses."
                ),
                links=(
                    DataSummaryLink(label="X Privacy Policy", url="https://x.com/en/privacy"),
                    DataSummaryLink(label="X Developer Agreement and Policy", url="https://developer.x.com/en/developer-terms/agreement-and-policy"),
                ),
            ),
            DataSummaryCard(
                title="How long X retains it",
                description=(
                    "X keeps account, security, and API records under its "
                    "Privacy Policy with no single fixed period. Anything you send yourself from a prepared link stays on X until you or X remove it. Disconnect revokes the token where possible and always clears "
                    "the local credential, but does not delete X's own records."
                ),
                links=(
                    DataSummaryLink(label="X Privacy Policy", url="https://x.com/en/privacy"),
                ),
            ),
        ),
    ),
)


def _basic_auth_header(api: HostAPI) -> str:
    raw = f"{api.config['X_OAUTH_CLIENT_ID']}:{api.config['X_OAUTH_CLIENT_SECRET']}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def _token_payload_from_response(token_response: Mapping[str, object], *, fallback_refresh_token: str) -> JSONObject:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("X OAuth token response returned no access token.")
    refresh_token = token_response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = fallback_refresh_token
    expires_in = token_response.get("expires_in")
    scope = token_response.get("scope")
    return {
        "access_token": access_token,
        "expires_at": now() + (expires_in if isinstance(expires_in, int) else DEFAULT_TOKEN_LIFETIME_SECONDS),
        "refresh_token": refresh_token,
        "scope": scope if isinstance(scope, str) else "",
        "token_type": "bearer",
    }


def _is_invalid_grant(body: bytes) -> bool:
    return b"invalid_grant" in body or b"invalid_request" in body


def _fetch_me(access_token: str) -> ConnectionAccount:
    try:
        response = json_request(
            "GET",
            f"{X_API_BASE_URL}/users/me?{encode_query({'user.fields': 'id,name,username'})}",
            headers={"authorization": f"Bearer {access_token}"},
            failure_message="X profile lookup failed.",
            invalid_response_message="X profile lookup returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, "profile lookup") from exc
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("X profile lookup returned an invalid response.")
    user_id = data.get("id")
    username = data.get("username")
    if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
        raise RuntimeError("X did not return a stable account id.")
    label = f"@{username}" if isinstance(username, str) and username else user_id
    return {"id": user_id, "label": label, "scopes": []}


class XCredentialStore:
    """OAuth 2.0 authorization-code + PKCE against X, with rotating single-use
    refresh tokens. The PKCE verifier rides in the HMAC-signed state (see
    shared/oauth2.signed_state for why that is acceptable for a confidential
    client)."""

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        verifier, challenge = pkce_verifier_and_challenge()
        state = signed_state(
            secret=api.config["X_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id, extra={"verifier": verifier}
        )
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": api.config["X_OAUTH_CLIENT_ID"],
                "redirect_uri": params["redirect_uri"],
                "scope": " ".join(X_OAUTH_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {"authorization_url": f"{X_AUTHORIZE_URL}?{query}", "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        state_payload = verify_state(
            params["state"], secret=api.config["X_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id
        )
        verifier = state_payload.get("verifier")
        if not isinstance(verifier, str) or not verifier:
            raise ValueError("Invalid OAuth state.")
        try:
            token_response = json_request(
                "POST",
                X_TOKEN_URL,
                headers={"authorization": _basic_auth_header(api)},
                form={
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "redirect_uri": params["redirect_uri"],
                    "code_verifier": verifier,
                    "client_id": api.config["X_OAUTH_CLIENT_ID"],
                },
                failure_message="X OAuth token exchange failed.",
                invalid_response_message="X OAuth token exchange returned an invalid response.",
            )
        except WebRequestError as exc:
            if exc.status in {400, 401, 403}:
                raise provider_warning(
                    "X",
                    "OAuth token exchange",
                    exc,
                    "X OAuth token exchange was rejected. Check the client credentials, callback URI, authorization code, PKCE settings, and requested scopes.",
                ) from exc
            if exc.status:
                raise provider_warning(
                    "X",
                    "OAuth token exchange",
                    exc,
                    f"X returned HTTP {exc.status} during OAuth token exchange.",
                ) from exc
            known = known_provider_transport_error(exc)
            if known:
                raise RuntimeError(known) from exc
            raise unmapped_provider_error("X", "OAuth token exchange", exc) from None
        granted_scopes = str(token_response.get("scope") or "").split()
        missing = REQUIRED_X_SCOPES - set(granted_scopes)
        if missing:
            raise RuntimeError(
                "X connection is missing required permissions: "
                f"{', '.join(sorted(missing))}. Update the X app permissions and reconnect."
            )
        existing = api.credentials.load()
        token_payload = _token_payload_from_response(token_response, fallback_refresh_token="")
        if not token_payload["refresh_token"]:
            raise RuntimeError("X connection returned no refresh token. Reconnect and grant offline access.")
        access_token = str(token_payload["access_token"])
        identity = _fetch_me(access_token)
        account: ConnectionAccount = {"id": identity["id"], "label": identity["label"], "scopes": granted_scopes}
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        api.credentials.save(
            {
                "account": account,
                "secret": token_payload,
                "metadata": {
                    "created_at": created_at if isinstance(created_at, int) else current_time,
                    "updated_at": current_time,
                },
            }
        )
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        existing = api.credentials.load()
        if existing is not None:
            secret = existing["secret"]
            token = secret.get("refresh_token") or secret.get("access_token")
            if isinstance(token, str) and token:
                try:
                    json_request(
                        "POST",
                        X_REVOKE_URL,
                        headers={"authorization": _basic_auth_header(api)},
                        form={"token": token},
                        failure_message="X token revoke failed.",
                        invalid_response_message="X token revoke returned an invalid response.",
                    )
                except Exception:
                    pass  # Best-effort revoke; the credential is cleared regardless.
        api.credentials.clear()

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_token(self, api: HostAPI) -> str:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        require_scopes(api, existing, REQUIRED_X_SCOPES, reconnect_message=X_RECONNECT_MESSAGE)
        payload = cast(Mapping[str, object], existing["secret"])
        if access_token_is_fresh(payload, now()):
            return str(payload.get("access_token") or "")
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        try:
            token_response = json_request(
                "POST",
                X_TOKEN_URL,
                headers={"authorization": _basic_auth_header(api)},
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": api.config["X_OAUTH_CLIENT_ID"],
                },
                failure_message="X OAuth token refresh failed.",
                invalid_response_message="X OAuth token refresh returned an invalid response.",
            )
        except WebRequestError as exc:
            if exc.status == 400 and _is_invalid_grant(exc.body):
                clear_if_still_loaded(api, existing)
                raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE) from exc
            if exc.status in {400, 401, 403}:
                raise provider_warning(
                    "X",
                    "OAuth token refresh",
                    exc,
                    "X OAuth token refresh was rejected. Check the app credentials and reconnect the account if the token was revoked.",
                ) from exc
            if exc.status:
                raise provider_warning(
                    "X",
                    "OAuth token refresh",
                    exc,
                    f"X returned HTTP {exc.status} during OAuth token refresh.",
                ) from exc
            known = known_provider_transport_error(exc)
            if known:
                raise RuntimeError(known) from exc
            raise unmapped_provider_error("X", "OAuth token refresh", exc) from None
        # X rotates refresh tokens: the one just used is spent, so the new
        # payload (with the newly issued refresh token) must win or the
        # connection is lost. Keep the old one only if none was returned.
        updated_payload = _token_payload_from_response(token_response, fallback_refresh_token=refresh_token)
        refreshed_scope = token_response.get("scope")
        if isinstance(refreshed_scope, str) and REQUIRED_X_SCOPES - set(refreshed_scope.split()):
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        save_if_still_connected(
            api,
            existing,
            {
                "account": existing["account"],
                "secret": updated_payload,
                "metadata": {**existing["metadata"], "updated_at": now()},
            },
            reconnect_message=X_RECONNECT_MESSAGE,
        )
        return str(updated_payload["access_token"])

    def refresh_identity(self, api: HostAPI, access_token: str) -> ConnectionAccount:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        identity = _fetch_me(access_token)
        if existing["account"]["id"] != identity["id"]:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
        return {"id": identity["id"], "label": identity["label"], "scopes": existing["account"]["scopes"]}


X_CREDENTIALS = XCredentialStore()


def _api_get(access_token: str, path_and_query: str, *, what: str) -> JSONObject:
    try:
        return json_request(
            "GET",
            f"{X_API_BASE_URL}{path_and_query}",
            headers={"authorization": f"Bearer {access_token}"},
            failure_message=f"X {what} request failed.",
            invalid_response_message=f"X {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status == 401:
        return IntegrationReconnectRequired(X_RECONNECT_MESSAGE)
    if exc.status == 429:
        message = "X API rate limit was reached."
    elif exc.status == 403:
        message = f"X declined the {what} request (HTTP 403 forbidden)."
    elif exc.status:
        message = f"X API returned HTTP {exc.status} for the {what} request."
    else:
        known = known_provider_transport_error(exc)
        if known:
            return RuntimeError(known)
        return unmapped_provider_error("X", what, exc)
    return provider_warning("X", what, exc, message)


def _usernames_by_id(response: JSONObject) -> dict[str, str]:
    includes = response.get("includes")
    users = includes.get("users") if isinstance(includes, dict) else None
    output: dict[str, str] = {}
    if isinstance(users, list):
        for user in users:
            if isinstance(user, dict) and isinstance(user.get("id"), str) and isinstance(user.get("username"), str):
                output[str(user["id"])] = str(user["username"])
    return output


def _tweet_summary(tweet: JSONObject, usernames: dict[str, str]) -> JSONObject:
    raw_metrics = tweet.get("public_metrics")
    raw_metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    # Named fields rather than X's public_metrics object echoed through: an
    # echoed provider body is a shape the manifest cannot state.
    metrics: JSONObject = {}
    for field, _ in TWEET_METRIC_FIELDS:
        count = raw_metrics.get(field)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            metrics[field] = count
    author_id = tweet.get("author_id")
    author_id_str = author_id if isinstance(author_id, str) else ""
    return {
        "id": str(tweet.get("id") or ""),
        "text": clip_text(str(tweet.get("text") or ""), 1_200),
        "author_id": author_id_str,
        "author_username": usernames.get(author_id_str, ""),
        "created_at": str(tweet.get("created_at") or ""),
        "metrics": metrics,
    }


def _search_tweets(access_token: str, tool_input: JSONObject, api: HostAPI) -> JSONObject:
    extra = set(tool_input) - {"query", "max_results", "start_time", "since_id"}
    if extra:
        raise ToolInputValidationError("X search tool input only supports query, max_results, start_time, and since_id.")
    query = tool_input.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolInputValidationError("X tool_input.query is required.")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputValidationError(
            f"X search query must be at most {MAX_QUERY_CHARS} characters."
        )
    max_results = int_field(tool_input, "max_results", provider="X", default=10, low=10, high=100)
    start_time = tool_input.get("start_time")
    since_id = tool_input.get("since_id")
    if start_time is not None and since_id is not None:
        raise ToolInputValidationError("X search accepts start_time or since_id, not both.")
    if start_time is not None and (not isinstance(start_time, str) or not START_TIME_RE.fullmatch(start_time.strip())):
        raise ToolInputValidationError("X tool_input.start_time must be an RFC3339 UTC second like 2026-08-20T12:00:00Z.")
    query_params = {
        "query": api.outbound.guard_request_parameter_string(query),
        "max_results": str(max_results),
        "tweet.fields": TWEET_FIELDS,
        "expansions": "author_id",
        "user.fields": "username",
    }
    if isinstance(start_time, str):
        query_params["start_time"] = start_time.strip()
    if since_id is not None:
        query_params["since_id"] = _valid_tweet_id(since_id, field="since_id")
    params = encode_query(query_params)
    response = _api_get(access_token, f"/tweets/search/recent?{params}", what="search")
    usernames = _usernames_by_id(response)
    data = response.get("data")
    tweets = [
        cast(JSONValue, _tweet_summary(cast(JSONObject, tweet), usernames))
        for tweet in (data if isinstance(data, list) else [])[:max_results]
        if isinstance(tweet, dict)
    ]
    return {
                "message": f"X search returned {len(tweets)} post(s).",
        "tweets": tweets,
    }


def _read_tweet(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"tweet_id"}
    if extra:
        raise ToolInputValidationError("X read tool input only supports tweet_id.")
    tweet_id = _valid_tweet_id(tool_input.get("tweet_id"), field="tweet_id")
    params = encode_query({"tweet.fields": TWEET_FIELDS, "expansions": "author_id", "user.fields": "username"})
    response = _api_get(access_token, f"/tweets/{tweet_id}?{params}", what="post lookup")
    data = response.get("data")
    if not isinstance(data, dict):
        return {"message": "X post was not found.", "tweet": None}
    return {
                "message": "X post loaded.",
        "tweet": _tweet_summary(cast(JSONObject, data), _usernames_by_id(response)),
    }


def _user_tweets(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"username", "user_id", "max_results"}
    if extra:
        raise ToolInputValidationError("X user posts tool input only supports username, user_id, and max_results.")
    username = tool_input.get("username")
    user_id = tool_input.get("user_id")
    if (username is None) == (user_id is None):
        raise ToolInputValidationError("X user posts require exactly one of tool_input.username or tool_input.user_id.")
    if user_id is not None:
        if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id.strip()):
            raise ToolInputValidationError("X tool_input.user_id must be a numeric id string.")
        resolved_id = user_id.strip()
        resolved_username = ""
    else:
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username.strip()):
            raise ToolInputValidationError("X tool_input.username must be a valid X handle.")
        handle = username.strip().lstrip("@")
        response = _api_get(access_token, f"/users/by/username/{handle}", what="user lookup")
        data = response.get("data")
        resolved = data.get("id") if isinstance(data, dict) else None
        if not isinstance(resolved, str) or not TWEET_ID_RE.fullmatch(resolved):
            return {"message": "X user was not found.", "tweets": []}
        resolved_id = resolved
        resolved_username = handle
    max_results = int_field(tool_input, "max_results", provider="X", default=10, low=5, high=100)
    params = encode_query({"max_results": str(max_results), "tweet.fields": TWEET_FIELDS})
    response = _api_get(access_token, f"/users/{resolved_id}/tweets?{params}", what="user posts")
    data = response.get("data")
    tweets: list[JSONValue] = []
    for tweet in (data if isinstance(data, list) else [])[:max_results]:
        if isinstance(tweet, dict):
            summary = _tweet_summary(cast(JSONObject, tweet), {})
            summary["author_id"] = resolved_id
            summary["author_username"] = resolved_username
            tweets.append(summary)
    return {
                "message": f"X returned {len(tweets)} post(s) for the user.",
        "tweets": tweets,
    }


def _get_trends(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"woeid", "max_trends"}
    if extra:
        raise ToolInputValidationError("X trends tool input only supports woeid and max_trends.")
    woeid = tool_input.get("woeid")
    if woeid is None:
        woeid = WORLDWIDE_WOEID
    if not isinstance(woeid, str) or not WOEID_RE.fullmatch(woeid.strip()):
        raise ToolInputValidationError("X tool_input.woeid must be a numeric WOEID string.")
    max_trends = int_field(tool_input, "max_trends", provider="X", default=20, low=1, high=50)
    params = encode_query({"max_trends": str(max_trends), "trend.fields": "trend_name,tweet_count"})
    # Trends lookups accept only app-only auth, so this action uses the
    # configured app Bearer token instead of the connected user's token. A 401
    # here therefore means the configured token is bad, not that the operator
    # account needs a reconnect.
    try:
        response = json_request(
            "GET",
            f"{X_API_BASE_URL}/trends/by/woeid/{woeid.strip()}?{params}",
            headers={"authorization": f"Bearer {api.config['X_BEARER_TOKEN']}"},
            failure_message="X trends request failed.",
            invalid_response_message="X trends returned an invalid response.",
        )
    except WebRequestError as exc:
        if exc.status == 401:
            message = (
                "X rejected the configured Bearer token. Update X_BEARER_TOKEN "
                "under Home > Integrations in the admin UI."
            )
            raise provider_warning("X", "trends", exc, message) from exc
        raise _mapped_web_error(exc, "trends") from exc
    data = response.get("data")
    trends: list[JSONValue] = []
    for trend in (data if isinstance(data, list) else [])[:max_trends]:
        if isinstance(trend, dict):
            tweet_count = trend.get("tweet_count")
            trends.append(
                {
                    "trend_name": str(trend.get("trend_name") or ""),
                    # tweet_count is optional in the provider response.
                    "tweet_count": tweet_count if isinstance(tweet_count, int) else None,
                }
            )
    place = "worldwide" if woeid.strip() == WORLDWIDE_WOEID else f"WOEID {woeid.strip()}"
    return {
                "message": f"X returned {len(trends)} trending topic(s) for {place}.",
        "trends": trends,
    }


def _personalized_trends(access_token: str, tool_input: JSONObject) -> JSONObject:
    if tool_input:
        raise ToolInputValidationError("X personalized trends take no tool input.")
    params = encode_query({"personalized_trend.fields": "trend_name,category,post_count,trending_since"})
    response = _api_get(access_token, f"/users/personalized_trends?{params}", what="personalized trends")
    data = response.get("data")
    trends: list[JSONValue] = []
    for trend in (data if isinstance(data, list) else [])[:50]:
        if isinstance(trend, dict):
            post_count = trend.get("post_count")
            trends.append(
                {
                    "trend_name": str(trend.get("trend_name") or ""),
                    "category": str(trend.get("category") or ""),
                    "post_count": post_count if isinstance(post_count, int) else None,
                    "trending_since": str(trend.get("trending_since") or ""),
                }
            )
    return {
                "message": f"X returned {len(trends)} personalized (For You) trend(s) for the connected account.",
        "trends": trends,
    }


def _valid_tweet_id(value: JSONValue | None, *, field: str) -> str:
    if not isinstance(value, str) or not TWEET_ID_RE.fullmatch(value.strip()):
        raise ToolInputValidationError(f"X tool_input.{field} must be a numeric post id string.")
    return value.strip()


def _lookup_user(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"username", "user_id"}
    if extra:
        raise ToolInputValidationError("X lookup_user tool input only supports username and user_id.")
    raw_username = tool_input.get("username")
    raw_user_id = tool_input.get("user_id")
    if (raw_username is None) == (raw_user_id is None):
        raise ToolInputValidationError(
            "X lookup_user requires exactly one of tool_input.username or tool_input.user_id."
        )
    username = requested_id = None
    if raw_username is not None:
        if not isinstance(raw_username, str) or not USERNAME_RE.fullmatch(raw_username.strip()):
            raise ToolInputValidationError("X tool_input.username must be a valid X handle.")
        username = raw_username.strip().lstrip("@")
    else:
        if not isinstance(raw_user_id, str) or not USER_ID_RE.fullmatch(str(raw_user_id).strip()):
            raise ToolInputValidationError("X tool_input.user_id must be a numeric user id string.")
        requested_id = str(raw_user_id).strip()
    params = encode_query({"user.fields": "id,name,username,public_metrics"})
    if isinstance(username, str):
        response = _api_get(
            access_token,
            f"/users/by/username/{username}?{params}",
            what="user lookup",
        )
    elif isinstance(requested_id, str):
        response = _api_get(
            access_token,
            f"/users/{requested_id}?{params}",
            what="user lookup",
        )
    else:
        raise ToolInputValidationError("X user lookup input is invalid.")
    data = response.get("data")
    if not isinstance(data, dict):
        raise ToolInputValidationError("The X user was not found.")
    resolved_id = data.get("id")
    resolved_username = data.get("username")
    if (
        not isinstance(resolved_id, str)
        or not USER_ID_RE.fullmatch(resolved_id)
        or (isinstance(requested_id, str) and resolved_id != requested_id)
        or not isinstance(resolved_username, str)
        or not USERNAME_RE.fullmatch(resolved_username)
    ):
        raise ToolInputValidationError("The X user was not found.")
    user: JSONObject = {
        "id": resolved_id,
        "username": resolved_username,
        "name": clip_text(str(data.get("name") or ""), 120),
    }
    public_metrics = data.get("public_metrics")
    if isinstance(public_metrics, dict):
        for field in ("followers_count", "following_count", "tweet_count"):
            count = public_metrics.get(field)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                user[field] = count
    return {
                "message": f"Resolved @{resolved_username} to X user id {resolved_id}.",
        "user": user,
    }


class XTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        # XCredentialStore implements the CredentialFlow protocol directly.
        return X_CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "search_tweets":
                return ActionExecuted(_search_tweets(X_CREDENTIALS.access_token(api), tool_input, api))
            if action == "read_tweet":
                return ActionExecuted(_read_tweet(X_CREDENTIALS.access_token(api), tool_input))
            if action == "user_tweets":
                return ActionExecuted(_user_tweets(X_CREDENTIALS.access_token(api), tool_input))
            if action == "get_trends":
                return ActionExecuted(_get_trends(api, tool_input))
            if action == "get_personalized_trends":
                return ActionExecuted(_personalized_trends(X_CREDENTIALS.access_token(api), tool_input))
            if action == "lookup_user":
                return ActionExecuted(_lookup_user(X_CREDENTIALS.access_token(api), tool_input))
            return ActionFailed("Unsupported X action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except ProviderWarning:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "X tool request failed.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = XTool()
