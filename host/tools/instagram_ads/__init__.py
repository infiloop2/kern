"""Meta Marketing API v25.0: bounded reads, approved new Reel launch and pause.

No request here retries a write. A launch creates under a paused parent and
activates that parent only after readback and a second resource revalidation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

from host.tools.host_api import ApprovalRecord, ConnectionAccount, HostAPI
from host.tools.manifest import ToolManifest
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalResult
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.ads_diagnostics import MAX_ADS_ERROR_BYTES, AdsProviderError, ads_error_body, report_ads_failure
from host.tools.shared.oauth2 import OAuth2CredentialStore, IntegrationReconnectRequired, access_token_is_fresh, clear_if_still_loaded, now, signed_state, verify_state
from host.tools.shared.web import WebRequestError, encode_query, json_request, known_provider_transport_error
from host.tools.tool import CredentialFlow, OAuthStartConnectParams, OAuthStartConnectResult, OAuthCompleteConnectParams, OAuthCompleteConnectResult

from .contract import MANIFEST, LAUNCH_FIELDS, LAUNCH_REQUIRED

GRAPH = "https://graph.facebook.com/v25.0"
INTERACTIVE_BUDGET_SECONDS = 240
SCOPES = ("ads_management", "ads_read", "pages_show_list", "pages_read_engagement", "instagram_basic")
RECONNECT = "Instagram Ads is no longer authorized. Connect again under Home > Integrations > Instagram Ads."
ID_RE = re.compile(r"^[0-9]{1,30}$")
CURSOR_RE = re.compile(r"^[A-Za-z0-9_+=:/.-]{1,1024}$")
# Meta's currency offset is not always the ISO currency exponent (e.g. BHD).
# Source: /documentation/ads-commerce/marketing-api/currencies, v25.0.
OFFSET_ONE = frozenset("CLP COP CRC HUF ISK IDR JPY KRW PYG TWD VND".split())
OFFSET_HUNDRED = frozenset("DZD ARS AUD BHD BDT BOB BGN BRL GBP CAD CNY HRK CZK DKK EGP EUR GTQ HNL HKD INR ILS JOD KES LVL LTL MOP MYR MXN NZD NIO NGN NOK PKR PEN PHP PLN QAR RON RUB SAR RSD SGD SKK ZAR SEK CHF THB TRY AED UAH USD UYU VEF FBZ VES".split())
DSA_COUNTRIES = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE AX GF GP MQ RE MF YT".split())
ACCOUNT_FIELDS = "id,account_id,name,currency,timezone_name,account_status,disable_reason,user_tasks,funding_source,min_daily_budget,default_dsa_beneficiary,default_dsa_payor"
CAMPAIGN_FIELDS = "id,account_id,name,objective,buying_type,status,effective_status,daily_budget,lifetime_budget,spend_cap,special_ad_categories,is_adset_budget_sharing_enabled,issues_info"
ADSET_FIELDS = "id,account_id,campaign_id,name,status,effective_status,lifetime_budget,daily_budget,start_time,end_time,bid_strategy,bid_amount,bid_constraints,billing_event,optimization_goal,optimization_sub_event,destination_type,promoted_object,is_dynamic_creative,is_budget_schedule_enabled,targeting,adset_schedule,pacing_type,frequency_control_specs,daily_spend_cap,daily_min_spend_target,lifetime_spend_cap,lifetime_min_spend_target,dsa_beneficiary,dsa_payor,issues_info"
CREATIVE_FIELDS = "id,account_id,name,object_id,instagram_user_id,source_instagram_media_id,call_to_action,call_to_action_type,link_url,object_url,destination_spec,object_story_spec,body,asset_feed_spec,degrees_of_freedom_spec"
AD_FIELDS = "id,account_id,campaign_id,adset_id,name,status,effective_status,issues_info,ad_review_feedback,creative{" + CREATIVE_FIELDS + "}"
MEDIA_FIELDS = "id,owner,caption,media_type,media_product_type,media_url,permalink,timestamp,boost_eligibility_info"
OBJECTIVES = {
    "WEBSITE_CLICKS": ("OUTCOME_TRAFFIC", "LINK_CLICKS", "WEBSITE"),
    # Follow the dedicated v25.0 profile-visit creation guide. The general
    # Reels table still calls this VISIT_INSTAGRAM_PROFILE; do not auto-switch.
    "PROFILE_VISITS": ("OUTCOME_TRAFFIC", "PROFILE_VISIT", "INSTAGRAM_PROFILE"),
    "ENGAGEMENTS": ("OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT", "ON_POST"),
    "VIDEO_VIEWS": ("OUTCOME_ENGAGEMENT", "THRUPLAY", "ON_VIDEO"),
}


def _json(value: JSONValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise ToolInputValidationError(f"{label} must be a numeric string of 1-30 digits.")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ToolInputValidationError(f"{label} must contain 1-{maximum} characters.")
    return value


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _object(value: object, label: str) -> JSONObject:
    if not isinstance(value, dict):
        raise RuntimeError(f"Meta did not return {label}.")
    return cast(JSONObject, value)


def _rows(value: JSONObject, *, maximum: int) -> list[JSONObject]:
    data = value.get("data")
    if not isinstance(data, list) or len(data) > maximum or any(not isinstance(row, dict) for row in data):
        raise RuntimeError("Meta returned an invalid or oversized page. No rows were silently dropped.")
    return cast(list[JSONObject], data)


def _cursor(value: JSONObject) -> str | None:
    paging = value.get("paging")
    if not isinstance(paging, dict) or not paging.get("next"):
        return None
    cursors = paging.get("cursors")
    after = cursors.get("after") if isinstance(cursors, dict) else None
    if not isinstance(after, str) or CURSOR_RE.fullmatch(after) is None or "://" in after or after.startswith("//"):
        raise RuntimeError("Meta returned another page without a usable cursor. Completeness cannot be established.")
    return after


def _page(tool_input: JSONObject, api: HostAPI) -> dict[str, str]:
    limit = tool_input.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ToolInputValidationError("limit must be an integer from 1 to 20.")
    params = {"limit": str(limit)}
    if "after" in tool_input:
        after = tool_input["after"]
        if not isinstance(after, str) or CURSOR_RE.fullmatch(after) is None or "://" in after or after.startswith("//"):
            raise ToolInputValidationError("after must be an opaque provider cursor, not a URL.")
        api.outbound.guard_request_parameter_string(after, allow_machine_tokens=True)
        params["after"] = after
    return params


def _validate(action: str, tool_input: JSONObject) -> None:
    spec = MANIFEST.action(action)
    if spec is None:
        raise ToolInputValidationError("Unsupported Instagram Ads action.")
    required = _strings(spec.input_schema.get("required"))
    fields = cast(JSONObject, spec.input_schema["properties"])
    if set(tool_input) - set(fields) or set(required) - set(tool_input):
        raise ToolInputValidationError("Missing required inputs or unknown fields for Instagram Ads " + action + ".")
    for key in ("account_id", "campaign_id", "page_id", "instagram_user_id", "media_id"):
        if key in tool_input:
            _id(tool_input[key], key)


def _fingerprint(api: HostAPI, token: str) -> str:
    stored = CREDENTIALS.load_connected(api)
    if stored["secret"].get("access_token") != token:
        raise IntegrationReconnectRequired(RECONNECT)
    return hashlib.sha256(_json({"account": cast(JSONObject, stored["account"]),
        "secret": stored["secret"], "grant_id": stored["metadata"].get("grant_id"),
        "app_id": api.config["INSTAGRAM_ADS_APP_ID"],
        "app_secret_sha256": hashlib.sha256(api.config["INSTAGRAM_ADS_APP_SECRET"].encode()).hexdigest(),
    }).encode()).hexdigest()


def _api_error_message(error: JSONValue) -> str:
    # Provider text can echo tokens, captions or temporary URLs. Only numeric
    # provider codes enter the operator-visible failure.
    details = [f"{key}={error[key]}" for key in ("code", "error_subcode")
               if isinstance(error, dict) and type(error.get(key)) is int]
    label = " (" + ", ".join(details) + ")" if details else ""
    return (f"Meta rejected the request{label}. Inspect Meta Ads Manager for advertiser access, eligibility "
            "and required regional beneficiary/payer declarations. External verification does not guarantee "
            "required per-request declarations are supplied by this integration.")


class _Graph:
    def __init__(self, token: str, app_secret: str, *, api: HostAPI | None = None, expected_connection: str = ""):
        self.token = token
        self.proof = hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()
        self.api = api
        self.loaded_credential = api.credentials.load() if api is not None else None
        self.expected_connection = expected_connection
        self.deadline = time.monotonic() + INTERACTIVE_BUDGET_SECONDS

    def _check_auth_error(self, error: JSONValue) -> None:
        if not isinstance(error, dict) or error.get("code") != 190:
            return
        if self.api is not None:
            loaded = self.loaded_credential
            if loaded is not None and loaded["secret"].get("access_token") == self.token:
                clear_if_still_loaded(self.api, loaded)
        raise IntegrationReconnectRequired(RECONNECT)

    def request(self, method: str, path: str, params: dict[str, str]) -> JSONObject:
        remaining = int(self.deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("Instagram Ads interactive request budget expired.")
        if method == "POST" and self.api is not None and self.expected_connection:
            if _fingerprint(self.api, self.token) != self.expected_connection:
                raise IntegrationReconnectRequired("Instagram Ads credential changed after approval. Request a new approval.")
        fields = {**params, "appsecret_proof": self.proof}
        try:
            result = json_request(method, GRAPH + path + ("?" + encode_query(fields) if method == "GET" else ""),
                headers={"Authorization": "Bearer " + self.token}, form=fields if method == "POST" else None,
                failure_message="Meta Marketing API request failed.", invalid_response_message="Meta returned an unclear response.",
                timeout=min(15, remaining), max_error_bytes=MAX_ADS_ERROR_BYTES)
        except WebRequestError as exc:
            cause = AdsProviderError("Meta Marketing API request failed.", f"{method} {path}", status=exc.status, body=exc.body, body_truncated=exc.body_truncated)
            try:
                body = json.loads(exc.body)
            except (ValueError, UnicodeDecodeError):
                body = None
            error = body.get("error") if isinstance(body, dict) else None
            try:
                self._check_auth_error(error)
            except IntegrationReconnectRequired as auth_error:
                raise auth_error from cause
            if isinstance(error, dict) and type(error.get("code")) is int:
                raise AdsProviderError(_api_error_message(error), f"{method} {path}", status=exc.status, body=exc.body, body_truncated=exc.body_truncated) from exc
            raise exc from cause
        except RuntimeError as exc:
            raise AdsProviderError(str(exc), f"{method} {path}") from exc
        if result.get("error"):
            cause = AdsProviderError(_api_error_message(result["error"]), f"{method} {path}", status=200,
                                     body=ads_error_body(_json(result).encode("utf-8")))
            try:
                self._check_auth_error(result["error"])
            except IntegrationReconnectRequired as auth_error:
                raise auth_error from cause
            raise cause
        return result

    def get(self, path: str, fields: str = "", **params: str) -> JSONObject:
        return self.request("GET", path, {**params, **({"fields": fields} if fields else {})})

    def post(self, path: str, value: JSONObject) -> JSONObject:
        return self.request("POST", path, {key: item if isinstance(item, str) else _json(item) for key, item in value.items()})


def _permissions(graph: _Graph) -> list[str]:
    rows = _rows(graph.get("/me/permissions", limit="100"), maximum=100)
    return sorted(str(row["permission"]) for row in rows if row.get("status") == "granted" and isinstance(row.get("permission"), str))


class MetaCredentialStore(OAuth2CredentialStore):
    reconnect_message = RECONNECT
    required_scopes = frozenset(SCOPES)

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        state = signed_state(secret=api.config["INSTAGRAM_ADS_APP_SECRET"], tool_id=MANIFEST.tool_id,
                             extra={"redirect_uri": params["redirect_uri"]})
        return {"authorization_url": "https://www.facebook.com/v25.0/dialog/oauth?" + encode_query({
            "client_id": api.config["INSTAGRAM_ADS_APP_ID"], "redirect_uri": params["redirect_uri"],
            "response_type": "code", "scope": ",".join(SCOPES), "state": state,
            "auth_type": "rerequest",
        }), "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        verify_state(params["state"], secret=api.config["INSTAGRAM_ADS_APP_SECRET"], tool_id=MANIFEST.tool_id)
        try:
            return self._complete_connect(params, api)
        except Exception as exc:
            report_ads_failure("instagram_ads", "oauth_complete_connect", exc, phase="connection")
            raise

    def _complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        state = verify_state(params["state"], secret=api.config["INSTAGRAM_ADS_APP_SECRET"], tool_id=MANIFEST.tool_id)
        if state.get("redirect_uri") != params["redirect_uri"]:
            raise RuntimeError("Instagram Ads OAuth callback changed.")
        common = {"client_id": api.config["INSTAGRAM_ADS_APP_ID"], "client_secret": api.config["INSTAGRAM_ADS_APP_SECRET"]}
        def exchange(fields: dict[str, str]) -> JSONObject:
            try:
                result = json_request("GET", GRAPH + "/oauth/access_token?" + encode_query({**common, **fields}),
                    failure_message="Meta OAuth exchange failed. Check app credentials, callback and code.",
                    invalid_response_message="Meta OAuth returned an invalid token response.", max_error_bytes=MAX_ADS_ERROR_BYTES)
            except WebRequestError as exc:
                raise AdsProviderError("Meta OAuth exchange failed. Check app credentials, callback and code.",
                                       "GET oauth/access_token", status=exc.status, body=exc.body, body_truncated=exc.body_truncated) from exc
            except RuntimeError as exc:
                raise AdsProviderError(str(exc), "GET oauth/access_token") from exc
            if result.get("error"):
                raise AdsProviderError(_api_error_message(result["error"]), "GET oauth/access_token", status=200,
                                       body=ads_error_body(_json(result).encode("utf-8")))
            return result
        short = exchange({"code": params["code"], "redirect_uri": params["redirect_uri"]})
        token = _text(short.get("access_token"), "Meta access token", 16384)
        long = exchange({"grant_type": "fb_exchange_token", "fb_exchange_token": token})
        token = _text(long.get("access_token"), "Meta long-lived token", 16384)
        expires = long.get("expires_in")
        if type(expires) is not int or expires <= 0:
            raise RuntimeError("Meta did not supply a finite token lifetime.")
        graph = _Graph(token, api.config["INSTAGRAM_ADS_APP_SECRET"])
        identity = graph.get("/me", "id,name")
        scopes = _permissions(graph)
        if self.required_scopes - set(scopes):
            raise RuntimeError("Meta did not grant all required ads/Page/Instagram scopes. Connect again and approve them.")
        account: ConnectionAccount = {"id": _id(identity.get("id"), "Facebook user id"),
            "label": str(identity.get("name") or identity["id"]), "scopes": scopes}
        self.save_connection(api, account, {"access_token": token, "expires_at": now() + expires},
                             metadata={"grant_id": uuid.uuid4().hex})
        return {"account": account}

    def access_token(self, api: HostAPI) -> str:
        stored = self.load_connected(api)
        if not access_token_is_fresh(stored["secret"], now()):
            clear_if_still_loaded(api, stored)
            raise IntegrationReconnectRequired(RECONNECT)
        return str(stored["secret"]["access_token"])


CREDENTIALS = MetaCredentialStore()


def _connection(graph: _Graph, api: HostAPI) -> JSONObject:
    stored = CREDENTIALS.load_connected(api)
    identity = graph.get("/me", "id,name")
    scopes = _permissions(graph)
    if identity.get("id") != stored["account"]["id"] or set(SCOPES) - set(scopes):
        raise IntegrationReconnectRequired(RECONNECT)
    return {"id": identity["id"], "label": str(identity.get("name") or identity["id"]),
            "scopes": cast(list[JSONValue], scopes), "fingerprint": _fingerprint(api, graph.token)}


def _offset(currency: str) -> int | None:
    return 1 if currency in OFFSET_ONE else 100 if currency in OFFSET_HUNDRED else None


def _account(graph: _Graph, account_id: str, *, write: bool = False, launch: bool = False) -> JSONObject:
    account = graph.get("/act_" + account_id, ACCOUNT_FIELDS)
    if account.get("id") != "act_" + account_id or str(account.get("account_id")) != account_id:
        raise ToolInputValidationError("Meta did not confirm the selected ad account.")
    if write:
        if not {"ADVERTISE", "MANAGE"}.intersection(_strings(account.get("user_tasks"))):
            raise ToolInputValidationError("The selected account must grant this user ADVERTISE or MANAGE.")
    if launch:
        if account.get("account_status") != 1:
            raise ToolInputValidationError("The selected advertiser must be active before launch.")
        if not account.get("funding_source") or str(account.get("funding_source")) == "0":
            raise ToolInputValidationError("Meta did not confirm configured billing. Configure it in Ads Manager before launch.")
        if _offset(str(account.get("currency") or "")) is None:
            raise ToolInputValidationError("Account currency has no verified Meta offset in this integration.")
    return account


def _account_result(value: JSONObject) -> JSONObject:
    result: JSONObject = {key: str(value.get(key) or "") for key in ("name", "currency", "timezone_name")}
    result.update({"account_id": str(value.get("account_id") or str(value.get("id") or "").removeprefix("act_")),
        "account_status": value.get("account_status"), "disable_reason": value.get("disable_reason"),
        "user_tasks": cast(list[JSONValue], _strings(value.get("user_tasks"))),
        "billing_configured": bool(value["funding_source"]) and str(value["funding_source"]) != "0" if "funding_source" in value else None,
        "currency_offset": _offset(str(value.get("currency") or "")),
        "minimum_daily_budget": str(value["min_daily_budget"]) if value.get("min_daily_budget") is not None else None,
        "default_dsa_beneficiary": value.get("default_dsa_beneficiary"), "default_dsa_payor": value.get("default_dsa_payor")})
    return result


def _field_state(value: JSONObject, key: str, kind: type) -> str:
    if key not in value:
        return "missing"
    item = value[key]
    if item is None:
        return "null"
    if not isinstance(item, kind) or isinstance(item, list) and any(not isinstance(entry, str) for entry in item):
        return "invalid"
    return "present" if item else "empty"


def _diagnostic_text(value: JSONObject, key: str) -> str | None:
    item = value.get(key)
    return item if isinstance(item, str) else None


def _diagnostic_page(row: JSONObject) -> JSONObject:
    state = _field_state(row, "instagram_business_account", dict)
    linked = row.get("instagram_business_account")
    linked = linked if isinstance(linked, dict) else {}
    type_state = _field_state(linked, "account_type", str)
    reason = "none"
    if state not in {"present", "empty"}:
        reason = "linked_identity_" + state
    elif type_state != "present":
        reason = "account_type_" + ("invalid" if type_state == "empty" else type_state)
    elif linked.get("account_type") not in {"BUSINESS", "MEDIA_CREATOR"}:
        reason = "account_type_unsupported"
    elif any(not isinstance(identifier, str) or ID_RE.fullmatch(identifier) is None
             for identifier in (row.get("id"), linked.get("id"))):
        reason = "invalid_id"
    return {"page_id": _diagnostic_text(row, "id"), "page_name": _diagnostic_text(row, "name"),
            "instagram_user_id": _diagnostic_text(linked, "id"), "username": _diagnostic_text(linked, "username"),
            "account_type": _diagnostic_text(linked, "account_type"), "linked_identity_state": state,
            "account_type_state": type_state, "identity_filter_reason": reason}


def _diagnostic_params(value: JSONObject, api: HostAPI, cursor: str) -> dict[str, str]:
    return _page({"limit": value.get("limit", 10), **({"after": value[cursor]} if cursor in value else {})}, api)


def _diagnostic_user_page(row: JSONObject) -> JSONObject:
    return {**_diagnostic_page(row), "tasks": cast(list[JSONValue], _strings(row.get("tasks"))),
            "tasks_state": _field_state(row, "tasks", list)}


def _diagnostic_edge(graph: _Graph, path: str, fields: str, params: dict[str, str],
                     convert: Callable[[JSONObject], JSONObject], stage: str) -> JSONObject:
    metadata: JSONObject = {"http_status": None, "error_code": None, "error_subcode": None}
    try:
        listing = graph.get(path, fields, **params)
        rows = [convert(row) for row in _rows(listing, maximum=int(params["limit"]))]
        return {"status": "ok", "items": cast(list[JSONValue], rows), "next_cursor": _cursor(listing), **metadata}
    except IntegrationReconnectRequired:
        raise
    except Exception as exc:
        report_ads_failure("instagram_ads", "diagnose_account", exc, phase=stage)
        # Read only typed numeric metadata. Never return exception/provider text.
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen and len(seen) < 16:
            seen.add(id(current))
            if isinstance(current, (AdsProviderError, WebRequestError)):
                if metadata["http_status"] is None and current.status:
                    metadata["http_status"] = current.status
                try:
                    body = json.loads(current.body)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    body = None
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    for source, target in (("code", "error_code"), ("error_subcode", "error_subcode")):
                        if metadata[target] is None and type(error.get(source)) is int:
                            metadata[target] = error[source]
            current = current.__cause__ or current.__context__
        return {"status": "failed", "items": [], "next_cursor": None, **metadata}


def _diagnose_account(graph: _Graph, value: JSONObject, api: HostAPI, connection: JSONObject) -> JSONObject:
    account_id = _id(value["account_id"], "account_id")
    account = _account(graph, account_id)
    prefix = "/act_" + account_id
    pages = _diagnostic_edge(graph, prefix + "/promote_pages", "id,name,instagram_business_account{id,username,account_type}",
                             _diagnostic_params(value, api, "pages_after"), _diagnostic_page, "diagnostic Pages")
    instagram = _diagnostic_edge(graph, prefix + "/connected_instagram_accounts", "id,username",
        _diagnostic_params(value, api, "instagram_after"),
        lambda row: {"instagram_user_id": _diagnostic_text(row, "id"), "username": _diagnostic_text(row, "username")},
        "diagnostic Instagram accounts")
    user_pages = _diagnostic_edge(graph, "/me/accounts", "id,name,tasks,instagram_business_account{id,username,account_type}",
        _diagnostic_params(value, api, "user_pages_after"), _diagnostic_user_page, "diagnostic user Pages")
    return {"message": "Provider evidence only. Missing tasks or filtered Pages do not prove missing Meta access. "
            "pages comes from the advertiser's promote_pages; user_pages comes from the Facebook user's accounts edge. "
            "User Page visibility and Page tasks do not establish access through the selected advertiser. "
            "A failed edge is unavailable, not empty. Each cursor continues only its own edge; no pages are followed automatically. "
            "No ad creation, provider authorization or delivery was tested; launch guards remain unchanged.",
            "connection": {"facebook_user_id": connection["id"], "name": connection["label"], "scopes": connection["scopes"]},
            "account": _account_result(account), "user_tasks_state": _field_state(account, "user_tasks", list),
            "launch_task_check_passes": bool({"ADVERTISE", "MANAGE"}.intersection(_strings(account.get("user_tasks")))),
            "pages": pages, "instagram_accounts": instagram, "user_pages": user_pages}


def _identity(graph: _Graph, account_id: str, page_id: str, instagram_id: str) -> JSONObject:
    pages = graph.get("/act_" + account_id + "/promote_pages", "id,name,instagram_business_account{id,username,account_type}", limit="100")
    match = next((row for row in _rows(pages, maximum=100) if row.get("id") == page_id), None)
    if match is None:
        raise ToolInputValidationError("Page advertising access was not confirmed within the bounded 100-Page check.")
    linked = _object(match.get("instagram_business_account"), "the Page's linked professional Instagram identity")
    if linked.get("id") != instagram_id or linked.get("account_type") not in {"BUSINESS", "MEDIA_CREATOR"}:
        raise ToolInputValidationError("Page must be linked to the selected real professional Instagram identity.")
    accounts = graph.get("/act_" + account_id + "/connected_instagram_accounts", "id,username", limit="100")
    if not any(row.get("id") == instagram_id for row in _rows(accounts, maximum=100)):
        raise ToolInputValidationError("Instagram advertising access was not confirmed within the bounded 100-identity check.")
    username = linked.get("username")
    if not isinstance(username, str) or re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username) is None:
        raise RuntimeError("Meta did not return a valid professional Instagram username.")
    return {"page_id": page_id, "page_name": str(match.get("name") or ""), "instagram_user_id": instagram_id,
            "username": username, "account_type": linked["account_type"]}


def _media(graph: _Graph, media_id: str, instagram_id: str) -> JSONObject:
    media = graph.get("/" + media_id, MEDIA_FIELDS)
    owner = media.get("owner")
    if media.get("id") != media_id or not isinstance(owner, dict) or owner.get("id") != instagram_id:
        raise ToolInputValidationError("Meta did not prove that the source media belongs to the selected Instagram identity.")
    if media.get("media_type") != "VIDEO" or media.get("media_product_type") != "REELS":
        raise ToolInputValidationError("Only an existing owned VIDEO Reel can be promoted.")
    eligibility = media.get("boost_eligibility_info")
    if isinstance(eligibility, dict) and eligibility.get("eligible_to_boost") is False:
        raise ToolInputValidationError("Meta reports this Reel is ineligible to boost. Inspect its music, filters, duration and sharing in Instagram.")
    for key in ("media_url", "permalink"):
        if not isinstance(media.get(key), str) or urlsplit(str(media[key])).scheme != "https":
            raise RuntimeError("Meta did not supply a visible HTTPS source Reel and permalink for approval.")
    if "caption" in media and not isinstance(media["caption"], str):
        raise RuntimeError("Meta returned invalid source caption.")
    return media


def _post_result(media: JSONObject, instagram_id: str) -> JSONObject:
    eligibility = media.get("boost_eligibility_info")
    info = eligibility if isinstance(eligibility, dict) else {}
    return {"media_id": str(media.get("id") or ""), "instagram_user_id": instagram_id,
            **{key: str(media.get(key) or "") for key in ("caption", "media_url", "permalink", "timestamp")},
            "eligible_to_boost": info.get("eligible_to_boost"), "boost_ineligible_reason": info.get("boost_ineligible_reason")}


def _ids(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 10:
        raise ToolInputValidationError(label + " must be an array of at most 10 ids.")
    ids = [_id(item, label) for item in value]
    if len(set(ids)) != len(ids):
        raise ToolInputValidationError(label + " must not repeat ids.")
    return ids


def _time(value: object, label: str) -> datetime:
    text = _text(value, label, 35)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputValidationError(label + " must be an ISO 8601 timestamp with timezone.") from exc
    if parsed.tzinfo is None or "T" not in text or parsed.microsecond:
        raise ToolInputValidationError(label + " must include T, timezone and whole seconds.")
    return parsed.astimezone(timezone.utc)


def _require_launch_time(value: JSONObject, seconds: int = INTERACTIVE_BUDGET_SECONDS) -> None:
    if _time(value.get("start_time"), "start_time") <= datetime.now(timezone.utc) + timedelta(seconds=seconds):
        raise ToolInputValidationError(f"Flight start must remain more than {seconds} seconds away. Approval delay or provider reads exhausted this margin. Request a new approval with a later flight.")


def _launch_input(tool_input: JSONObject) -> JSONObject:
    if set(tool_input) - set(LAUNCH_FIELDS) or set(LAUNCH_REQUIRED) - set(tool_input):
        raise ToolInputValidationError("Invalid launch fields.")
    value = dict(tool_input)
    value["name"] = _text(value.get("name"), "name", 120)
    if value.get("objective") not in OBJECTIVES or value.get("special_ad_category") != "NONE":
        raise ToolInputValidationError("Select a supported objective and explicitly declare special_ad_category=NONE.")
    budget = value.get("lifetime_budget")
    if not isinstance(budget, str) or re.fullmatch(r"[1-9][0-9]{0,18}", budget) is None or int(budget) > 2**63 - 1:
        raise ToolInputValidationError("lifetime_budget must be a positive signed-int64 string in Meta budget units.")
    start, end = _time(value.get("start_time"), "start_time"), _time(value.get("end_time"), "end_time")
    if start <= datetime.now(timezone.utc) or end <= start:
        raise ToolInputValidationError("Flight start must be in the future, with a later finite end.")
    value.update({"start_time": start.isoformat(), "end_time": end.isoformat()})
    audience = _object(value.get("audience"), "a closed audience object")
    if set(audience) - {"countries", "country_group", "interest_ids", "advantage_plus"}:
        raise ToolInputValidationError("Unknown audience fields.")
    if ("countries" in audience) == ("country_group" in audience):
        raise ToolInputValidationError("Select nonempty countries OR country_group=worldwide.")
    if "countries" in audience:
        countries = audience["countries"]
        if not isinstance(countries, list) or not 1 <= len(countries) <= 50 or any(not isinstance(c, str) or re.fullmatch(r"[A-Z]{2}", c) is None for c in countries) or len(set(cast(list[str], countries))) != len(countries):
            raise ToolInputValidationError("countries must contain 1-50 distinct uppercase country codes.")
    elif audience["country_group"] != "worldwide":
        raise ToolInputValidationError("Only the provider worldwide country group is supported.")
    normalized: JSONObject = {**audience, "interest_ids": cast(list[JSONValue], _ids(audience.get("interest_ids", []), "interest_ids")),
                             "advantage_plus": audience.get("advantage_plus", True)}
    if type(normalized["advantage_plus"]) is not bool:
        raise ToolInputValidationError("advantage_plus must be a boolean.")
    value["audience"] = normalized
    if value["objective"] == "WEBSITE_CLICKS":
        destination = _text(value.get("destination_url"), "destination_url", 2048)
        try:
            parsed = urlsplit(destination)
            port = parsed.port
        except ValueError as exc:
            raise ToolInputValidationError("Invalid destination URL.") from exc
        host = str(parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or port not in (None, 443) or any(char.isspace() or ord(char) < 32 for char in destination):
            raise ToolInputValidationError("destination_url must be HTTPS without credentials or whitespace.")
        if any(host == domain or host.endswith("." + domain) for domain in ("instagram.com", "facebook.com", "fb.com")):
            raise ToolInputValidationError("Use WEBSITE_CLICKS for an external website; use PROFILE_VISITS for the Instagram profile.")
    elif "destination_url" in value:
        raise ToolInputValidationError("destination_url is only supported for WEBSITE_CLICKS.")
    return value


def _audience(graph: _Graph, audience: JSONObject) -> JSONObject:
    geo: JSONObject
    if "countries" in audience:
        available = graph.get("/search", type="adgeolocation", location_types='["country"]', q="", limit="1000")
        codes = {str(row.get("key")) for row in _rows(available, maximum=1000)}
        if not set(_strings(audience["countries"])).issubset(codes):
            raise ToolInputValidationError("Meta did not confirm every selected country within the bounded country lookup.")
        geo = {"countries": audience["countries"]}
    else:
        available = graph.get("/search", type="adgeolocation", location_types='["country_group"]', q="worldwide", limit="20")
        if not any(row.get("key") == "worldwide" for row in _rows(available, maximum=20)):
            raise ToolInputValidationError("Meta did not confirm the worldwide country group for this connection.")
        geo = {"country_groups": ["worldwide"]}
    # Meta documents home/recent as the only available location types and the
    # default. Put it in the approval rather than accepting an arbitrary readback.
    geo["location_types"] = ["home", "recent"]
    targeting: JSONObject = {"geo_locations": geo, "age_min": 18, "age_max": 65, "publisher_platforms": ["instagram"],
        "instagram_positions": ["reels"], "targeting_automation": {"advantage_audience": 1 if audience["advantage_plus"] else 0}}
    if audience["advantage_plus"]:
        # The provider otherwise derives this suggestion from the same default
        # age bounds. Make it explicit in both the approval and request.
        targeting["age_range"] = [18, 65]
    details: JSONObject = {"targeting": targeting, "interests": []}
    interests = _strings(audience["interest_ids"])
    if interests:
        checked = graph.get("/search", type="adinterestvalid", interest_fbid_list=_json(cast(list[JSONValue], interests)), limit="100")
        rows = _rows(checked, maximum=100)
        matched = [row for row in rows if str(row.get("id")) in interests and row.get("valid") is True]
        if {str(row.get("id")) for row in matched} != set(interests):
            raise ToolInputValidationError("Meta did not confirm all interest ids as valid.")
        details["interests"] = cast(list[JSONValue], [{"id": str(row["id"]), "name": str(row.get("name") or "")} for row in matched])
        targeting["flexible_spec"] = [{"interests": [{"id": interest} for interest in interests]}]
    return details


def _prepare(graph: _Graph, tool_input: JSONObject, api: HostAPI) -> JSONObject:
    value = _launch_input(tool_input)
    account_id = _id(value["account_id"], "account_id")
    connection = _connection(graph, api)
    account = _account(graph, account_id, write=True, launch=True)
    identity = _identity(graph, account_id, _id(value["page_id"], "page_id"), _id(value["instagram_user_id"], "instagram_user_id"))
    if value["objective"] == "PROFILE_VISITS" and identity["account_type"] != "BUSINESS":
        raise ToolInputValidationError("Meta's documented profile-visit route requires a linked Instagram Business account.")
    media = _media(graph, _id(value["media_id"], "media_id"), str(identity["instagram_user_id"]))
    audience = _audience(graph, _object(value["audience"], "audience"))
    minimums = graph.get("/act_" + account_id + "/minimum_budgets", "currency,min_daily_budget_imp", limit="100")
    minimum = next((row.get("min_daily_budget_imp") for row in _rows(minimums, maximum=100) if row.get("currency") == account.get("currency")), None)
    if type(minimum) is not int or minimum <= 0:
        raise ToolInputValidationError("Meta did not supply a usable impression-billing minimum budget for this currency.")
    seconds = int((_time(value["end_time"], "end_time") - _time(value["start_time"], "start_time")).total_seconds())
    # Provider documents daily minimum multiplied by flight days. Round partial
    # days up here and disclose this conservative minimum in the approval.
    minimum_lifetime = minimum * ((seconds + 86399) // 86400)
    if int(str(value["lifetime_budget"])) < minimum_lifetime:
        raise ToolInputValidationError(f"Lifetime budget is below the provider impression minimum for this flight: {minimum_lifetime} Meta budget units (partial days rounded up).")
    countries = _strings(_object(value["audience"], "audience").get("countries"))
    regulated = "country_group" in cast(JSONObject, value["audience"]) or bool(DSA_COUNTRIES.intersection(countries))
    dsa: JSONObject = {}
    if regulated:
        for key in ("dsa_beneficiary", "dsa_payor"):
            try:
                dsa[key] = _text(account.get("default_" + key), "default_" + key, 512)
            except ToolInputValidationError as exc:
                raise ToolInputValidationError(f"This geography requires both saved public DSA names. Set accurate beneficiary and payer defaults in Meta Ads Manager > Advertising settings, then request a new launch approval. Meta did not supply a usable default_{key}.") from exc
    objective, optimization, destination_type = OBJECTIVES[str(value["objective"])]
    destination: JSONValue = value.get("destination_url")
    cta: JSONValue = None
    if value["objective"] == "PROFILE_VISITS":
        destination = "https://www.instagram.com/" + str(identity["username"])
        cta = {"type": "VIEW_INSTAGRAM_PROFILE", "value": {"link": destination}}
    elif value["objective"] == "WEBSITE_CLICKS":
        cta = {"type": "LEARN_MORE", "value": {"link": destination}}
    return {"input": value, "connection": connection, "account": _account_result(account),
        "funding_source_sha256": hashlib.sha256(str(account["funding_source"]).encode()).hexdigest(),
        "identity": identity, "source_reel": _post_result(media, str(identity["instagram_user_id"])),
        "audience": audience, "provider_objective": objective, "optimization_goal": optimization,
        "destination_type": destination_type, "destination": destination, "call_to_action": cta,
        "dsa": dsa,
        "billing_event": "IMPRESSIONS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "minimum_lifetime_budget": str(minimum_lifetime)}


def _binding(proposal: JSONObject) -> JSONObject:
    # CDN signatures may rotate without altering the source media. Bind the
    # source id, owner, exact caption/permalink and canonical media locator.
    bound = json.loads(_json(proposal))
    media = bound["source_reel"]
    url = urlsplit(media["media_url"])
    media["media_url"] = url.scheme + "://" + url.netloc + url.path
    return cast(JSONObject, bound)


def _campaign(graph: _Graph, account_id: str, campaign_id: str) -> JSONObject:
    campaign = graph.get("/" + campaign_id, CAMPAIGN_FIELDS)
    if campaign.get("id") != campaign_id or str(campaign.get("account_id")) != account_id:
        raise ToolInputValidationError("Campaign does not belong to the selected account.")
    return campaign


def _summary(proposal: JSONObject) -> str:
    value = _object(proposal["input"], "launch input")
    account = _object(proposal["account"], "account")
    offset = cast(int, account["currency_offset"])
    amount = Decimal(str(value["lifetime_budget"])) / Decimal(offset)
    # Provider names and captions remain exact in the payload. Keep this short
    # enough for HostApprovals' 500-byte limit even at the numeric input bounds.
    return (f"Launch NEW Reels campaign: account {value['account_id']}, Reel {value['media_id']}. "
            f"{value['objective']}; total {amount:.2f} {account['currency']}; {value['start_time']} to {value['end_time']}. "
            "Billed for impressions. Review exact identity, caption, audience and destination in payload. "
            "Create paused, verify, activate parent last. Meta review may release delivery later within this flight. "
            "ACTIVE does not prove delivery; Meta spend is separate from Kern costs.")


def _matches(expected: JSONValue, actual: JSONValue, *, exact_keys: bool = False) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        if exact_keys and set(actual) != set(expected):
            # Meta may label an id reference. A name cannot alter targeting;
            # every other additional field must stop parent activation.
            if set(expected) != {"id"} or set(actual) != {"id", "name"} or not isinstance(actual["name"], str):
                return False
        return all(key in actual and (value == actual[key] if exact_keys and key == "age_range"
                   else _matches(value, actual[key], exact_keys=exact_keys)) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        remaining = list(actual)
        for value in expected:
            match = next((index for index, row in enumerate(remaining) if _matches(value, row, exact_keys=exact_keys)), None)
            if match is None:
                return False
            remaining.pop(match)
        return True
    return expected == actual


def _has_enhancement(value: JSONValue) -> bool:
    if isinstance(value, dict):
        return value.get("enroll_status") == "OPT_IN" or any(_has_enhancement(item) for item in value.values())
    return isinstance(value, list) and any(_has_enhancement(item) for item in value)


def _verify(graph: _Graph, proposal: JSONObject, ids: dict[str, str], *, children_active: bool) -> JSONObject:
    value = _object(proposal["input"], "input")
    campaign = _campaign(graph, str(value["account_id"]), ids["campaign_id"])
    expected_campaign: JSONObject = {"name": value["name"], "objective": proposal["provider_objective"], "buying_type": "AUCTION", "status": "PAUSED", "special_ad_categories": []}
    if not _matches(expected_campaign, campaign) or any(str(campaign.get(key) or "0") != "0" for key in ("daily_budget", "lifetime_budget", "spend_cap")) or campaign.get("is_adset_budget_sharing_enabled") not in (False, 0):
        raise RuntimeError("Paused campaign configuration differs from approval.")
    adset = graph.get("/" + ids["adset_id"], ADSET_FIELDS)
    expected_adset: JSONObject = {"id": ids["adset_id"], "name": value["name"], "account_id": value["account_id"], "campaign_id": ids["campaign_id"],
        "status": "ACTIVE" if children_active else "PAUSED", "lifetime_budget": value["lifetime_budget"], "is_dynamic_creative": False,
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "billing_event": "IMPRESSIONS",
        "optimization_goal": proposal["optimization_goal"], "destination_type": proposal["destination_type"],
        "targeting": _object(proposal["audience"], "audience")["targeting"]}
    if value["objective"] in {"PROFILE_VISITS", "ENGAGEMENTS"}:
        expected_adset["promoted_object"] = {"page_id": value["page_id"]}
    dsa = _object(proposal["dsa"], "resolved DSA defaults")
    expected_adset.update(dsa)
    if not dsa and any(adset.get(key) not in (None, "") for key in ("dsa_beneficiary", "dsa_payor")):
        raise RuntimeError("Ad-set public DSA names were not approved for this geography.")
    if not _matches(expected_adset, adset) or str(adset.get("daily_budget") or "0") != "0":
        raise RuntimeError("Ad-set budget, ownership, outcome, targeting or status differs from approval.")
    if any(_time(adset.get(key), key) != _time(value[key], key) for key in ("start_time", "end_time")):
        raise RuntimeError("Ad-set flight differs from approval.")
    if adset.get("is_budget_schedule_enabled") is not False:
        raise RuntimeError("Meta did not confirm budget scheduling disabled before activation.")
    if any(adset.get(key) for key in ("adset_schedule", "frequency_control_specs", "bid_constraints")) or adset.get("pacing_type") not in (None, [], ["standard"]):
        raise RuntimeError("Provider added unapproved scheduling, pacing, frequency or bid constraints.")
    if adset.get("optimization_sub_event") not in (None, "NONE"):
        raise RuntimeError("Provider added an optimization sub-event outside approval.")
    for key in ("bid_amount", "daily_min_spend_target", "lifetime_min_spend_target", "daily_spend_cap", "lifetime_spend_cap"):
        # The documented cap-removal sentinel means no cap, just like zero.
        allowed = {"0", "922337203685478"} if key.endswith("spend_cap") else {"0"}
        if str(adset.get(key) or "0") not in allowed:
            raise RuntimeError("Provider added an unapproved bid amount or spend constraint.")
    targeting = _object(adset.get("targeting"), "targeting")
    if not _matches(expected_adset["targeting"], targeting, exact_keys=True):
        raise RuntimeError("Provider targeting contains unapproved fields or values.")
    creative = graph.get("/" + ids["creative_id"], CREATIVE_FIELDS)
    if not _matches({"id": ids["creative_id"], "name": value["name"], "account_id": value["account_id"], "object_id": value["page_id"],
                     "instagram_user_id": value["instagram_user_id"], "source_instagram_media_id": value["media_id"]}, creative) or creative.get("asset_feed_spec"):
        raise RuntimeError("Creative source Reel or advertising identity differs from approval.")
    if _has_enhancement(creative.get("degrees_of_freedom_spec")):
        raise RuntimeError("Provider enabled a creative enhancement outside the approved original Reel.")
    if "destination_spec" not in creative:
        raise RuntimeError("Meta did not return the requested destination configuration. It cannot be verified before activation.")
    if creative["destination_spec"] is not None and creative["destination_spec"] != {}:
        raise RuntimeError("Creative has additional destination configuration or optimization outside approval.")
    if any(creative.get(key) and creative[key] != proposal["destination"] for key in ("link_url", "object_url")):
        raise RuntimeError("Creative contains a top-level destination outside approval.")
    if creative.get("body") is not None and creative["body"] != _object(proposal["source_reel"], "source Reel")["caption"]:
        raise RuntimeError("Creative caption differs from the approved original Reel.")
    cta = proposal["call_to_action"]
    returned_ctas = [creative["call_to_action"]] if creative.get("call_to_action") is not None else []
    story = creative.get("object_story_spec")
    if isinstance(story, dict):
        for key in ("page_id", "instagram_user_id"):
            if key in story and story[key] != value[key]:
                raise RuntimeError("Creative story overrides the approved advertising identity.")
        for key in ("link_data", "video_data"):
            data = story.get(key)
            if isinstance(data, dict):
                if "message" in data and data["message"] != _object(proposal["source_reel"], "source Reel")["caption"]:
                    raise RuntimeError("Creative text overrides the approved original Reel.")
                if data.get("call_to_action") is not None:
                    returned_ctas.append(data["call_to_action"])
                if cta is not None and data.get("link") is not None and data["link"] != proposal["destination"]:
                    raise RuntimeError("Creative story contains an unapproved destination.")
    if (cta is not None and (not returned_ctas or any(not _matches(cta, actual, exact_keys=True) for actual in returned_ctas))) or (cta is None and any(returned_ctas)):
        raise RuntimeError("Provider did not confirm the exact approved CTA and destination.")
    allowed_types = (None, _object(cta, "CTA")["type"]) if cta is not None else (None, "", "NONE")
    if creative.get("call_to_action_type") not in allowed_types:
        raise RuntimeError("Creative CTA type differs from approval.")
    ad = graph.get("/" + ids["ad_id"], AD_FIELDS)
    if not _matches({"id": ids["ad_id"], "name": value["name"], "account_id": value["account_id"], "campaign_id": ids["campaign_id"],
                     "adset_id": ids["adset_id"], "status": "ACTIVE" if children_active else "PAUSED",
                     "creative": {"id": ids["creative_id"]}}, ad):
        raise RuntimeError("Ad hierarchy, creative or configured status differs from approval.")
    for parent, edge, identifier in ((ids["campaign_id"], "adsets", ids["adset_id"]), (ids["adset_id"], "ads", ids["ad_id"])):
        listing = graph.get("/" + parent + "/" + edge, "id", limit="2")
        rows = _rows(listing, maximum=2)
        if len(rows) != 1 or rows[0].get("id") != identifier or _cursor(listing):
            raise RuntimeError("Provider hierarchy no longer contains exactly the approved one ad set and one ad.")
    return ad


def _failure(exc: Exception, *, action: str, phase: str, confirmed: tuple[str, ...] = (), diagnose: bool = True) -> ActionFailed:
    if diagnose:
        report_ads_failure("instagram_ads", action, exc, phase=phase, confirmed=confirmed)
    if isinstance(exc, IntegrationReconnectRequired):
        return ActionFailed(str(exc), reconnect_required=True)
    if isinstance(exc, ToolInputValidationError):
        return ActionFailed(exc.message)
    if isinstance(exc, WebRequestError):
        known = known_provider_transport_error(exc)
        if known:
            return ActionFailed(known)
        if exc.status in (401, 403):
            return ActionFailed("Meta denied access. Check app review, granted scopes and advertiser/Page/Instagram assignments.")
        if exc.status == 429:
            return ActionFailed("Meta rate limit reached. This call did not retry.")
        return ActionFailed(f"Meta Marketing API returned HTTP {exc.status or 'transport failure'}. Check Meta eligibility, account and regional requirements.")
    return ActionFailed(str(exc) or "Instagram Ads could not confirm the result.")


def _launch(graph: _Graph, proposal: JSONObject, api: HostAPI) -> ApprovalResult:
    ids: dict[str, str] = {}
    stage = "revalidation before creation"
    try:
        value = _object(proposal["input"], "input")
        fresh = _prepare(graph, value, api)
        if _binding(fresh) != _binding(proposal):
            raise RuntimeError("Credential/access, account, source Reel or launch details changed since approval. Request a new approval.")
        _require_launch_time(value)
        prefix = "/act_" + str(value["account_id"])
        stage = "campaign creation"
        result = graph.post(prefix + "/campaigns", {"name": value["name"], "objective": proposal["provider_objective"],
            "buying_type": "AUCTION", "status": "PAUSED", "special_ad_categories": [], "is_adset_budget_sharing_enabled": False})
        ids["campaign_id"] = _id(result.get("id"), "created campaign id")
        stage = "ad-set creation"
        adset: JSONObject = {"name": value["name"], "campaign_id": ids["campaign_id"], "lifetime_budget": value["lifetime_budget"],
            "start_time": value["start_time"], "end_time": value["end_time"], "status": "PAUSED", "is_dynamic_creative": False,
            "is_budget_schedule_enabled": False,
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "billing_event": "IMPRESSIONS", "optimization_goal": proposal["optimization_goal"],
            "destination_type": proposal["destination_type"], "targeting": _object(proposal["audience"], "audience")["targeting"]}
        if value["objective"] in {"PROFILE_VISITS", "ENGAGEMENTS"}:
            adset["promoted_object"] = {"page_id": value["page_id"]}
        adset.update(_object(proposal["dsa"], "resolved DSA defaults"))
        result = graph.post(prefix + "/adsets", adset)
        ids["adset_id"] = _id(result.get("id"), "created ad-set id")
        stage = "creative creation"
        creative: JSONObject = {"name": value["name"], "object_id": value["page_id"], "instagram_user_id": value["instagram_user_id"],
                               "source_instagram_media_id": value["media_id"]}
        if proposal["call_to_action"] is not None:
            creative["call_to_action"] = proposal["call_to_action"]
        result = graph.post(prefix + "/adcreatives", creative)
        ids["creative_id"] = _id(result.get("id"), "created creative id")
        stage = "ad creation"
        result = graph.post(prefix + "/ads", {"name": value["name"], "adset_id": ids["adset_id"], "creative": {"creative_id": ids["creative_id"]}, "status": "PAUSED"})
        ids["ad_id"] = _id(result.get("id"), "created ad id")
        stage = "paused configuration verification"
        _verify(graph, proposal, ids, children_active=False)
        for key, label in (("ad_id", "ad activation under paused parent"), ("adset_id", "ad-set activation under paused parent")):
            stage = label
            if graph.post("/" + ids[key], {"status": "ACTIVE"}).get("success") is not True:
                raise RuntimeError("Meta did not confirm child activation.")
        stage = "resource revalidation before parent activation"
        if _binding(_prepare(graph, value, api)) != _binding(proposal):
            raise RuntimeError("Launch resources changed while the hierarchy was paused. Parent activation stopped.")
        stage = "active-child configuration verification under paused parent"
        _verify(graph, proposal, ids, children_active=True)
        stage = "activation time verification under paused parent"
        remaining = int(graph.deadline - time.monotonic())
        if remaining < 30:  # Parent POST and readback each have a 15-second bound.
            raise RuntimeError("Not enough interactive request budget to activate and read back the parent. Parent activation stopped.")
        _require_launch_time(value, remaining)
        stage = "parent activation"
        if graph.post("/" + ids["campaign_id"], {"status": "ACTIVE"}).get("success") is not True:
            raise RuntimeError("Meta did not confirm parent activation.")
        stage = "parent activation readback"
        campaign = _campaign(graph, str(value["account_id"]), ids["campaign_id"])
        if campaign.get("status") != "ACTIVE":
            raise RuntimeError("Meta did not report the campaign configured ACTIVE.")
        return ApprovalExecuted(f"Instagram Ads launch configured ACTIVE. Confirmed ids: {_json(cast(JSONObject, ids))}. "
            f"Parent effective status: {campaign.get('effective_status', 'unavailable')}. "
            "Provider review may delay delivery and later release it within the approved flight. Configured ACTIVE does not prove accepted review, impressions or spend. Inspect get_campaign/get_performance or Ads Manager.")
    except Exception as exc:
        mapped = _failure(exc, action="launch_campaign", phase=stage, confirmed=tuple(f"{key} {value}" for key, value in ids.items()))
        return ActionFailed(f"Instagram Ads stopped at {stage}. Confirmed ids: {_json(cast(JSONObject, ids))}. "
            f"{mapped.error} This stage may have an unconfirmed outcome. Inspect Ads Manager before another launch. "
            "No automatic retry, cleanup or resume was attempted.", reconnect_required=mapped.reconnect_required)


def _campaign_result(value: JSONObject) -> JSONObject:
    return {**{key: str(value.get(key) or "") for key in ("id", "account_id", "name", "objective", "status", "effective_status")},
            "configuration_json": _json(value)}


def _report_dates(value: JSONObject) -> tuple[date, date]:
    if any(not isinstance(value[key], str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value[key])) is None for key in ("start_date", "end_date")):
        raise ToolInputValidationError("Report dates must be YYYY-MM-DD.")
    try:
        start, end = date.fromisoformat(str(value["start_date"])), date.fromisoformat(str(value["end_date"]))
    except ValueError as exc:
        raise ToolInputValidationError("Report dates must be YYYY-MM-DD.") from exc
    if end < start or (end - start).days >= 90:
        raise ToolInputValidationError("Report range must be 1-90 inclusive days.")
    return start, end


def _read(graph: _Graph, action: str, value: JSONObject, api: HostAPI) -> JSONObject:
    params = _page(value, api) if action not in {"get_account"} else {}
    if action == "list_accounts":
        listing = graph.get("/me/adaccounts", ACCOUNT_FIELDS, **params)
        return {"message": "Accessible advertisers; billing presence does not guarantee delivery.",
                "items": cast(list[JSONValue], [_account_result(row) for row in _rows(listing, maximum=int(params["limit"]))]), "next_cursor": _cursor(listing)}
    account_id = _id(value["account_id"], "account_id")
    account = _account(graph, account_id)
    if action == "get_account":
        return _account_result(account)
    prefix = "/act_" + account_id
    if action == "list_identities":
        listing = graph.get(prefix + "/promote_pages", "id,name,instagram_business_account{id,username,account_type}", **params)
        rows = []
        for row in _rows(listing, maximum=int(params["limit"])):
            linked = row.get("instagram_business_account")
            if isinstance(linked, dict) and linked.get("account_type") in {"BUSINESS", "MEDIA_CREATOR"}:
                rows.append({"page_id": _id(row.get("id"), "Page id"), "page_name": str(row.get("name") or ""),
                    "instagram_user_id": _id(linked.get("id"), "Instagram id"), "username": str(linked.get("username") or ""), "account_type": linked["account_type"]})
        return {"message": "Advertiser Pages with real linked professional Instagram identities; launch verifies Instagram asset access separately.",
                "items": cast(list[JSONValue], rows), "next_cursor": _cursor(listing)}
    if action == "list_posts":
        instagram_id = _id(value["instagram_user_id"], "instagram_user_id")
        _identity(graph, account_id, _id(value["page_id"], "page_id"), instagram_id)
        listing = graph.get("/" + instagram_id + "/media", MEDIA_FIELDS, **params)
        rows = []
        for row in _rows(listing, maximum=int(params["limit"])):
            owner = row.get("owner")
            if row.get("media_type") == "VIDEO" and row.get("media_product_type") == "REELS" and isinstance(owner, dict) and owner.get("id") == instagram_id:
                rows.append(_post_result(row, instagram_id))
        return {"message": "Owned video Reels from this media page. Meta boost eligibility is not ad review acceptance; other media is filtered.",
                "items": cast(list[JSONValue], rows), "next_cursor": _cursor(listing)}
    if action == "lookup_targeting":
        kind = value["type"]
        if kind not in ("COUNTRY", "COUNTRY_GROUP", "INTEREST"):
            raise ToolInputValidationError("Select COUNTRY, COUNTRY_GROUP or INTEREST.")
        query = _text(value["query"], "query", 100)
        if not query.isascii():
            raise ToolInputValidationError("query must be ASCII for the direct parameter guard.")
        options = {"type": "adinterest"} if kind == "INTEREST" else {"type": "adgeolocation", "location_types": '["country"]' if kind == "COUNTRY" else '["country_group"]'}
        listing = graph.get("/search", q=query, **options, **params)
        rows = [{"id": str(row.get("id") or row.get("key") or ""), "name": str(row.get("name") or ""), "type": str(row.get("type") or kind)} for row in _rows(listing, maximum=int(params["limit"]))]
    elif action == "list_campaigns":
        listing = graph.get(prefix + "/campaigns", CAMPAIGN_FIELDS, **params)
        rows = [_campaign_result(row) for row in _rows(listing, maximum=int(params["limit"]))]
    elif action == "get_campaign":
        campaign = _campaign(graph, account_id, _id(value["campaign_id"], "campaign_id"))
        listing = graph.get("/" + str(value["campaign_id"]) + "/adsets", ADSET_FIELDS + ",ads.limit(10){" + AD_FIELDS + "}", **params)
        rows = []
        for row in _rows(listing, maximum=int(params["limit"])):
            ads = _object(row.get("ads"), "ad-set ads page")
            configuration = {key: item for key, item in row.items() if key != "ads"}
            rows.append({**{key: str(row.get(key) or "") for key in ("id", "name", "status", "effective_status")},
                "configuration_json": _json(configuration), "ads": [{**{key: str(ad.get(key) or "") for key in ("id", "name", "status", "effective_status")},
                    "configuration_json": _json(ad)} for ad in _rows(ads, maximum=10)], "ads_next_cursor": _cursor(ads)})
        return {"campaign": _campaign_result(campaign), "adsets": cast(list[JSONValue], rows), "next_cursor": _cursor(listing)}
    elif action == "get_performance":
        _campaign(graph, account_id, _id(value["campaign_id"], "campaign_id"))
        start, end = _report_dates(value)
        listing = graph.get("/" + str(value["campaign_id"]) + "/insights", "date_start,date_stop,account_currency,spend,impressions,reach,clicks,inline_link_clicks,actions,video_thruplay_watched_actions",
            time_range=_json({"since": start.isoformat(), "until": end.isoformat()}), time_increment="1", **params)
        rows = []
        for row in _rows(listing, maximum=int(params["limit"])):
            actions = row.get("actions")
            video = row.get("video_thruplay_watched_actions")
            rows.append({**{key: str(row.get(key) or "") for key in ("date_start", "date_stop", "account_currency")},
                **{key: str(row[key]) if row.get(key) is not None else None for key in ("spend", "impressions", "reach", "clicks", "inline_link_clicks")},
                "actions": [{"action_type": str(item.get("action_type") or ""), "value": str(item["value"])} for item in actions if isinstance(item, dict) and item.get("value") is not None] if isinstance(actions, list) else None,
                "video_thruplay": str(video[0]["value"]) if isinstance(video, list) and video and isinstance(video[0], dict) and "value" in video[0] else None})
    else:
        raise ToolInputValidationError("Unsupported Instagram Ads read action.")
    return {"message": "One bounded Meta page. Missing report metrics remain unavailable; actions/reach may be attributed, modelled or estimated.",
            "items": cast(list[JSONValue], rows), "next_cursor": _cursor(listing)}


class InstagramAdsTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        validated = False
        try:
            _validate(action, tool_input)
            # Validate all direct free text before even the account read.
            spec = MANIFEST.action(action)
            if spec is not None and spec.approval == "direct":
                if action == "diagnose_account":
                    _diagnostic_params(tool_input, api, "pages_after")
                    _diagnostic_params(tool_input, api, "instagram_after")
                    _diagnostic_params(tool_input, api, "user_pages_after")
                if "after" in tool_input or "limit" in tool_input:
                    _page(tool_input, api)
                if action == "lookup_targeting":
                    query = _text(tool_input["query"], "query", 100)
                    api.outbound.guard_request_parameter_string(query)
            if action == "launch_campaign":
                _launch_input(tool_input)
            elif action == "get_performance":
                _report_dates(tool_input)
            validated = True
            token = CREDENTIALS.access_token(api)
            graph = _Graph(token, api.config["INSTAGRAM_ADS_APP_SECRET"], api=api)
            if action == "launch_campaign":
                proposal = _prepare(graph, tool_input, api)
                _require_launch_time(_object(proposal["input"], "input"))
                record = api.approvals.request(action_id=action, summary=_summary(proposal), payload={"tool_id": MANIFEST.tool_id, "proposal": proposal})
            elif action == "end_campaign":
                account_id, campaign_id = str(tool_input["account_id"]), str(tool_input["campaign_id"])
                connection = _connection(graph, api)
                _account(graph, account_id, write=True)
                campaign = _campaign(graph, account_id, campaign_id)
                record = api.approvals.request(action_id=action,
                    summary=f"Pause Meta campaign {campaign_id} in ad account {account_id}. Retain history/reports. Review the campaign name in the payload. Ads Manager can resume it; stopping may be delayed and past delivery remains billable.",
                    payload={"tool_id": MANIFEST.tool_id, "account_id": account_id, "campaign_id": campaign_id,
                             "campaign_name": campaign.get("name", ""), "connection": connection})
            else:
                connection = _connection(graph, api)
                if action == "diagnose_account":
                    return ActionExecuted(_diagnose_account(graph, tool_input, api, connection))
                return ActionExecuted(_read(graph, action, tool_input, api))
            return ActionPendingApproval(record.approval_id, record.summary)
        except Exception as exc:
            return _failure(exc, action=action, phase="proposal" if action in {"launch_campaign", "end_campaign"} else "read", diagnose=validated)

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            payload = approval.payload
            if payload.get("tool_id") != MANIFEST.tool_id or approval.action_id not in {"launch_campaign", "end_campaign"}:
                raise RuntimeError("Invalid Instagram Ads approval.")
            proposal = _object(payload.get("proposal"), "launch proposal") if approval.action_id == "launch_campaign" else payload
            connection = _object(proposal.get("connection"), "approved connection")
            token = CREDENTIALS.access_token(api)
            if connection.get("fingerprint") != _fingerprint(api, token):
                raise RuntimeError("Instagram Ads connection or app credentials changed after approval. Request a new approval.")
            graph = _Graph(token, api.config["INSTAGRAM_ADS_APP_SECRET"], api=api, expected_connection=str(connection["fingerprint"]))
            stored = CREDENTIALS.load_connected(api)
            expiry = stored["secret"].get("expires_at")
            if type(expiry) is not int or expiry <= now() + INTERACTIVE_BUDGET_SECONDS:
                raise IntegrationReconnectRequired("Instagram Ads token cannot cover the full approved action. Connect again under Home > Integrations > Instagram Ads and request a new approval.")
            if approval.action_id == "launch_campaign":
                _require_launch_time(_object(proposal["input"], "input"))
                return _launch(graph, proposal, api)
            if _connection(graph, api) != connection:
                raise RuntimeError("Facebook identity or scopes changed after approval. Request a new approval.")
            account_id, campaign_id = _id(payload.get("account_id"), "account_id"), _id(payload.get("campaign_id"), "campaign_id")
            _account(graph, account_id, write=True)
            _campaign(graph, account_id, campaign_id)
            try:
                if graph.post("/" + campaign_id, {"status": "PAUSED"}).get("success") is not True:
                    raise RuntimeError("Meta did not confirm parent pause.")
                campaign = _campaign(graph, account_id, campaign_id)
                if campaign.get("status") != "PAUSED":
                    raise RuntimeError("Meta did not report the parent configured PAUSED.")
            except Exception as exc:
                mapped = _failure(exc, action=approval.action_id, phase="ending campaign", confirmed=(f"campaign {campaign_id}",))
                return ActionFailed(f"Campaign {campaign_id} pause is unconfirmed. {mapped.error} Inspect Ads Manager; do not assume delivery stopped or retry this approval.", reconnect_required=mapped.reconnect_required)
            return ApprovalExecuted(f"Campaign {campaign_id} in account {account_id} is configured PAUSED; effective status {campaign.get('effective_status', 'unavailable')}. History/reports retained. Stopping may be delayed, past delivery remains billable and Ads Manager can resume it.")
        except Exception as exc:
            return _failure(exc, action=approval.action_id, phase="approval revalidation")


BUNDLED_TOOL = InstagramAdsTool()
