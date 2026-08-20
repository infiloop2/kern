"""Zoho Mail bundled tool package."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import cast

from host.param_guard import (
    PARAM_GUARD_PROTECTION,
    PARAM_GUARD_TECHNICAL_DETAIL,
    ParamGuardDenied,
)
from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI, StoredCredential
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
    ProviderWarning,
    WebRequestError,
    encode_query,
    json_request,
    known_provider_transport_error,
    provider_warning,
    unmapped_provider_error,
)
from host.tools.tool import (
    ConnectionStatus,
    CredentialFlow,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
)


# Zoho accounts and mail data are partitioned by data centre. Kern requires the
# operator to name the account's data centre explicitly because the host's
# provider-neutral OAuth callback intentionally accepts only code/state and
# does not pass Zoho's additional `location` callback parameter to tools.
ZOHO_DATA_CENTERS: dict[str, tuple[str, str]] = {
    "us": ("https://accounts.zoho.com", "https://mail.zoho.com"),
    "eu": ("https://accounts.zoho.eu", "https://mail.zoho.eu"),
    "in": ("https://accounts.zoho.in", "https://mail.zoho.in"),
    "au": ("https://accounts.zoho.com.au", "https://mail.zoho.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://mail.zoho.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://mail.zohocloud.ca"),
    "cn": ("https://accounts.zoho.com.cn", "https://mail.zoho.com.cn"),
    "ae": ("https://accounts.zoho.ae", "https://mail.zoho.ae"),
    "sa": ("https://accounts.zoho.sa", "https://mail.zoho.sa"),
}
ZOHO_OAUTH_SCOPES = (
    "ZohoMail.accounts.READ",
    "ZohoMail.folders.READ",
    "ZohoMail.messages.READ",
    "ZohoMail.messages.CREATE",
)
REQUIRED_ZOHO_SCOPES = frozenset(ZOHO_OAUTH_SCOPES)
ZOHO_RECONNECT_MESSAGE = "Zoho Mail is no longer connected. Please reconnect the tool."
ZOHO_SEND_ACTION_TYPE = "zoho_mail_propose_send"
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600
MAX_MESSAGE_RESULTS = 50
MAX_FOLDER_RESULTS = 200
MAX_MESSAGE_CONTENT_CHARS = 64_000
MAX_BODY_CHARS = 40_000
MAX_HTML_BODY_CHARS = 80_000
MAX_SUBJECT_CHARS = 500
MAX_RECIPIENTS_PER_FIELD = 50
MAX_LINK_URL_CHARS = 2_048
MAX_APPROVAL_PAYLOAD_BYTES = 64 * 1024
ID_RE = re.compile(r"^[0-9]{1,30}$")
EMAIL_RE = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+$")
HTML_TAG_RE = re.compile(r"<[A-Za-z][^>]*>")


ZOHO_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}
INLINE_SEGMENT_SCHEMA: JSONObject = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "style": {"type": "string", "enum": ["bold", "italic", "bold_italic"]},
            },
            "required": ["text", "style"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"text": {"type": "string"}, "url": {"type": "string"}},
            "required": ["text", "url"],
            "additionalProperties": False,
        },
    ]
}
BODY_BLOCK_SCHEMA: JSONObject = {
    "type": "array",
    "description": (
        "Ordered safe email blocks. Supports paragraphs, line groups, headings, bullet or numbered lists, "
        "rich text with styled/link segments, and dividers; raw HTML is never accepted."
    ),
    "minItems": 1,
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ["paragraph"]}, "text": {"type": "string"}},
                "required": ["type", "text"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["line_group"]},
                    "lines": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["type", "lines"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["heading"]},
                    "level": {"type": "string", "enum": ["1", "2", "3"]},
                    "text": {"type": "string"},
                },
                "required": ["type", "level", "text"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["bullet_list", "numbered_list"]},
                    "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["type", "items"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["rich_text"]},
                    "segments": {"type": "array", "items": INLINE_SEGMENT_SCHEMA, "minItems": 1},
                },
                "required": ["type", "segments"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ["divider"]}},
                "required": ["type"],
                "additionalProperties": False,
            },
        ]
    },
}


MANIFEST = ToolManifest(
    tool_id="zoho_mail",
    display_name="Zoho Mail",
    description=(
        "Connect one Zoho Mail mailbox and let your agent list, search, and read email, "
        "and send safely rendered rich HTML or plaintext messages with your approval."
    ),
    connection="oauth",
    actions=(
        ActionSpec(
            id="search_messages",
            description="Search the connected mailbox with Zoho Mail search syntax and return bounded message summaries.",
            data_policy=(
                "Read-only. Sends the search_key plus paging values to the connected Zoho Mail account. "
                "The search text first passes Kern's parameter guard; the result enters active model context. "
                "Runs directly with no approval."
            ),
            input_schema=_schema(
                {
                    "search_key": {
                        "type": "string",
                        "description": "Zoho Mail search syntax, e.g. sender:alice@example.com::has:attachment.",
                    },
                    "start": {"type": "string", "description": "1-based result offset (default 1)."},
                    "limit": {"type": "string", "description": "1-50 messages (default 20)."},
                },
                ["search_key"],
            ),
            output_schema=ZOHO_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="list_folders",
            description="List folders in the connected Zoho Mail mailbox, including the ids needed by list_messages and read_message.",
            data_policy="Read-only. Sends no agent-supplied values to Zoho. Folder metadata enters active model context. Runs directly with no approval.",
            input_schema=_schema({}),
            output_schema=ZOHO_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="list_senders",
            description=(
                "List the connected mailbox's default sender and verified sender aliases accepted by send_email."
            ),
            data_policy=(
                "Read-only. Sends no agent-supplied values to Zoho. The connected mailbox's default sender and "
                "verified sender aliases enter active model context. Runs directly with no approval."
            ),
            input_schema=_schema({}),
            output_schema=ZOHO_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="list_messages",
            description="List recent messages in one Zoho Mail folder by folder id.",
            data_policy=(
                "Read-only. Sends the folder id and paging values to the connected Zoho Mail account. "
                "Message summaries enter active model context. Runs directly with no approval."
            ),
            input_schema=_schema(
                {
                    "folder_id": {"type": "string", "description": "Numeric folder id returned by list_folders."},
                    "start": {"type": "string", "description": "1-based result offset (default 1)."},
                    "limit": {"type": "string", "description": "1-50 messages (default 20)."},
                },
                ["folder_id"],
            ),
            output_schema=ZOHO_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="read_message",
            description="Read one Zoho Mail message's metadata and body using its folder and message ids.",
            data_policy=(
                "Read-only. Sends only the folder and message ids to the connected Zoho Mail account. "
                "The message metadata and plaintext body enter active model context. Runs directly with no approval."
            ),
            input_schema=_schema(
                {
                    "folder_id": {"type": "string", "description": "Numeric folder id returned by a list or search action."},
                    "message_id": {"type": "string", "description": "Numeric message id returned by a list or search action."},
                },
                ["folder_id", "message_id"],
            ),
            output_schema=ZOHO_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="send_email",
            description="Queue approval to send one safely rendered rich HTML or plaintext email from the connected Zoho mailbox.",
            data_policy=(
                "Queues operator approval before anything is sent. After approval, the exact sender, recipients, "
                "subject, chosen format, and rendered body go to Zoho and are delivered to the named recipients."
            ),
            input_schema=_schema(
                {
                    "from_address": {
                        "type": "string",
                        "description": "Optional verified Zoho sender or alias; defaults to the connected mailbox address.",
                    },
                    "to": {"type": "string", "description": "Recipient or comma-separated recipients."},
                    "cc": {"type": "string", "description": "Optional Cc recipient or comma-separated recipients."},
                    "bcc": {"type": "string", "description": "Optional Bcc recipient or comma-separated recipients."},
                    "subject": {"type": "string", "description": "Email subject."},
                    "mail_format": {
                        "type": "string",
                        "enum": ["html", "plaintext"],
                        "description": "html (default) safely renders structured blocks; plaintext sends their text form.",
                    },
                    "blocks": BODY_BLOCK_SCHEMA,
                },
                ["to", "subject", "blocks"],
            ),
            output_schema=ZOHO_OUTPUT_SCHEMA,
            approval="operator",
        ),
    ),
    config=(
        ConfigRequirement(key="ZOHO_OAUTH_CLIENT_ID", description="Zoho server-based application's OAuth client id."),
        ConfigRequirement(key="ZOHO_OAUTH_CLIENT_SECRET", description="Zoho server-based application's OAuth client secret."),
        ConfigRequirement(
            key="ZOHO_DATA_CENTER",
            description="Lowercase Zoho data-centre code: us, eu, in, au, jp, ca, cn, ae, or sa (use eu for mail.zoho.eu).",
        ),
    ),
    protections=(
        "Mailbox reads stay inside the one Zoho account connected by the operator.",
        "Sending waits for explicit operator approval of the sender, all recipients, subject, and body preview.",
        "Rich messages are rendered only from typed blocks: text is escaped, links require http/https, and raw HTML, images, scripts, styles, and tracking elements are not accepted.",
        "Kern stores OAuth tokens in its encrypted credential store; the agent never receives the tokens or OAuth client secret.",
        "The OAuth grant can read accounts, folders, and messages and create messages; it cannot delete mail or reorganize the mailbox.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        "Kern uses Zoho's OAuth 2.0 authorization-code flow with offline access and refreshes one-hour access tokens. API and token endpoints are pinned to the configured Zoho data centre.",
        "Outgoing HTML is deterministically rendered from structured blocks with escaped text and validated links; plaintext remains available. Incoming HTML is converted to bounded plaintext before it enters model context; provider response objects and links are not passed through wholesale.",
        PARAM_GUARD_TECHNICAL_DETAIL,
    ),
    setup_steps=(
        SetupStep(
            title="Confirm your Zoho data centre",
            description=(
                "Open Zoho Mail and note its hostname. mail.zoho.eu means eu; mail.zoho.com means us. "
                "Kern pins both authorization and Mail API calls to this region, so the code must match the mailbox."
            ),
            link_url="https://www.zoho.com/mail/help/api/getting-started-with-api.html",
            link_label="View Zoho Mail data-centre endpoints",
        ),
        SetupStep(
            title="Create a server-based Zoho OAuth client",
            description=(
                "Open the Zoho API Console for the same data centre (for Europe, api-console.zoho.eu), choose Add Client, "
                "select Server-based Applications, and give the client a recognizable Kern name. Do not use Self Client."
            ),
            link_url="https://api-console.zoho.com/",
            link_label="Open the Zoho API Console",
        ),
        SetupStep(
            title="Register Kern's callback URI",
            description=(
                "Paste the exact callback URI shown below into the client's Authorized Redirect URI and save. "
                "The scheme, hostname, port, path, and trailing slash must match exactly."
            ),
            show_callback=True,
        ),
        SetupStep(
            title="Save the Zoho client values in Kern",
            description=(
                "Copy the Client ID and Client Secret into the matching write-only fields below. Set ZOHO_DATA_CENTER "
                "to the lowercase code from step one (eu for a European mailbox), then save."
            ),
            show_config=True,
        ),
        SetupStep(
            title="Enable and connect the mailbox",
            description=(
                "Enable Zoho Mail, choose Connect, sign in to the mailbox Kern may use, and approve the requested account, "
                "folder, read-message, and create-message permissions. Confirm Kern shows the expected mailbox address."
            ),
            link_url="https://www.zoho.com/mail/help/api/using-oauth-2.html",
            link_label="View Zoho Mail OAuth documentation",
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
                            "Search text, paging values, and message or folder ids go to Zoho Mail. Returned folder metadata, "
                            "message summaries, headers, and bounded plaintext bodies enter active model context."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Sends",
                        text=(
                            "Only after approval, the sender, To/Cc/Bcc recipients, subject, chosen format, and rendered body "
                            "go to Zoho Mail and then to the named recipients."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Zoho", text="OAuth and mailbox API calls stay in the configured Zoho data centre."),
                    DataSummaryPoint(label="Recipients", text="An approved send delivers the message to every visible and blind-copy recipient named in the approval."),
                ),
            ),
            DataSummaryCard(
                title="What Zoho can do with it",
                description=(
                    "Zoho processes mailbox content, OAuth activity, and request metadata to provide Zoho Mail under its Privacy Policy."
                ),
                links=(DataSummaryLink(label="Zoho Privacy Policy", url="https://www.zoho.com/privacy.html"),),
            ),
            DataSummaryCard(
                title="How long Zoho retains it",
                description=(
                    "Mail remains in the Zoho mailbox while the subscription and mailbox retain it. Zoho documents separate "
                    "Trash cleanup and post-deletion recovery periods; disconnecting Kern revokes the token where possible but does not delete mail."
                ),
                links=(
                    DataSummaryLink(
                        label="Zoho Mail deletion and retention policy",
                        url="https://www.zoho.com/mail/help/data-deletion-policy.html",
                    ),
                ),
            ),
        )
    ),
    agent_notes=(
        "Use list_senders to discover the current default sender and verified aliases before setting from_address. "
        "Use list_folders before list_messages. read_message needs both folder_id and message_id from a list or search result. "
        "search_messages accepts Zoho syntax such as sender:alice@example.com::has:attachment. Sending defaults to safe HTML "
        "rendered from paragraph, heading, list, rich-text, link, and divider blocks; set mail_format to plaintext when needed. "
        "Raw HTML, attachments, replies, drafts, deletes, and mailbox organization are not implemented."
    ),
)


def _data_center(api: HostAPI) -> str:
    value = api.config["ZOHO_DATA_CENTER"].strip().lower()
    if value not in ZOHO_DATA_CENTERS:
        raise RuntimeError(
            "ZOHO_DATA_CENTER must be one of us, eu, in, au, jp, ca, cn, ae, or sa under Home > Integrations."
        )
    return value


def _oauth_urls(data_center: str) -> tuple[str, str, str]:
    accounts_base, mail_base = ZOHO_DATA_CENTERS[data_center]
    return f"{accounts_base}/oauth/v2/auth", f"{accounts_base}/oauth/v2/token", mail_base


def _parse_scopes(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [scope for scope in re.split(r"[\s,]+", value.strip()) if scope]


def _token_payload(response: Mapping[str, object], *, fallback_refresh_token: str) -> JSONObject:
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Zoho OAuth token response returned no access token.")
    refresh_token = response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = fallback_refresh_token
    raw_expires_in = response.get("expires_in")
    if isinstance(raw_expires_in, int) and not isinstance(raw_expires_in, bool):
        expires_in = raw_expires_in
    elif isinstance(raw_expires_in, str) and raw_expires_in.isascii() and raw_expires_in.isdecimal():
        expires_in = int(raw_expires_in)
    else:
        expires_in = DEFAULT_TOKEN_LIFETIME_SECONDS
    return {
        "access_token": access_token,
        "expires_at": now() + expires_in,
        "refresh_token": refresh_token,
        "scope": str(response.get("scope") or ""),
        "token_type": str(response.get("token_type") or "Bearer"),
    }


def _is_invalid_grant(body: bytes) -> bool:
    lowered = body.lower()
    return b"invalid_grant" in lowered or b"invalid_code" in lowered


def _mapped_web_error(exc: WebRequestError, what: str) -> Exception:
    if exc.status == 401:
        return IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
    if exc.status == 429:
        message = "Zoho Mail API rate limit was reached."
    elif exc.status == 403:
        message = f"Zoho Mail declined the {what} request (HTTP 403 forbidden). Check the connected permissions and mailbox policy."
    elif exc.status == 400:
        message = f"Zoho Mail rejected the {what} request as invalid."
    elif exc.status:
        message = f"Zoho Mail returned HTTP {exc.status} for the {what} request."
    else:
        known = known_provider_transport_error(exc)
        if known:
            return RuntimeError(known)
        return unmapped_provider_error("Zoho Mail", what, exc)
    return provider_warning("Zoho Mail", what, exc, message)


def _api_request(
    access_token: str,
    data_center: str,
    method: str,
    path: str,
    *,
    what: str,
    query: Mapping[str, str] | None = None,
    body: JSONObject | None = None,
) -> JSONObject:
    _, _, mail_base = _oauth_urls(data_center)
    url = f"{mail_base}/api{path}"
    if query:
        url = f"{url}?{encode_query(query)}"
    try:
        response = json_request(
            method,
            url,
            headers={"authorization": f"Zoho-oauthtoken {access_token}"},
            body=body,
            failure_message=f"Zoho Mail {what} request failed.",
            invalid_response_message=f"Zoho Mail {what} returned an invalid response.",
        )
    except WebRequestError as exc:
        raise _mapped_web_error(exc, what) from exc
    status = response.get("status")
    status_code = status.get("code") if isinstance(status, dict) else None
    if isinstance(status_code, str) and status_code.isascii() and status_code.isdecimal():
        status_code = int(status_code)
    if status_code != 200:
        raise RuntimeError(f"Zoho Mail {what} did not succeed.")
    return response


def _id_value(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else ""


def _required_id(tool_input: JSONObject, field: str) -> str:
    value = _id_value(tool_input.get(field))
    if not value:
        raise ToolInputValidationError(f"Zoho Mail tool_input.{field} must be a numeric id string.")
    return value


def _data_list(response: JSONObject, *, what: str) -> list[JSONObject]:
    data = response.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"Zoho Mail {what} returned invalid data.")
    return [cast(JSONObject, item) for item in data if isinstance(item, dict)]


def _account_addresses(record: JSONObject) -> set[str]:
    addresses: set[str] = set()
    for key in ("mailboxAddress", "primaryEmailAddress", "incomingUserName"):
        value = record.get(key)
        if isinstance(value, str) and EMAIL_RE.fullmatch(value.strip()):
            addresses.add(value.strip().lower())
    raw_addresses = record.get("emailAddress")
    if isinstance(raw_addresses, list):
        for item in raw_addresses:
            if isinstance(item, dict) and item.get("isConfirmed") is True:
                value = item.get("mailId")
                if isinstance(value, str) and EMAIL_RE.fullmatch(value.strip()):
                    addresses.add(value.strip().lower())
    send_details = record.get("sendMailDetails")
    if isinstance(send_details, list):
        for item in send_details:
            if isinstance(item, dict) and item.get("status") is True:
                value = item.get("fromAddress")
                if isinstance(value, str) and EMAIL_RE.fullmatch(value.strip()):
                    addresses.add(value.strip().lower())
    return addresses


def _sender_result(account: ConnectionAccount, record: JSONObject) -> JSONObject:
    default_address = account["label"].lower()
    addresses = sorted(_account_addresses(record) - {default_address})
    addresses.insert(0, default_address)
    return {
        "status": "success_executed",
        "message": f"Zoho Mail returned {len(addresses)} verified sender address(es).",
        "default_from_address": default_address,
        "from_addresses": cast(list[JSONValue], addresses),
    }


def _account_from_record(record: JSONObject, scopes: list[str]) -> ConnectionAccount:
    account_id = _id_value(record.get("accountId"))
    if not account_id:
        raise RuntimeError("Zoho Mail did not return a stable account id.")
    for key in ("mailboxAddress", "primaryEmailAddress", "incomingUserName"):
        label = record.get(key)
        if isinstance(label, str) and EMAIL_RE.fullmatch(label.strip()):
            return {"id": account_id, "label": label.strip(), "scopes": scopes}
    raise RuntimeError("Zoho Mail did not return a mailbox address.")


def _fetch_mail_account(access_token: str, data_center: str, scopes: list[str]) -> tuple[ConnectionAccount, JSONObject]:
    response = _api_request(access_token, data_center, "GET", "/accounts", what="account lookup")
    records = _data_list(response, what="account lookup")
    candidates = [record for record in records if record.get("type") == "ZOHO_ACCOUNT"]
    for record in candidates or records:
        try:
            return _account_from_record(record, scopes), record
        except RuntimeError:
            continue
    raise RuntimeError("Zoho Mail returned no usable hosted mailbox for the connected user.")


class ZohoMailCredentialStore:
    """Zoho authorization-code OAuth with one-hour tokens and offline refresh."""

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        data_center = _data_center(api)
        authorize_url, _, _ = _oauth_urls(data_center)
        state = signed_state(
            secret=api.config["ZOHO_OAUTH_CLIENT_SECRET"],
            tool_id=MANIFEST.tool_id,
            extra={"data_center": data_center},
        )
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": api.config["ZOHO_OAUTH_CLIENT_ID"],
                "redirect_uri": params["redirect_uri"],
                "scope": ",".join(ZOHO_OAUTH_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return {"authorization_url": f"{authorize_url}?{query}", "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        state_payload = verify_state(
            params["state"], secret=api.config["ZOHO_OAUTH_CLIENT_SECRET"], tool_id=MANIFEST.tool_id
        )
        data_center = _data_center(api)
        if state_payload.get("data_center") != data_center:
            raise ValueError("Zoho data centre changed during OAuth. Start Connect again.")
        _, token_url, _ = _oauth_urls(data_center)
        try:
            token_response = json_request(
                "POST",
                token_url,
                form={
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "client_id": api.config["ZOHO_OAUTH_CLIENT_ID"],
                    "client_secret": api.config["ZOHO_OAUTH_CLIENT_SECRET"],
                    "redirect_uri": params["redirect_uri"],
                    "scope": ",".join(ZOHO_OAUTH_SCOPES),
                },
                failure_message="Zoho OAuth token exchange failed.",
                invalid_response_message="Zoho OAuth token exchange returned an invalid response.",
            )
        except WebRequestError as exc:
            if exc.status in {400, 401, 403}:
                raise provider_warning(
                    "Zoho Mail",
                    "OAuth token exchange",
                    exc,
                    "Zoho OAuth token exchange was rejected. Check the data centre, client credentials, callback URI, and authorization code.",
                ) from exc
            known = known_provider_transport_error(exc)
            if known:
                raise RuntimeError(known) from exc
            raise unmapped_provider_error("Zoho Mail", "OAuth token exchange", exc) from None
        # Zoho's documented token response shape does not guarantee a scope
        # field. An explicit scope field is still checked if the provider sends
        # one; otherwise the all-or-nothing consent request is the source of
        # truth and the first API call verifies that the grant is usable.
        response_scopes = _parse_scopes(token_response.get("scope"))
        granted_scopes = response_scopes or list(ZOHO_OAUTH_SCOPES)
        missing = REQUIRED_ZOHO_SCOPES - set(granted_scopes)
        if missing:
            raise RuntimeError(
                "Zoho Mail connection is missing required permissions: " + ", ".join(sorted(missing)) + "."
            )
        token_payload = _token_payload(token_response, fallback_refresh_token="")
        if not token_payload["scope"]:
            token_payload["scope"] = ",".join(granted_scopes)
        if not token_payload["refresh_token"]:
            raise RuntimeError("Zoho Mail connection returned no refresh token. Reconnect and grant offline access.")
        account, _ = _fetch_mail_account(str(token_payload["access_token"]), data_center, granted_scopes)
        existing = api.credentials.load()
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        credential: StoredCredential = {
            "account": account,
            "secret": token_payload,
            "metadata": {
                "created_at": created_at if isinstance(created_at, int) else current_time,
                "updated_at": current_time,
                "data_center": data_center,
            },
        }
        api.credentials.save(credential)
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        existing = api.credentials.load()
        if existing is not None:
            data_center = existing["metadata"].get("data_center")
            token = existing["secret"].get("refresh_token") or existing["secret"].get("access_token")
            if isinstance(data_center, str) and data_center in ZOHO_DATA_CENTERS and isinstance(token, str) and token:
                _, token_url, _ = _oauth_urls(data_center)
                try:
                    json_request(
                        "POST",
                        f"{token_url}/revoke",
                        form={"token": token},
                        failure_message="Zoho OAuth token revoke failed.",
                        invalid_response_message="Zoho OAuth token revoke returned an invalid response.",
                    )
                except Exception:
                    pass
        api.credentials.clear()

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_context(self, api: HostAPI) -> tuple[str, str, StoredCredential]:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
        require_scopes(api, existing, REQUIRED_ZOHO_SCOPES, reconnect_message=ZOHO_RECONNECT_MESSAGE)
        data_center = existing["metadata"].get("data_center")
        if not isinstance(data_center, str) or data_center not in ZOHO_DATA_CENTERS:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
        if _data_center(api) != data_center:
            raise IntegrationReconnectRequired(
                "Zoho Mail's saved connection uses a different data centre. Restore ZOHO_DATA_CENTER or reconnect the tool."
            )
        secret = cast(Mapping[str, object], existing["secret"])
        if access_token_is_fresh(secret, now()):
            return str(secret.get("access_token") or ""), data_center, existing
        refresh_token = secret.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
        _, token_url, _ = _oauth_urls(data_center)
        try:
            token_response = json_request(
                "POST",
                token_url,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": api.config["ZOHO_OAUTH_CLIENT_ID"],
                    "client_secret": api.config["ZOHO_OAUTH_CLIENT_SECRET"],
                },
                failure_message="Zoho OAuth token refresh failed.",
                invalid_response_message="Zoho OAuth token refresh returned an invalid response.",
            )
        except WebRequestError as exc:
            if exc.status in {400, 401} and _is_invalid_grant(exc.body):
                clear_if_still_loaded(api, existing)
                raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE) from exc
            if exc.status in {400, 401, 403}:
                raise provider_warning(
                    "Zoho Mail",
                    "OAuth token refresh",
                    exc,
                    "Zoho OAuth token refresh was rejected. Check the client credentials and reconnect if access was revoked.",
                ) from exc
            known = known_provider_transport_error(exc)
            if known:
                raise RuntimeError(known) from exc
            raise unmapped_provider_error("Zoho Mail", "OAuth token refresh", exc) from None
        refreshed_scopes = _parse_scopes(token_response.get("scope"))
        if refreshed_scopes and REQUIRED_ZOHO_SCOPES - set(refreshed_scopes):
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
        updated_payload = _token_payload(token_response, fallback_refresh_token=refresh_token)
        if not updated_payload["scope"]:
            updated_payload["scope"] = str(existing["secret"].get("scope") or "")
        updated: StoredCredential = {
            "account": existing["account"],
            "secret": updated_payload,
            "metadata": {**existing["metadata"], "updated_at": now()},
        }
        save_if_still_connected(api, existing, updated, reconnect_message=ZOHO_RECONNECT_MESSAGE)
        return str(updated_payload["access_token"]), data_center, updated

    def refresh_account(self, api: HostAPI, access_token: str, data_center: str) -> tuple[ConnectionAccount, JSONObject]:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
        account, record = _fetch_mail_account(access_token, data_center, existing["account"]["scopes"])
        if account["id"] != existing["account"]["id"]:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
        return account, record


ZOHO_CREDENTIALS = ZohoMailCredentialStore()


def _plain(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return clip_text(html.unescape(value).strip(), limit)


def _message_summary(record: JSONObject) -> JSONObject:
    attachment = record.get("hasAttachment")
    has_attachment = attachment is True or str(attachment).lower() in {"1", "true", "yes"}
    return {
        "message_id": _id_value(record.get("messageId")),
        "folder_id": _id_value(record.get("folderId")),
        "thread_id": _id_value(record.get("threadId")),
        "from": _plain(record.get("fromAddress"), 1_000),
        "to": _plain(record.get("toAddress"), 2_000),
        "cc": _plain(record.get("ccAddress"), 2_000),
        "sender": _plain(record.get("sender"), 500),
        "subject": _plain(record.get("subject"), 2_000),
        "summary": _plain(record.get("summary"), 4_000),
        "received_at_ms": str(record.get("receivedTime") or record.get("receivedtime") or "")[:32],
        "sent_at_ms": str(record.get("sentDateInGMT") or "")[:32],
        "read_status": str(record.get("status") or "")[:64],
        "has_attachment": has_attachment,
    }


def _folder_summary(record: JSONObject) -> JSONObject:
    return {
        "folder_id": _id_value(record.get("folderId")),
        "name": _plain(record.get("folderName"), 500),
        "path": _plain(record.get("path"), 1_000),
        "type": _plain(record.get("folderType"), 100),
        "imap_access": record.get("imapAccess") is True,
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag.lower() in {"br", "p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag.lower() in {"p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _message_text(value: object) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    if HTML_TAG_RE.search(value):
        parser = _TextExtractor()
        parser.feed(value)
        raw = "".join(parser.parts)
    else:
        raw = html.unescape(value)
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    normalized = "\n".join(line for line in lines if line).strip()
    truncated = len(normalized) > MAX_MESSAGE_CONTENT_CHARS
    return normalized[:MAX_MESSAGE_CONTENT_CHARS], truncated


def _paging(tool_input: JSONObject) -> tuple[int, int]:
    start = int_field(tool_input, "start", provider="Zoho Mail", default=1, low=1, high=1_000_000)
    limit = int_field(tool_input, "limit", provider="Zoho Mail", default=20, low=1, high=MAX_MESSAGE_RESULTS)
    return start, limit


def _search_messages(access_token: str, data_center: str, tool_input: JSONObject, api: HostAPI) -> JSONObject:
    search_key = tool_input.get("search_key")
    if not isinstance(search_key, str) or not search_key.strip():
        raise ToolInputValidationError("Zoho Mail search requires tool_input.search_key.")
    guarded = api.outbound.guard_request_parameter_string(
        search_key.strip(), allow_identifiers=True
    )
    start, limit = _paging(tool_input)
    response = _api_request(
        access_token,
        data_center,
        "GET",
        f"/accounts/{_connected_account_id(api)}/messages/search",
        what="message search",
        query={"searchKey": guarded, "start": str(start), "limit": str(limit), "includeto": "true"},
    )
    records = _data_list(response, what="message search")[:limit]
    return {
        "status": "success_executed",
        "message": f"Zoho Mail returned {len(records)} message(s).",
        "search_key": guarded,
        "messages": cast(list[JSONValue], [_message_summary(record) for record in records]),
    }


def _connected_account_id(api: HostAPI) -> str:
    existing = api.credentials.load()
    account_id = _id_value(existing["account"].get("id")) if existing is not None else ""
    if not account_id:
        raise IntegrationReconnectRequired(ZOHO_RECONNECT_MESSAGE)
    return account_id


def _list_folders(access_token: str, data_center: str, tool_input: JSONObject, api: HostAPI) -> JSONObject:
    if tool_input:
        raise ToolInputValidationError("Zoho Mail list_folders takes no input.")
    response = _api_request(
        access_token,
        data_center,
        "GET",
        f"/accounts/{_connected_account_id(api)}/folders",
        what="folder listing",
    )
    records = _data_list(response, what="folder listing")[:MAX_FOLDER_RESULTS]
    return {
        "status": "success_executed",
        "message": f"Zoho Mail returned {len(records)} folder(s).",
        "folders": cast(list[JSONValue], [_folder_summary(record) for record in records]),
    }


def _list_messages(access_token: str, data_center: str, tool_input: JSONObject, api: HostAPI) -> JSONObject:
    folder_id = _required_id(tool_input, "folder_id")
    start, limit = _paging(tool_input)
    response = _api_request(
        access_token,
        data_center,
        "GET",
        f"/accounts/{_connected_account_id(api)}/messages/view",
        what="message listing",
        query={"folderId": folder_id, "start": str(start), "limit": str(limit), "includeto": "true"},
    )
    records = _data_list(response, what="message listing")[:limit]
    return {
        "status": "success_executed",
        "message": f"Zoho Mail returned {len(records)} message(s) from folder {folder_id}.",
        "folder_id": folder_id,
        "messages": cast(list[JSONValue], [_message_summary(record) for record in records]),
    }


def _read_message(access_token: str, data_center: str, tool_input: JSONObject, api: HostAPI) -> JSONObject:
    folder_id = _required_id(tool_input, "folder_id")
    message_id = _required_id(tool_input, "message_id")
    account_id = _connected_account_id(api)
    base_path = f"/accounts/{account_id}/folders/{folder_id}/messages/{message_id}"
    details_response = _api_request(
        access_token, data_center, "GET", f"{base_path}/details", what="message metadata"
    )
    content_response = _api_request(
        access_token, data_center, "GET", f"{base_path}/content", what="message content"
    )
    details = details_response.get("data")
    content_data = content_response.get("data")
    if not isinstance(details, dict) or not isinstance(content_data, dict):
        raise RuntimeError("Zoho Mail message read returned invalid data.")
    summary = _message_summary(cast(JSONObject, details))
    content, truncated = _message_text(content_data.get("content"))
    summary["content"] = content
    summary["content_truncated"] = truncated
    return {"status": "success_executed", "message": "Zoho Mail message loaded.", "zoho_message": summary}


def _email_field(tool_input: JSONObject, field: str, *, required: bool) -> str:
    value = tool_input.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
        raise ToolInputValidationError(f"Zoho Mail tool_input.{field} must contain valid email addresses.")
    parts = [part.strip() for part in value.split(",")]
    if len(parts) > MAX_RECIPIENTS_PER_FIELD or any(not EMAIL_RE.fullmatch(part) for part in parts):
        raise ToolInputValidationError(f"Zoho Mail tool_input.{field} must contain valid comma-separated email addresses.")
    return ",".join(parts)


def _safe_link_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if (
        not url
        or len(url) > MAX_LINK_URL_CHARS
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return url


def _escaped_multiline(value: str) -> str:
    return "<br>".join(html.escape(line, quote=False) for line in value.split("\n"))


def _rich_text_bodies(value: object) -> tuple[str, str] | None:
    if not isinstance(value, list) or not value:
        return None
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for segment in value:
        if not isinstance(segment, dict):
            return None
        text = segment.get("text")
        if not isinstance(text, str) or not text:
            return None
        escaped = _escaped_multiline(text)
        if set(segment) == {"text"}:
            plain_parts.append(text)
            html_parts.append(escaped)
        elif set(segment) == {"text", "style"}:
            style = segment.get("style")
            if not isinstance(style, str):
                return None
            tags = {
                "bold": ("<strong>", "</strong>"),
                "italic": ("<em>", "</em>"),
                "bold_italic": ("<strong><em>", "</em></strong>"),
            }.get(style)
            if tags is None:
                return None
            plain_parts.append(text)
            html_parts.append(f"{tags[0]}{escaped}{tags[1]}")
        elif set(segment) == {"text", "url"}:
            url = _safe_link_url(segment.get("url"))
            if not url:
                return None
            plain_parts.append(url if text == url else f"{text} ({url})")
            html_parts.append(
                f'<a href="{html.escape(url, quote=True)}">{escaped}</a>'
            )
        else:
            return None
    plain = "".join(plain_parts).strip()
    rendered_html = "".join(html_parts).strip()
    return (plain, rendered_html) if plain and rendered_html else None


def _bodies_from_blocks(value: JSONValue | None) -> tuple[str, str] | None:
    if not isinstance(value, list) or not value:
        return None
    plain_blocks: list[str] = []
    html_blocks: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        block_type = item.get("type")
        if not isinstance(block_type, str):
            return None
        if block_type == "paragraph" and set(item) == {"type", "text"}:
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            normalized = text.strip()
            plain_blocks.append(normalized)
            html_blocks.append(f"<p>{_escaped_multiline(normalized)}</p>")
        elif block_type == "line_group" and set(item) == {"type", "lines"}:
            lines = item.get("lines")
            if not isinstance(lines, list) or not lines or any(not isinstance(line, str) for line in lines):
                return None
            normalized_lines = [str(line).strip() for line in lines]
            if any(not line for line in normalized_lines):
                return None
            plain_blocks.append("\n".join(normalized_lines))
            escaped_lines = "<br>".join(
                html.escape(line, quote=False) for line in normalized_lines
            )
            html_blocks.append(f"<p>{escaped_lines}</p>")
        elif block_type == "heading" and set(item) == {"type", "level", "text"}:
            level = item.get("level")
            text = item.get("text")
            if (
                not isinstance(level, str)
                or level not in {"1", "2", "3"}
                or not isinstance(text, str)
                or not text.strip()
            ):
                return None
            normalized = text.strip()
            plain_blocks.append(normalized)
            html_blocks.append(f"<h{level}>{_escaped_multiline(normalized)}</h{level}>")
        elif block_type in {"bullet_list", "numbered_list"} and set(item) == {"type", "items"}:
            items = item.get("items")
            if not isinstance(items, list) or not items or any(not isinstance(entry, str) for entry in items):
                return None
            normalized_items = [str(entry).strip() for entry in items]
            if any(not entry for entry in normalized_items):
                return None
            ordered = block_type == "numbered_list"
            plain_blocks.append(
                "\n".join(
                    f"{index}. {entry}" if ordered else f"- {entry}"
                    for index, entry in enumerate(normalized_items, start=1)
                )
            )
            tag = "ol" if ordered else "ul"
            list_items = "".join(
                f"<li>{_escaped_multiline(entry)}</li>" for entry in normalized_items
            )
            html_blocks.append(f"<{tag}>{list_items}</{tag}>")
        elif block_type == "rich_text" and set(item) == {"type", "segments"}:
            bodies = _rich_text_bodies(item.get("segments"))
            if bodies is None:
                return None
            plain_blocks.append(bodies[0])
            html_blocks.append(f"<p>{bodies[1]}</p>")
        elif block_type == "divider" and set(item) == {"type"}:
            plain_blocks.append("---")
            html_blocks.append("<hr>")
        else:
            return None
    plain_body = "\n\n".join(plain_blocks).strip()
    html_body = "\n".join(html_blocks).strip()
    if (
        not plain_body
        or not html_body
        or len(plain_body) > MAX_BODY_CHARS
    ):
        return None
    return plain_body, html_body


def _send_proposal(tool_input: JSONObject, account: ConnectionAccount, account_record: JSONObject) -> JSONObject:
    to = _email_field(tool_input, "to", required=True)
    cc = _email_field(tool_input, "cc", required=False)
    bcc = _email_field(tool_input, "bcc", required=False)
    subject = tool_input.get("subject")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject.strip()) > MAX_SUBJECT_CHARS
        or "\r" in subject
        or "\n" in subject
        or any(ord(character) < 32 and character != "\t" for character in subject)
    ):
        raise ToolInputValidationError(
            f"Zoho Mail tool_input.subject must contain 1-{MAX_SUBJECT_CHARS} characters."
        )
    bodies = _bodies_from_blocks(tool_input.get("blocks"))
    if bodies is None:
        raise ToolInputValidationError(
            "Zoho Mail send requires valid non-empty structured body blocks within the size limits."
        )
    mail_format = tool_input.get("mail_format", "html")
    if not isinstance(mail_format, str) or mail_format not in {"html", "plaintext"}:
        raise ToolInputValidationError("Zoho Mail tool_input.mail_format must be html or plaintext.")
    plain_body, html_body = bodies
    if mail_format == "html" and len(html_body) > MAX_HTML_BODY_CHARS:
        raise ToolInputValidationError(
            f"Zoho Mail rendered HTML body must be at most {MAX_HTML_BODY_CHARS} characters."
        )
    content = html_body if mail_format == "html" else plain_body
    raw_from = tool_input.get("from_address")
    if raw_from is None:
        from_address = account["label"]
    elif isinstance(raw_from, str) and EMAIL_RE.fullmatch(raw_from.strip()):
        from_address = raw_from.strip()
    else:
        raise ToolInputValidationError("Zoho Mail tool_input.from_address must be a valid email address.")
    if from_address.lower() not in _account_addresses(account_record):
        raise ToolInputValidationError("Zoho Mail from_address is not a verified sender on the connected account.")
    message: JSONObject = {
        "fromAddress": from_address,
        "toAddress": to,
        "subject": subject.strip(),
        "content": content,
        "mailFormat": mail_format,
    }
    if cc:
        message["ccAddress"] = cc
    if bcc:
        message["bccAddress"] = bcc
    disclosures = [f"to {clip_text(to, 70)}"]
    if cc:
        disclosures.append(f"cc {clip_text(cc, 45)}")
    if bcc:
        disclosures.append(f"bcc {clip_text(bcc, 45)}")
    normalized_body = " ".join(plain_body.split())
    summary = (
        f"Send Zoho Mail message from {clip_text(from_address, 60)} {', '.join(disclosures)} "
        f"as {mail_format} with subject \"{clip_text(subject.strip(), 50)}\"; "
        f"text preview ({len(normalized_body)} chars): "
        f"\"{clip_text(normalized_body, 80)}\"."
    )
    return {"message": message, "summary": summary}


def _approval_payload(proposal: JSONObject, account: ConnectionAccount) -> JSONObject:
    payload: JSONObject = {
        "action_type": ZOHO_SEND_ACTION_TYPE,
        "tool_id": MANIFEST.tool_id,
        "zoho_account": {"id": account["id"], "label": account["label"]},
        "proposal": proposal,
    }
    serialized = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_APPROVAL_PAYLOAD_BYTES:
        raise ToolInputValidationError(
            "Zoho Mail message is too large to queue for approval; shorten the body or recipient fields."
        )
    return payload


class ZohoMailTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return ZOHO_CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if MANIFEST.action(action) is None:
                return ActionFailed("Unsupported Zoho Mail action.")
            access_token, data_center, _ = ZOHO_CREDENTIALS.access_context(api)
            if action == "search_messages":
                return ActionExecuted(_search_messages(access_token, data_center, tool_input, api))
            if action == "list_folders":
                return ActionExecuted(_list_folders(access_token, data_center, tool_input, api))
            if action == "list_senders":
                if tool_input:
                    raise ToolInputValidationError("Zoho Mail list_senders takes no input.")
                account, account_record = ZOHO_CREDENTIALS.refresh_account(api, access_token, data_center)
                return ActionExecuted(_sender_result(account, account_record))
            if action == "list_messages":
                return ActionExecuted(_list_messages(access_token, data_center, tool_input, api))
            if action == "read_message":
                return ActionExecuted(_read_message(access_token, data_center, tool_input, api))
            if action == "send_email":
                account, account_record = ZOHO_CREDENTIALS.refresh_account(api, access_token, data_center)
                proposal = _send_proposal(tool_input, account, account_record)
                payload = _approval_payload(proposal, account)
                approval = api.approvals.request(
                    action_id=action,
                    summary=str(proposal["summary"]),
                    payload=payload,
                )
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionFailed("Unsupported Zoho Mail action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except ParamGuardDenied as exc:
            return ActionFailed(str(exc))
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except ProviderWarning:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Zoho Mail tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            payload = approval.payload
            if payload.get("action_type") != ZOHO_SEND_ACTION_TYPE or approval.action_id != "send_email":
                return ActionFailed("Zoho Mail approval payload is invalid.")
            proposal = payload.get("proposal")
            approved_account = payload.get("zoho_account")
            if not isinstance(proposal, dict) or not isinstance(approved_account, dict):
                return ActionFailed("Zoho Mail approval payload is invalid.")
            message = proposal.get("message")
            if not isinstance(message, dict):
                return ActionFailed("Zoho Mail approval payload is invalid.")
            access_token, data_center, _ = ZOHO_CREDENTIALS.access_context(api)
            current_account, account_record = ZOHO_CREDENTIALS.refresh_account(api, access_token, data_center)
            if approved_account.get("id") != current_account["id"]:
                return ActionFailed("Zoho Mail account changed after approval. Please queue a new approval.")
            from_address = message.get("fromAddress")
            if not isinstance(from_address, str) or from_address.lower() not in _account_addresses(account_record):
                return ActionFailed("Zoho Mail sender changed after approval. Please queue a new approval.")
            _api_request(
                access_token,
                data_center,
                "POST",
                f"/accounts/{current_account['id']}/messages",
                what="message send",
                body=cast(JSONObject, message),
            )
            to_address = str(message.get("toAddress") or "the approved recipient")
            return ApprovalExecuted(f"Sent Zoho Mail message from {from_address} to {to_address}.")
        except IntegrationReconnectRequired as exc:
            return ActionFailed(str(exc), reconnect_required=True)
        except ProviderWarning:
            raise
        except Exception as exc:
            return ActionFailed(str(exc) or "Zoho Mail send failed after approval.")


BUNDLED_TOOL = ZohoMailTool()
