"""Instagram tool package (Instagram API with Instagram Login)."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Mapping
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalResult,
)
from host.tools.tool import (
    ConnectionStatus,
    CredentialFlow,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
)
from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI
from host.tools.shared.inputs import ToolInputValidationError, clip_text, int_field
from host.tools.shared.oauth2 import (
    IntegrationReconnectRequired,
    access_token_is_fresh,
    clear_if_still_loaded,
    now,
    require_scopes,
    save_if_still_connected,
    signed_state,
    verify_state,
)
from host.tools.shared.web import (
    UnmappedProviderError,
    WebRequestError,
    encode_query,
    json_request,
    known_provider_transport_error,
    stream_request_bytes,
    unmapped_provider_error,
)

IG_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH_BASE_URL = "https://graph.instagram.com"
IG_GRAPH_VERSION = "v25.0"
IG_OAUTH_SCOPES = ("instagram_business_basic", "instagram_business_content_publish")
REQUIRED_IG_SCOPES = frozenset(IG_OAUTH_SCOPES)
IG_RECONNECT_MESSAGE = "Instagram is no longer connected. Please reconnect the tool."
LONG_LIVED_TOKEN_LIFETIME_SECONDS = 60 * 24 * 3600
# Long-lived tokens refresh in place while still valid; refresh opportunistically
# once under this threshold so a quiet fortnight cannot silently expire the token.
REFRESH_THRESHOLD_SECONDS = 14 * 24 * 3600
MAX_CAPTION_CHARS = 2_200
MEDIA_ID_RE = re.compile(r"^[0-9]{1,30}$")
PUBLISH_POLL_ATTEMPTS = 8
PUBLISH_POLL_DELAY_SECONDS = 15
IG_READ_POLICY = (
    "Read-only. Fetches the connected professional account's own profile or "
    "media data from Instagram and returns it to the host and active model context. "
    "Runs directly with no approval."
)


IG_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="instagram",
    display_name="Instagram",
    description="Connect your professional Instagram account and let your agent read its posts and performance, and publish Reel videos with your approval.",
    connection="oauth",
    actions=(
        ActionSpec(id="get_profile",
            description="Read only the connected professional Instagram account's user id, username, account type, follower count, and media count. This cannot inspect another account.",
            data_policy=IG_READ_POLICY,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=IG_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_recent_media",
            description="Read up to 25 recent posts from the connected account with captions, permalinks, timestamps, and like/comment counts. This measures that account's own performance; it is not public-post, hashtag, audio, or global trend discovery.",
            data_policy=IG_READ_POLICY,
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "string", "description": "1-25 (default 10)."}},
                "additionalProperties": False,
            },
            output_schema=IG_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="get_publishing_limit",
            description="Read how many API-published posts the connected account has used from Instagram's 100-post rolling 24-hour publishing quota. This is quota status, not media analytics.",
            data_policy=IG_READ_POLICY,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=IG_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="post_reel",
            description="Queue approval to publish one Reel to the connected Instagram account using a video from the agent workspace. Nothing reaches Instagram before approval; this action does not discover media.",
            data_policy=(
                "Publishes a video as a Reel on the connected Instagram account, publicly "
                "visible per the account's settings. After approval, the tools service uploads "
                "the privately staged video bytes to Meta. Queued for explicit approval; nothing reaches "
                "Instagram until you approve. The proposal and sanitized publication outcome "
                "are available to the active model; staged binary video is not model context."
            ),
            input_schema={
                "type": "object",
                "required": ["video_asset_id"],
                "properties": {
                    "video_asset_id": {"type": "string", "description": "Internal reference for the video uploaded from the agent workspace."},
                    "caption": {"type": "string", "description": "Caption (up to 2200 chars)."},
                    "share_to_feed": {"type": "boolean", "description": "Also show in the main feed (default true)."},
                },
                "additionalProperties": False,
            },
            output_schema=IG_OUTPUT_SCHEMA,
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(key="INSTAGRAM_APP_ID", description="Instagram app id (Meta developer app, Instagram API with Instagram Login)."),
        ConfigRequirement(key="INSTAGRAM_APP_SECRET", description="Instagram app secret."),
    ),
    protections=(
        "Your Instagram app credentials and connected-account OAuth tokens stay in the host credential store and are never returned to or read by the agent.",
        "OAuth connects one professional Instagram account and requests only profile, media, and publishing permissions. Reads are limited to that connected account; public discovery is a separate tool.",
        "Publishing happens only after your approval.",
    ),
    setup_steps=(
        SetupStep(
            title="Prepare a professional Instagram account",
            description="In the Instagram mobile app, switch to the profile you want the agent to use. Look for View professional dashboard near the top of the profile. If it is present, the account is already professional and no conversion is needed. To see whether it is Business or Creator, open the profile menu (☰) > Settings and activity; under For professionals, look for Business tools and controls or Creator tools and controls. Some app versions place this under Account or Preferences. If View professional dashboard is missing, the account is personal; complete the next step to convert it. Record the username so you can verify the same identity after Connect.",
            link_url="https://www.facebook.com/help/instagram/257516379077270",
            link_label="Check Meta's professional dashboard guide",
        ),
        SetupStep(
            title="Convert a personal account if needed",
            description="If View professional dashboard is missing, open the profile menu (☰) > Settings and activity > For professionals > Account type and tools > Switch to professional account. Continue through the introduction, choose the category that best describes the profile, then choose Business or Creator and finish the prompts. Professional accounts are public. A Facebook Page is optional for this Instagram Login integration.",
            link_url="https://www.facebook.com/help/instagram/138925576505882",
            link_label="Review professional account types",
        ),
        SetupStep(
            title="Create the Meta developer app",
            description="Open My Apps in Meta for Developers and choose Create App. If Meta asks for a use case, choose Other; if it asks for an app type, choose Business. Name the app, add the contact email, and finish creation. In the new app's Dashboard, find Add products to your app, locate Instagram, and choose Set up. In the left sidebar, open Instagram > API setup with Instagram login. Do not choose API setup with Facebook login; that is a different integration and requires a Facebook Page.",
            link_url="https://developers.facebook.com/apps/",
            link_label="Open My Apps in Meta for Developers",
        ),
        SetupStep(
            title="Keep Development mode and add the Instagram tester",
            description="Use Development mode for this setup; new Meta apps start there, so leave the App mode switch at Development. In the app's left sidebar, open App roles > Roles, choose Add people, select Instagram Tester, enter the exact professional Instagram username, and choose Add. Then sign in to that Instagram account, open Settings > Website permissions > Apps and websites > Tester invites, and accept this app. Only accepted app-role accounts can connect while the app remains in Development mode. Live mode is needed only if you later complete Meta App Review and connect accounts that are not app roles.",
            link_url="https://www.instagram.com/accounts/manage_access/",
            link_label="Open Instagram tester invitations",
        ),
        SetupStep(
            title="Copy the Instagram credentials and register the callback",
            show_callback=True,
            description="Stay inside the same Meta app and open Instagram > API setup with Instagram login in the left sidebar. Copy the Instagram App ID and Instagram App Secret shown on that page; these are the values Kern uses. On the same page, find Set up Instagram business login and open Business login settings. Paste the exact callback URI displayed in this guide into Valid OAuth Redirect URIs, then save changes. If Client OAuth Login and Web OAuth Login switches are shown, leave both enabled. A different scheme, host, port, path, or trailing slash causes Meta to reject Connect. Kern requests only instagram_business_basic and instagram_business_content_publish.",
        ),
        SetupStep(
            title="Configure and connect Kern",
            show_config=True,
            description="Open Instagram under Home > Integrations. Save the Instagram App ID as INSTAGRAM_APP_ID and Instagram App Secret as INSTAGRAM_APP_SECRET, enable the tool, choose Connect, sign in to the intended professional account, and approve the displayed scopes. The page shows the connected username automatically; confirm it matches the username recorded above.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Reads", text="Only the connected account id and bounded field and limit parameters; reads cover your own professional account, not other accounts."),
                    DataSummaryPoint(label="Publishing", text="A Reel reaches Meta only after your approval, and sends the caption and video uploaded from the agent workspace."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Meta", text="Everything goes to Meta's Instagram Graph API under the connected account."),
                    DataSummaryPoint(label="The public internet", text="An approved Reel is published on Instagram under the account's audience settings and stays there until removed."),
                ),
            ),
            DataSummaryCard(
                title="What Meta can do with it",
                description=(
                    "Meta processes the login, API requests, and published content under its Privacy Policy and platform terms, "
                    "the same as content you post in the Instagram app."
                ),
                links=(
                    DataSummaryLink(label="Instagram Privacy Policy", url="https://privacycenter.instagram.com/policy/"),
                    DataSummaryLink(label="Meta Platform Terms", url="https://developers.facebook.com/terms/"),
                ),
            ),
            DataSummaryCard(
                title="How long Meta retains it",
                description=(
                    "Published content stays on Instagram until the account or Meta removes it, and Meta keeps account and "
                    "security records under its policy. Disconnect clears the local token but not Meta's records."
                ),
                links=(
                    DataSummaryLink(label="Instagram Privacy Policy", url="https://privacycenter.instagram.com/policy/"),
                ),
            ),
        ),
    ),
    # Nothing to add beyond the description: it drives this tool on its own.
    agent_notes="",
)


def _graph_url(path: str, params: Mapping[str, str]) -> str:
    return f"{IG_GRAPH_BASE_URL}/{IG_GRAPH_VERSION}{path}?{encode_query(params)}"


def _meta_error_code(body: bytes) -> int:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    error = decoded.get("error") if isinstance(decoded, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else 0


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    code = _meta_error_code(exc.body)
    if exc.status == 401 or code == 190:
        return IntegrationReconnectRequired(IG_RECONNECT_MESSAGE)
    if exc.status == 429 or code in {4, 9, 17, 613}:
        message = "Instagram API rate limit was reached."
    elif code == 9007 or code == 2207026:
        message = f"Instagram could not process the video for the {what} request (format or fetch problem)."
    elif exc.status:
        message = f"Instagram API returned HTTP {exc.status} for the {what} request."
    else:
        known = known_provider_transport_error(exc)
        if known:
            return RuntimeError(known)
        return unmapped_provider_error("Instagram", what, exc)
    return RuntimeError(message)


def _connect_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status in {400, 401, 403}:
        return RuntimeError(
            f"Instagram {what} was rejected. Check the app credentials, callback URI, authorization code, and permissions."
        )
    if exc.status == 429:
        return RuntimeError(f"Instagram rate-limited the {what}.")
    if exc.status:
        return RuntimeError(f"Instagram returned HTTP {exc.status} during {what}.")
    known = known_provider_transport_error(exc)
    if known:
        return RuntimeError(known)
    return unmapped_provider_error("Instagram", what, exc)


def _graph_get(access_token: str, path: str, params: dict[str, str], *, what: str) -> JSONObject:
    try:
        return json_request(
            "GET",
            _graph_url(path, {**params, "access_token": access_token}),
            failure_message=f"Instagram {what} request failed.",
            invalid_response_message=f"Instagram {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc


def _fetch_me(access_token: str) -> JSONObject:
    return _graph_get(
        access_token,
        "/me",
        {"fields": "user_id,username,name,account_type,followers_count,media_count"},
        what="profile lookup",
    )


def _account_from_me(me: JSONObject, scopes: list[str]) -> ConnectionAccount:
    user_id = me.get("user_id") or me.get("id")
    username = me.get("username")
    if not isinstance(user_id, str) or not MEDIA_ID_RE.fullmatch(user_id):
        raise RuntimeError("Instagram did not return a stable account id.")
    label = f"@{username}" if isinstance(username, str) and username else user_id
    return {"id": user_id, "label": label, "scopes": scopes}


class InstagramCredentialStore:
    """Business Login for Instagram: code -> short-lived token -> long-lived
    (60-day) token, refreshed in place while still valid."""

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        state = signed_state(secret=api.config["INSTAGRAM_APP_SECRET"], tool_id=MANIFEST.tool_id)
        query = urllib.parse.urlencode(
            {
                "client_id": api.config["INSTAGRAM_APP_ID"],
                "redirect_uri": params["redirect_uri"],
                "response_type": "code",
                "scope": ",".join(IG_OAUTH_SCOPES),
                "state": state,
            }
        )
        return {"authorization_url": f"{IG_AUTHORIZE_URL}?{query}", "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        verify_state(params["state"], secret=api.config["INSTAGRAM_APP_SECRET"], tool_id=MANIFEST.tool_id)
        try:
            token_response = json_request(
                "POST",
                IG_TOKEN_URL,
                form={
                    "client_id": api.config["INSTAGRAM_APP_ID"],
                    "client_secret": api.config["INSTAGRAM_APP_SECRET"],
                    "grant_type": "authorization_code",
                    "redirect_uri": params["redirect_uri"],
                    # Instagram appends #_ to the redirect; the host strips
                    # fragments before the tool sees the code.
                    "code": params["code"],
                },
                failure_message="Instagram OAuth token exchange failed.",
                invalid_response_message="Instagram OAuth token exchange returned an invalid response.",
            )
        except WebRequestError as exc:
            raise _connect_web_error(exc, "OAuth token exchange") from exc
        short_lived = _short_lived_token(token_response)
        granted_scopes = _granted_permissions(token_response)
        missing = REQUIRED_IG_SCOPES - set(granted_scopes)
        if missing:
            # Nothing was saved yet, so an already-connected account survives a
            # reconnect the user under-approved.
            raise RuntimeError(
                "Instagram connection is missing required permissions: "
                f"{', '.join(sorted(missing))}. Reconnect and approve every displayed permission."
            )
        try:
            long_lived = json_request(
                "GET",
                f"{IG_GRAPH_BASE_URL}/access_token?"
                + encode_query(
                    {
                        "grant_type": "ig_exchange_token",
                        "client_secret": api.config["INSTAGRAM_APP_SECRET"],
                        "access_token": short_lived,
                    }
                ),
                failure_message="Instagram long-lived token exchange failed.",
                invalid_response_message="Instagram long-lived token exchange returned an invalid response.",
            )
        except WebRequestError as exc:
            raise _connect_web_error(exc, "long-lived token exchange") from exc
        access_token = long_lived.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Instagram long-lived token exchange returned no access token.")
        expires_in = long_lived.get("expires_in")
        me = _fetch_me(access_token)
        account = _account_from_me(me, granted_scopes)
        existing = api.credentials.load()
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        api.credentials.save(
            {
                "account": account,
                "secret": {
                    "access_token": access_token,
                    "expires_at": current_time
                    + (expires_in if isinstance(expires_in, int) else LONG_LIVED_TOKEN_LIFETIME_SECONDS),
                    "obtained_at": current_time,
                },
                "metadata": {
                    "created_at": created_at if isinstance(created_at, int) else current_time,
                    "updated_at": current_time,
                },
            }
        )
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        # Instagram Login has no self-serve revoke endpoint; clearing the
        # credential is the disconnect (the user can also remove the app in
        # their Instagram settings).
        api.credentials.clear()

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_token(self, api: HostAPI) -> str:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(IG_RECONNECT_MESSAGE)
        require_scopes(api, existing, REQUIRED_IG_SCOPES, reconnect_message=IG_RECONNECT_MESSAGE)
        payload = cast(Mapping[str, object], existing["secret"])
        if not access_token_is_fresh(payload, now()):
            # Drop the dead token so connection_status stops reporting connected
            # with an expired credential (matching the X tool's behavior).
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(IG_RECONNECT_MESSAGE)
        access_token = str(payload.get("access_token") or "")
        expires_at = payload.get("expires_at")
        obtained_at = payload.get("obtained_at")
        # Refresh in place when the 60-day token is inside the threshold; the
        # refresh endpoint only works on tokens that are >=24h old and still
        # valid, so failures fall back to the current token rather than
        # breaking the call.
        if (
            isinstance(expires_at, int)
            and expires_at - now() < REFRESH_THRESHOLD_SECONDS
            and isinstance(obtained_at, int)
            and now() - obtained_at >= 24 * 3600
        ):
            try:
                refreshed = json_request(
                    "GET",
                    f"{IG_GRAPH_BASE_URL}/refresh_access_token?"
                    + encode_query({"grant_type": "ig_refresh_token", "access_token": access_token}),
                    failure_message="Instagram token refresh failed.",
                    invalid_response_message="Instagram token refresh returned an invalid response.",
                )
                new_token = refreshed.get("access_token")
                new_expires_in = refreshed.get("expires_in")
                if isinstance(new_token, str) and new_token:
                    current_time = now()
                    save_if_still_connected(
                        api,
                        existing,
                        {
                            "account": existing["account"],
                            "secret": {
                                "access_token": new_token,
                                "expires_at": current_time
                                + (new_expires_in if isinstance(new_expires_in, int) else LONG_LIVED_TOKEN_LIFETIME_SECONDS),
                                "obtained_at": current_time,
                            },
                            "metadata": {**existing["metadata"], "updated_at": current_time},
                        },
                        reconnect_message=IG_RECONNECT_MESSAGE,
                    )
                    return new_token
            except IntegrationReconnectRequired:
                raise
            except Exception:
                pass  # Best-effort refresh; the current token is still valid.
        return access_token

    def refresh_identity(self, api: HostAPI, access_token: str) -> ConnectionAccount:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(IG_RECONNECT_MESSAGE)
        account = _account_from_me(_fetch_me(access_token), existing["account"]["scopes"])
        if existing["account"]["id"] != account["id"]:
            raise IntegrationReconnectRequired(IG_RECONNECT_MESSAGE)
        return account


def _short_lived_token(token_response: JSONObject) -> str:
    """The code exchange returns either a flat object or a {"data": [...]}
    wrapper depending on API era; accept both."""
    candidate: JSONValue = token_response.get("access_token")
    if not candidate:
        data = token_response.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            candidate = cast(JSONObject, data[0]).get("access_token")
    if not isinstance(candidate, str) or not candidate:
        raise RuntimeError("Instagram OAuth token exchange returned no access token.")
    return candidate


def _granted_permissions(token_response: JSONObject) -> list[str]:
    """The permissions the user actually granted, from whichever shape the code
    exchange used (`permissions` is a comma-separated string in the flat
    response and a list in the wrapped one). A response that reports none is a
    failed connect, not an assumed full grant: recording the requested scopes
    unverified is exactly the fabricated snapshot this tool now refuses to
    store — later calls would pass the required-scope check and fail at Meta
    instead."""
    candidate: JSONValue = token_response.get("permissions")
    if not candidate:
        data = token_response.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            candidate = cast(JSONObject, data[0]).get("permissions")
    if isinstance(candidate, str):
        granted = [permission.strip() for permission in candidate.split(",") if permission.strip()]
    elif isinstance(candidate, list):
        granted = [permission.strip() for permission in candidate if isinstance(permission, str) and permission.strip()]
    else:
        granted = []
    if not granted:
        raise RuntimeError("Instagram OAuth token exchange reported no granted permissions.")
    return granted


IG_CREDENTIALS = InstagramCredentialStore()


def _profile_result(me: JSONObject) -> JSONObject:
    return {
        "status": "success_executed",
        "message": "Instagram profile loaded.",
        "user_id": str(me.get("user_id") or me.get("id") or ""),
        "username": str(me.get("username") or ""),
        "account_type": str(me.get("account_type") or ""),
        "followers_count": me.get("followers_count") if isinstance(me.get("followers_count"), int) else None,
        "media_count": me.get("media_count") if isinstance(me.get("media_count"), int) else None,
    }


def _recent_media(access_token: str, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"limit"}
    if extra:
        raise ToolInputValidationError("Instagram recent media tool input only supports limit.")
    limit = int_field(tool_input, "limit", provider="Instagram", default=10, low=1, high=25)
    response = _graph_get(
        access_token,
        "/me/media",
        {
            "fields": "id,media_type,media_product_type,caption,permalink,timestamp,like_count,comments_count",
            "limit": str(limit),
        },
        what="media listing",
    )
    data = response.get("data")
    media: list[JSONValue] = []
    for item in (data if isinstance(data, list) else [])[:limit]:
        if not isinstance(item, dict):
            continue
        record = cast(JSONObject, item)
        media.append(
            {
                "id": str(record.get("id") or ""),
                "media_type": str(record.get("media_type") or ""),
                "product_type": str(record.get("media_product_type") or ""),
                "caption": clip_text(str(record.get("caption") or ""), 300),
                "permalink": str(record.get("permalink") or ""),
                "timestamp": str(record.get("timestamp") or ""),
                "like_count": record.get("like_count") if isinstance(record.get("like_count"), int) else None,
                "comments_count": record.get("comments_count") if isinstance(record.get("comments_count"), int) else None,
            }
        )
    return {
        "status": "success_executed",
        "message": f"Instagram returned {len(media)} recent post(s).",
        "media": media,
    }


def _publishing_limit(access_token: str, user_id: str) -> JSONObject:
    response = _graph_get(
        access_token,
        f"/{user_id}/content_publishing_limit",
        {"fields": "quota_usage,config"},
        what="publishing limit",
    )
    data = response.get("data")
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    record = cast(JSONObject, first)
    config = record.get("config")
    quota_total = config.get("quota_total") if isinstance(config, dict) else None
    return {
        "status": "success_executed",
        "message": "Instagram publishing quota loaded.",
        "quota_usage": record.get("quota_usage") if isinstance(record.get("quota_usage"), int) else None,
        "quota_total": quota_total if isinstance(quota_total, int) else None,
    }


def _reel_proposal(tool_input: JSONObject, api: HostAPI) -> JSONObject:
    extra = set(tool_input) - {"video_asset_id", "caption", "share_to_feed"}
    if extra:
        raise ToolInputValidationError(
            "Instagram Reel tool input only supports video_asset_id, caption, and share_to_feed."
        )
    asset_id = tool_input.get("video_asset_id")
    if not isinstance(asset_id, str):
        raise ToolInputValidationError(
            "Instagram post_reel requires a video uploaded from the agent workspace."
        )
    metadata = api.assets.describe(asset_id)
    if metadata.media_type not in {"video/mp4", "video/quicktime"}:
        raise ToolInputValidationError(
            "Instagram post_reel requires an MP4 or MOV video uploaded from the agent workspace."
        )
    asset: JSONObject = {
        "asset_id": metadata.asset_id,
        "filename": metadata.filename,
        "media_type": metadata.media_type,
        "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
    }
    caption = tool_input.get("caption")
    if caption is not None and not isinstance(caption, str):
        raise ToolInputValidationError("Instagram tool_input.caption must be a string.")
    caption_text = (caption or "").strip()
    # Reject rather than silently truncate: a truncated caption would publish
    # text the agent did not intend and the operator could not see in full.
    if len(caption_text) > MAX_CAPTION_CHARS:
        raise ToolInputValidationError(f"Instagram caption must be at most {MAX_CAPTION_CHARS} characters.")
    share_to_feed = tool_input.get("share_to_feed")
    if share_to_feed is not None and not isinstance(share_to_feed, bool):
        raise ToolInputValidationError("Instagram tool_input.share_to_feed must be a boolean.")
    proposal: JSONObject = {
        "caption": caption_text,
        "share_to_feed": True if share_to_feed is None else share_to_feed,
        "video_asset": asset,
    }
    return proposal


def _reel_summary(proposal: JSONObject, account_label: str) -> str:
    video_asset = proposal.get("video_asset")
    if isinstance(video_asset, dict):
        filename = str(video_asset.get("filename") or "video")
        size = video_asset.get("size_bytes")
        digest = str(video_asset.get("sha256") or "")[:12]
        video_ref = f"staged {filename} ({size} bytes, SHA-256 {digest}…)"
    else:
        video_ref = "staged video"
    account_label = clip_text(account_label, 80)
    caption = str(proposal.get("caption") or "")
    # Disclose the full caption length so a summary-only reader knows how much
    # is clipped; the operator can expand the exact payload to read all of it.
    count = f"{len(caption)}-char caption" if caption else "no caption"
    for caption_clip in (240, 140, 80):
        summary = (
            f"Publish an Instagram Reel as {account_label} from {clip_text(video_ref, 150)}"
            f"{' (also to feed)' if proposal.get('share_to_feed') else ''}"
            f", {count} \"{clip_text(caption, caption_clip)}\"."
        )
        if len(summary.encode("utf-8")) <= 500:
            return summary
    return summary


def _publish_reel(access_token: str, user_id: str, proposal: JSONObject, api: HostAPI) -> str:
    create_params = {
        "media_type": "REELS",
        "share_to_feed": "true" if proposal.get("share_to_feed") else "false",
        "access_token": access_token,
    }
    video_asset = proposal.get("video_asset")
    if not isinstance(video_asset, dict):
        raise RuntimeError("Instagram approval payload has no staged video asset.")
    create_params["upload_type"] = "resumable"
    caption = str(proposal.get("caption") or "")
    if caption:
        create_params["caption"] = caption
    try:
        created = json_request(
            "POST",
            _graph_url(f"/{user_id}/media", create_params),
            failure_message="Instagram media container creation failed.",
            invalid_response_message="Instagram media container creation returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, "Reel container") from exc
    container_id = created.get("id")
    if not isinstance(container_id, str) or not MEDIA_ID_RE.fullmatch(container_id):
        raise RuntimeError("Instagram did not return a media container id.")
    upload_uri = created.get("uri")
    asset_id = video_asset.get("asset_id")
    if not isinstance(upload_uri, str) or not _is_meta_upload_uri(upload_uri):
        raise RuntimeError("Instagram did not return a resumable video upload URI.")
    if not isinstance(asset_id, str):
        raise RuntimeError("Instagram approval payload has an invalid video asset id.")
    metadata = api.assets.describe(asset_id)
    if (
        video_asset.get("filename") != metadata.filename
        or video_asset.get("media_type") != metadata.media_type
        or video_asset.get("size_bytes") != metadata.size_bytes
        or video_asset.get("sha256") != metadata.sha256
    ):
        raise RuntimeError("Staged video no longer matches the approved asset.")
    with api.assets.open(asset_id) as source:
        try:
            stream_request_bytes(
                "POST",
                upload_uri,
                headers={
                    "Authorization": f"OAuth {access_token}",
                    "Content-Type": metadata.media_type,
                    "offset": "0",
                    "file_size": str(metadata.size_bytes),
                },
                body=source,
                content_length=metadata.size_bytes,
                failure_message="Instagram resumable video upload failed.",
                timeout=120,
            )
        except WebRequestError as exc:
            raise _mapped_web_error(exc, "Reel upload") from exc
    for _ in range(PUBLISH_POLL_ATTEMPTS):
        status = _graph_get(access_token, f"/{container_id}", {"fields": "status_code"}, what="container status")
        status_code = status.get("status_code")
        if status_code == "FINISHED":
            break
        if status_code in {"ERROR", "EXPIRED"}:
            raise RuntimeError("Instagram could not process the video (container status ERROR).")
        time.sleep(PUBLISH_POLL_DELAY_SECONDS)
    else:
        raise RuntimeError(
            "Instagram is still processing the video; the approval was spent. "
            "Queue a new approval and try again (long videos take a while)."
        )
    try:
        published = json_request(
            "POST",
            _graph_url(f"/{user_id}/media_publish", {"creation_id": container_id, "access_token": access_token}),
            failure_message="Instagram publish failed.",
            invalid_response_message="Instagram publish returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, "publish") from exc
    media_id = published.get("id")
    return media_id if isinstance(media_id, str) else ""


def _is_meta_upload_uri(value: str) -> bool:
    """Accept only Meta's documented Instagram resumable-upload origin.

    The OAuth token is sent in the upload Authorization header, so an arbitrary
    HTTPS URI from a malformed container response must never be followed.
    """
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "rupload.facebook.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path.startswith("/ig-api-upload/")
    )


class InstagramTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        # InstagramCredentialStore implements the CredentialFlow protocol directly.
        return IG_CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "get_profile":
                if tool_input:
                    raise ToolInputValidationError("Instagram get_profile takes no input.")
                return ActionExecuted(_profile_result(_fetch_me(IG_CREDENTIALS.access_token(api))))
            if action == "get_recent_media":
                return ActionExecuted(_recent_media(IG_CREDENTIALS.access_token(api), tool_input))
            if action == "get_publishing_limit":
                if tool_input:
                    raise ToolInputValidationError("Instagram get_publishing_limit takes no input.")
                access_token = IG_CREDENTIALS.access_token(api)
                account = IG_CREDENTIALS.refresh_identity(api, access_token)
                return ActionExecuted(_publishing_limit(access_token, account["id"]))
            if action == "post_reel":
                proposal = _reel_proposal(tool_input, api)
                access_token = IG_CREDENTIALS.access_token(api)
                account = IG_CREDENTIALS.refresh_identity(api, access_token)
                payload: JSONObject = {
                    "action": action,
                    "tool_id": MANIFEST.tool_id,
                    "instagram_account": {"id": account["id"], "label": account["label"]},
                    "proposal": proposal,
                }
                approval = api.approvals.request(
                    action_id=action, summary=_reel_summary(proposal, account["label"]), payload=payload
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported Instagram action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except UnmappedProviderError:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Instagram tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            # The host hands a loaded record: approved, and this tool's own.
            if approval.action_id != "post_reel":
                return ActionFailed("Instagram approval action is invalid.")
            payload = approval.payload
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                return ActionFailed("Instagram approval payload is invalid.")
            access_token = IG_CREDENTIALS.access_token(api)
            current_account = IG_CREDENTIALS.refresh_identity(api, access_token)
            approved_account = payload.get("instagram_account")
            if not isinstance(approved_account, dict):
                return ActionFailed("Instagram approval payload is invalid.")
            if approved_account.get("id") != current_account["id"]:
                return ActionFailed("Instagram account changed after approval. Please queue a new approval.")
            asset = proposal.get("video_asset")
            asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
            media_id = _publish_reel(
                access_token, current_account["id"], cast(JSONObject, proposal), api
            )
            # Keep the source after any failed attempt so another approval that
            # references it is not broken and the agent can retry cleanly.
            if isinstance(asset_id, str):
                api.assets.delete(asset_id)
            suffix = f" (media id {media_id})" if media_id else ""
            return ApprovalExecuted(f"Published a Reel to Instagram as {current_account['label']}{suffix}.")
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except UnmappedProviderError:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Instagram publish failed after approval.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = InstagramTool()
