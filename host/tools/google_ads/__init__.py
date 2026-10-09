"""Bounded Google Ads reads and exact approval-bound Search campaign writes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math
import json
import re
import urllib.parse
import unicodedata
from typing import NoReturn, cast

from host.tools.google_ads.manifest import ADS_SCOPE, MANIFEST, MAX_MONEY_MICROS, MAX_ROWS
from host.tools.google_ads.locations import list_locations
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalResult
from host.tools.shared.google import GoogleCredentialStore, IntegrationReconnectRequired, google_token_for_revoke_from_payload, revoke_google_token
from host.tools.shared.ads_diagnostics import MAX_ADS_ERROR_BYTES, AdsProviderError, ads_error_body, ads_failure_context, report_ads_failure
from host.tools.shared.inputs import ToolInputValidationError, clip_text
from host.tools.shared.web import WebRequestError, UnmappedProviderError, ProviderWarning, json_request, transport_or_unmapped_provider_error
from host.tools.tool import CredentialFlow, OAuthCompleteConnectParams, OAuthCompleteConnectResult
from host.tools.manifest import ToolManifest

API_BASE = "https://googleads.googleapis.com/v25"


class AdsGoogleCredentialStore(GoogleCredentialStore):
    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        self._verify_state(params["state"], api)
        try:
            return super().complete_connect(params, api)
        except ProviderWarning as exc:
            exc.response_body = ads_error_body(exc.response_body.encode("utf-8")).decode("utf-8")
            exc.diagnostic_context.update(ads_failure_context("google_ads", "oauth_complete_connect", exc, phase="connection"))
            raise
        except KeyError:
            raise
        except Exception as exc:
            report_ads_failure("google_ads", "oauth_complete_connect", exc, phase="connection")
            raise

    def disconnect(self, api: HostAPI) -> None:
        try:
            existing = api.credentials.load()
            if existing is not None:
                token = google_token_for_revoke_from_payload(existing["secret"])
                if token:
                    outcome = revoke_google_token(token)
                    if not outcome.get("success"):
                        status = outcome.get("status")
                        failure = AdsProviderError("Google token revocation failed; local credentials were disconnected.",
                                                   "POST OAuth revoke", status=status if type(status) is int else 0)
                        report_ads_failure("google_ads", "oauth_disconnect", failure, phase="connection")
            api.credentials.clear()
        except ProviderWarning:
            raise
        except Exception as exc:
            report_ads_failure("google_ads", "oauth_disconnect", exc, phase="connection")
            raise


CREDENTIALS = AdsGoogleCredentialStore(
    tool_id="google_ads", scopes=("openid", "email", ADS_SCOPE), required_scopes=frozenset({ADS_SCOPE}),
    reconnect_message="Google Ads is disconnected or missing permissions. Reconnect Google Ads in Home > Integrations.",
)
CUSTOMER_FIELDS = "customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager, customer.test_account"
CAMPAIGN_FIELDS = "campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, campaign.campaign_budget, campaign_budget.amount_micros, campaign_budget.total_amount_micros, campaign_budget.period, campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign.start_date_time, campaign.end_date_time, campaign.bidding_strategy_type, campaign.primary_status"
ACTIONS = {action.id: action for action in MANIFEST.actions}


def _fail(message: str) -> NoReturn:
    raise ToolInputValidationError(message)


def _id(value: JSONValue, field: str, customer: bool = False) -> str:
    pattern = r"[0-9]{10}" if customer else r"[0-9]{1,19}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value, flags=re.ASCII):
        _fail(f"{field} must be {'10' if customer else '1 to 19'} ASCII digits without hyphens.")
    return cast(str, value)


def _integer(value: JSONValue, field: str, high: int) -> int:
    if type(value) is not int or not 1 <= cast(int, value) <= high:
        _fail(f"{field} must be an integer from 1 to {high:,}.")
    return cast(int, value)


def _text(value: JSONValue, field: str, chars: int, byte_limit: int) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > chars or len(value.encode("utf-8")) > byte_limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail(f"{field} requires trimmed text, at most {chars} characters and {byte_limit} UTF-8 bytes, without control characters.")
    return cast(str, value)


def _ad_texts(value: JSONValue, field: str, low: int, high: int, chars: int) -> list[JSONValue]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        _fail(f"{field} must contain {low} to {high} distinct text values.")
    values = [_text(item, field, chars, chars * 4) for item in cast(list[JSONValue], value)]
    if any(sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in item) > chars for item in values):
        _fail(f"{field} allows at most {chars} characters, counting double-width characters as two.")
    if len(set(values)) != len(values) or any("{" in item or "}" in item for item in values):
        _fail(f"{field} must contain distinct plain text without ad customizers.")
    return cast(list[JSONValue], values)


def _ids(value: JSONValue, field: str) -> list[JSONValue]:
    if not isinstance(value, list) or not 0 <= len(value) <= 10:
        _fail(f"{field} must contain 0 to 10 distinct constant IDs.")
    values = [_id(item, field) for item in cast(list[JSONValue], value)]
    if len(set(values)) != len(values):
        _fail(f"{field} must contain distinct IDs.")
    return cast(list[JSONValue], values)


def _keywords(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        _fail("keywords must contain 1 to 20 keyword objects.")
    result: list[JSONValue] = []
    seen: set[tuple[str, str]] = set()
    for item in cast(list[JSONValue], value):
        if not isinstance(item, dict) or set(item) != {"text", "match_type"}:
            _fail("Each keyword requires only text and match_type.")
        record = cast(JSONObject, item)
        text = _text(record.get("text"), "keyword text", 80, 320)
        match = record.get("match_type")
        if match not in ("EXACT", "PHRASE", "BROAD"):
            _fail("Keyword match_type must be EXACT, PHRASE or BROAD.")
        if len(text.split()) > 10 or any(char in text for char in '[]"{}'):
            _fail("Keyword text requires at most 10 words, without match-type brackets, quotes or customizers.")
        key = (text.casefold(), cast(str, match))
        if key in seen:
            _fail("Duplicate keyword/match-type pairs are not allowed.")
        seen.add(key)
        result.append({"text": text, "match_type": cast(str, match)})
    return result


def _timestamp(value: JSONValue, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})", value, re.ASCII):
        _fail(f"{field} must be RFC3339 with seconds and an explicit UTC offset, without fractions.")
    try:
        return datetime.fromisoformat(cast(str, value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail(f"{field} must be a valid RFC3339 timestamp.")


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flight(values: JSONObject, account: JSONObject) -> JSONObject:
    try:
        zone = ZoneInfo(cast(str, account["time_zone"]))
    except (ZoneInfoNotFoundError, ValueError):
        _fail("Google Ads returned an unsupported account time zone.")
    start = _timestamp(values["start_time"], "start_time")
    end = _timestamp(values["end_time"], "end_time")
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    if local_start.strftime("%H:%M:%S") != "00:00:00" or local_end.strftime("%H:%M:%S") != "23:59:59":
        _fail("Search flights require account-local start 00:00:00 and end 23:59:59. Use the account time zone's UTC offset.")
    if not 3 <= (local_end.date() - local_start.date()).days + 1 <= 90:
        _fail("Search total-budget flights require 3 to 90 inclusive days in the account time zone.")
    now = _now()
    if end <= now or start.astimezone(zone).date() < now.astimezone(zone).date():
        _fail("The approved flight expired or its start date passed. Choose new start/end times and queue a new approval.")
    result: JSONObject = {"time_zone": account["time_zone"]}
    for field, instant in (("start_date_time", start), ("end_date_time", end)):
        local = instant.astimezone(zone)
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            _fail("Flight times cannot fall in an ambiguous daylight-saving hour. Choose an unambiguous time.")
        result[field] = local.strftime("%Y-%m-%d %H:%M:%S")
    return result


def _input(action: str, raw: JSONObject) -> JSONObject:
    spec = ACTIONS.get(action)
    if spec is None:
        _fail("Unsupported Google Ads action.")
    properties = cast(JSONObject, spec.input_schema["properties"])
    required = cast(list[str], spec.input_schema.get("required", []))
    if set(raw) - set(properties) or any(key not in raw for key in required):
        _fail("Google Ads input has unknown fields or is missing a required field. Use the action's declared schema.")
    result = dict(raw)
    if action == "list_locations":
        query = raw.get("query", "")
        if not isinstance(query, str) or len(query) > 80 or len(query.encode("utf-8")) > 320 or any(ord(c) < 32 or ord(c) == 127 for c in query):
            _fail("Location query must be at most 80 characters and 320 UTF-8 bytes, without control characters.")
        country_code = raw.get("country_code", "")
        if not isinstance(country_code, str) or country_code and not re.fullmatch(r"[A-Z]{2}", country_code, re.ASCII):
            _fail("country_code must be empty or two uppercase ASCII letters, such as GB.")
        return {"query": query.strip(), "country_code": country_code,
                "limit": _integer(raw.get("limit", 250), "limit", 250)}
    if "customer_id" in raw:
        result["customer_id"] = _id(raw["customer_id"], "customer_id", customer=True)
    if "campaign_id" in raw:
        result["campaign_id"] = _id(raw["campaign_id"], "campaign_id")
    if "limit" in properties:
        result["limit"] = _integer(raw.get("limit", 100), "limit", MAX_ROWS)
    if "total_budget_micros" in raw:
        result["total_budget_micros"] = _integer(raw["total_budget_micros"], "total_budget_micros", MAX_MONEY_MICROS)
    if action == "report":
        if raw["report_type"] not in ("campaigns", "keywords", "search_terms"):
            _fail("report_type must be campaigns, keywords or search_terms.")
        dates: list[date] = []
        for key in ("start_date", "end_date"):
            value = raw[key]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value, flags=re.ASCII):
                _fail(f"{key} must be YYYY-MM-DD.")
            try:
                dates.append(date.fromisoformat(cast(str, value)))
            except ValueError:
                _fail(f"{key} must be a valid calendar date.")
        if not 0 <= (dates[1] - dates[0]).days <= 365:
            _fail("Report range must be ordered and cover at most 366 days.")
    if action == "launch_campaign":
        result["name"] = _text(raw["name"], "name", 128, 255)
        result["geo_target_ids"] = _ids(raw.get("geo_target_ids", []), "geo_target_ids")
        result["keywords"] = _keywords(raw["keywords"])
        start = _timestamp(raw["start_time"], "start_time")
        end = _timestamp(raw["end_time"], "end_time")
        if not timedelta(0) < end - start < timedelta(days=91):
            _fail("end_time must follow start_time; Search total-budget flights require 3 to 90 account-local days.")
        result["start_time"] = _utc_text(start)
        result["end_time"] = _utc_text(end)
    if "final_url" in raw:
        url = _text(raw["final_url"], "final_url", 2048, 2048)
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None and not parsed.fragment
            parsed.port
        except ValueError:
            valid = False
        if not valid or "\\" in url or any(char.isspace() for char in url):
            _fail("final_url must be an HTTPS URL without credentials, fragment, whitespace or backslashes.")
        result["final_url"] = url
        result["headlines"] = _ad_texts(raw["headlines"], "headlines", 3, 15, 30)
        result["descriptions"] = _ad_texts(raw["descriptions"], "descriptions", 2, 4, 90)
    return result


def _request(token: str, suffix: str, *, customer: str = "", body: JSONObject | None = None, stage: str = "") -> JSONObject:
    path = f"customers/{customer}/{suffix}" if customer else f"customers:{suffix}"
    headers = {"authorization": f"Bearer {token}"}
    operation = f"{'POST' if body is not None else 'GET'} {path}" + (f" ({stage})" if stage else "")
    try:
        result = json_request("POST" if body is not None else "GET", f"{API_BASE}/{path}", headers=headers, body=body,
                            failure_message="Google Ads request failed.", invalid_response_message="Google Ads returned an invalid response.", max_error_bytes=MAX_ADS_ERROR_BYTES)
    except WebRequestError as exc:
        if exc.status == 401:
            cause = AdsProviderError("Google Ads authentication failed.", operation, status=exc.status, body=exc.body, body_truncated=exc.body_truncated)
            raise IntegrationReconnectRequired("Google rejected the Ads credentials. Reconnect Google Ads in Home > Integrations.") from cause
        messages = {
            403: "Google Ads denied access. Check this Cloud project's production API access, Ads scope and account permissions.",
            429: "Google Ads quota or rate limit reached. Wait for capacity before making another request.",
            400: "Google Ads rejected the request. Check account eligibility, targeting IDs, ad policies and action inputs in the integration guide.",
        }
        if exc.status in messages:
            raise AdsProviderError(messages[exc.status], operation, status=exc.status, body=exc.body, body_truncated=exc.body_truncated) from exc
        warning = transport_or_unmapped_provider_error("Google Ads", operation, exc)
        if isinstance(warning, ProviderWarning):
            warning.body_truncated = exc.body_truncated
            warning.response_body = ads_error_body(exc.body).decode("utf-8", "replace")
        else:
            warning = AdsProviderError(str(warning), operation, status=exc.status, body=exc.body, body_truncated=exc.body_truncated)
        raise warning from exc
    except RuntimeError as exc:
        raise AdsProviderError(str(exc), operation) from exc
    if result.get("error") or result.get("partialFailureError"):
        raise AdsProviderError("Google Ads returned an error response.", operation,
                               status=200, body=ads_error_body(json.dumps(result).encode("utf-8")))
    return result


def _object(value: JSONValue, name: str) -> JSONObject:
    if not isinstance(value, dict):
        raise RuntimeError(f"Google Ads returned invalid {name} data.")
    return value


def _rows(response: JSONObject) -> list[JSONObject]:
    value = response.get("results", [])
    if not isinstance(value, list):
        raise RuntimeError("Google Ads returned invalid query rows.")
    return [_object(item, "query row") for item in value]


def _search(token: str, customer: str, query: str) -> list[JSONObject]:
    # Only a fixed category from an internally constructed query is recorded.
    source = re.search(r"\bFROM ([a-z_]+)\b", query, re.ASCII)
    stage = {
        "customer": "account preflight", "campaign": "campaign read",
        "ad_group": "ad group read", "campaign_criterion": "campaign targeting read",
        "ad_group_criterion": "keyword read", "ad_group_ad": "ad creative read",
        "keyword_view": "keyword report", "search_term_view": "search term report",
    }.get(source.group(1) if source else "", "Ads query")
    return _rows(_request(token, "googleAds:search", customer=customer, body={"query": query}, stage=stage))


def _direct_accounts(token: str) -> list[str]:
    value = _request(token, "listAccessibleCustomers").get("resourceNames", [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not re.fullmatch(r"customers/[0-9]{10}", item, re.ASCII) for item in value):
        raise RuntimeError("Google Ads returned invalid accessible account IDs.")
    return [cast(str, item).split("/")[1] for item in value]


def _string(record: JSONObject, key: str, maximum: int = 512) -> str:
    value = record.get(key, "")
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise RuntimeError(f"Google Ads returned invalid {key}.")
    return value


def _decimal(record: JSONObject, key: str) -> str:
    value = record.get(key, "0")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value, re.ASCII):
        raise RuntimeError(f"Google Ads returned invalid {key}.")
    return value


def _bool(record: JSONObject, key: str) -> bool:
    value = record.get(key, False)
    if type(value) is not bool:
        raise RuntimeError(f"Google Ads returned invalid {key}.")
    return cast(bool, value)


def _account_row(record: JSONObject) -> JSONObject:
    return {"customer_id": _id(record.get("id"), "provider customer ID", customer=True),
            "name": _string(record, "descriptiveName"), "currency_code": _string(record, "currencyCode", 3),
            "time_zone": _string(record, "timeZone", 100), "manager": _bool(record, "manager"), "test_account": _bool(record, "testAccount")}


def _account(token: str, customer: str) -> JSONObject:
    rows = _search(token, customer, f"SELECT {CUSTOMER_FIELDS} FROM customer LIMIT 1")
    if len(rows) != 1:
        _fail("Google Ads account could not be resolved.")
    account = _account_row(_object(rows[0].get("customer"), "customer"))
    if account["customer_id"] != customer:
        _fail("Google Ads returned a different customer.")
    return account


def _scope(token: str, values: JSONObject) -> JSONObject:
    customer = cast(str, values["customer_id"])
    if customer not in _direct_accounts(token):
        _fail("customer_id is not directly accessible. Give the connected Google user direct access to this advertiser account.")
    account = _account(token, customer)
    if account["manager"]:
        _fail("Select an advertiser Ads account, not a manager, for campaign operations.")
    if not re.fullmatch(r"[A-Z]{3}", cast(str, account["currency_code"]), re.ASCII) or not account["time_zone"]:
        raise RuntimeError("Google Ads did not return account currency and time zone.")
    return account


def _campaign_row(row: JSONObject) -> JSONObject:
    campaign = _object(row.get("campaign"), "campaign")
    budget = _object(row.get("campaignBudget"), "campaign budget")
    return {"campaign_id": _id(campaign.get("id"), "campaign ID"), "name": _string(campaign, "name"),
            "status": _string(campaign, "status"), "budget_resource": _string(campaign, "campaignBudget"),
            "budget_period": _string(budget, "period"), "daily_budget_micros": _decimal(budget, "amountMicros"),
            "total_budget_micros": _decimal(budget, "totalAmountMicros"),
            "start_time": _string(campaign, "startDateTime", 19), "end_time": _string(campaign, "endDateTime", 19),
            "bidding_strategy": _string(campaign, "biddingStrategyType"), "primary_status": _string(campaign, "primaryStatus"),
            "shared_budget": _bool(budget, "explicitlyShared") or int(_decimal(budget, "referenceCount")) > 1}


def _campaign(token: str, values: JSONObject, campaign_id: str) -> JSONObject:
    rows = _search(token, cast(str, values["customer_id"]),
        f"SELECT {CAMPAIGN_FIELDS} FROM campaign WHERE campaign.id = {campaign_id} AND campaign.status != 'REMOVED' LIMIT 1")
    if len(rows) != 1 or _object(rows[0].get("campaign"), "campaign").get("advertisingChannelType") != "SEARCH":
        _fail("Target must be an existing, non-removed Search campaign in this customer.")
    result = _campaign_row(rows[0])
    if result["campaign_id"] != campaign_id:
        _fail("Google Ads returned a different campaign.")
    return result


def _group_row(row: JSONObject) -> JSONObject:
    group = _object(row.get("adGroup"), "ad group")
    campaign = _object(row.get("campaign"), "campaign")
    return {"ad_group_id": _id(group.get("id"), "ad group ID"), "campaign_id": _id(campaign.get("id"), "campaign ID"),
            "name": _string(group, "name"), "status": _string(group, "status"), "cpc_bid_micros": _decimal(group, "cpcBidMicros")}


def _target(token: str, action: str, values: JSONObject) -> JSONObject:
    if action == "launch_campaign":
        return {}
    return {"campaign": _campaign(token, values, cast(str, values["campaign_id"]))}


def _number(record: JSONObject, key: str) -> float:
    value = record.get(key, 0)
    if type(value) not in (int, float) or not math.isfinite(cast(float, value)):
        raise RuntimeError(f"Google Ads returned invalid {key}.")
    return float(cast(float, value))


def _report(token: str, values: JSONObject) -> list[JSONObject]:
    kind = cast(str, values["report_type"])
    fields = "segments.date, campaign.id, campaign.name, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, metrics.conversions_value"
    source = "campaign"
    if kind == "keywords":
        source = "keyword_view"
        fields += ", ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type"
    elif kind == "search_terms":
        source = "search_term_view"
        fields += ", ad_group.id, search_term_view.search_term"
    query = (f"SELECT {fields} FROM {source} WHERE campaign.advertising_channel_type = 'SEARCH' "
             f"AND segments.date BETWEEN '{values['start_date']}' AND '{values['end_date']}'")
    if "campaign_id" in values:
        query += f" AND campaign.id = {values['campaign_id']}"
    query += f" ORDER BY segments.date DESC, metrics.cost_micros DESC LIMIT {cast(int, values['limit']) + 1}"
    rows = _search(token, cast(str, values["customer_id"]), query)
    result: list[JSONObject] = []
    for row in rows:
        campaign = _object(row.get("campaign"), "campaign")
        metrics = _object(row.get("metrics", {}), "metrics")
        group = _object(row.get("adGroup", {}), "ad group")
        criterion = _object(row.get("adGroupCriterion", {}), "keyword criterion")
        keyword = _object(criterion.get("keyword", {}), "keyword")
        search_term = _object(row.get("searchTermView", {}), "search term")
        result.append({
            "date": _string(_object(row.get("segments"), "segments"), "date", 10),
            "campaign_id": _id(campaign.get("id"), "campaign ID"), "campaign_name": _string(campaign, "name"),
            "ad_group_id": _string(group, "id", 19), "criterion_id": _string(criterion, "criterionId", 19),
            "keyword": _string(keyword, "text", 320), "match_type": _string(keyword, "matchType", 30),
            "search_term": _string(search_term, "searchTerm", 2048),
            "impressions": _decimal(metrics, "impressions"), "clicks": _decimal(metrics, "clicks"), "cost_micros": _decimal(metrics, "costMicros"),
            "conversions": _number(metrics, "conversions"), "conversion_value": _number(metrics, "conversionsValue"),
        })
    return result


def _resource(customer: str, kind: str, identifier: str) -> str:
    return f"customers/{customer}/{kind}/{identifier}"


def _create(kind: str, value: JSONObject) -> JSONObject:
    return {kind + "Operation": {"create": value}}


def _ad_operation(group_resource: str, values: JSONObject) -> JSONObject:
    return _create("adGroupAd", {"adGroup": group_resource, "status": "ENABLED", "ad": {
        "finalUrls": [values["final_url"]], "responsiveSearchAd": {
            "headlines": [{"text": text} for text in cast(list[JSONValue], values["headlines"])],
            "descriptions": [{"text": text} for text in cast(list[JSONValue], values["descriptions"])],
        },
    }})


def _keyword_operations(group_resource: str, values: JSONObject) -> list[JSONValue]:
    operations: list[JSONValue] = []
    for item in cast(list[JSONObject], values["keywords"]):
        operations.append(_create("adGroupCriterion", {"adGroup": group_resource, "status": "ENABLED", "negative": False,
            "keyword": {"text": item["text"], "matchType": item["match_type"]}}))
    return operations


def _launch_operations(values: JSONObject, flight: JSONObject) -> list[JSONValue]:
    customer = cast(str, values["customer_id"])
    budget = _resource(customer, "campaignBudgets", "-1")
    campaign = _resource(customer, "campaigns", "-2")
    group = _resource(customer, "adGroups", "-3")
    operations: list[JSONValue] = [
        _create("campaignBudget", {"resourceName": budget, "name": values["name"], "period": "CUSTOM_PERIOD", "totalAmountMicros": str(values["total_budget_micros"]), "deliveryMethod": "STANDARD", "explicitlyShared": False}),
        _create("campaign", {"resourceName": campaign, "name": values["name"], "status": "PAUSED", "advertisingChannelType": "SEARCH", "campaignBudget": budget,
            "targetSpend": {}, "startDateTime": flight["start_date_time"], "endDateTime": flight["end_date_time"],
            "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False, "targetContentNetwork": False, "targetPartnerSearchNetwork": False},
            "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
            "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}),
    ]
    for identifier in cast(list[str], values["geo_target_ids"]):
        operations.append(_create("campaignCriterion", {"campaign": campaign, "location": {"geoTargetConstant": f"geoTargetConstants/{identifier}"}}))
    operations.append(_create("adGroup", {"resourceName": group, "name": values["name"], "campaign": campaign, "status": "ENABLED", "type": "SEARCH_STANDARD"}))
    operations.extend(_keyword_operations(group, values))
    operations.append(_ad_operation(group, values))
    return operations


def _status_operation(resource: str, status: str) -> list[JSONValue]:
    return [{"campaignOperation": {"updateMask": "status", "update": {"resourceName": resource, "status": status}}}]


def _mutate(token: str, values: JSONObject, operations: list[JSONValue], resources: list[str]) -> None:
    response = _request(token, "googleAds:mutate", customer=cast(str, values["customer_id"]),
        body={"mutateOperations": operations, "partialFailure": False})
    results = response.get("mutateOperationResponses")
    if not isinstance(results, list) or len(results) != len(operations) or response.get("partialFailureError"):
        raise RuntimeError("Google Ads mutation outcome is unclear.")
    kinds = {"campaignBudget": "campaignBudgets", "campaign": "campaigns", "campaignCriterion": "campaignCriteria", "adGroup": "adGroups", "adGroupCriterion": "adGroupCriteria", "adGroupAd": "adGroupAds"}
    for operation, result in zip(operations, results):
        operation_key = next(iter(_object(operation, "mutation operation")))
        kind = operation_key.removesuffix("Operation")
        record = _object(result, "mutation result")
        if set(record) != {kind + "Result"}:
            raise RuntimeError("Google Ads mutation returned an unexpected result type.")
        resource = _string(_object(record[kind + "Result"], "mutation resource"), "resourceName")
        identifier = r"[0-9]{1,19}~[0-9]{1,19}" if kind in ("campaignCriterion", "adGroupCriterion", "adGroupAd") else r"[0-9]{1,19}"
        if not re.fullmatch(rf"customers/{values['customer_id']}/{kinds[kind]}/{identifier}", resource, re.ASCII):
            raise RuntimeError("Google Ads mutation returned a resource outside its expected account/type.")
        details = _object(_object(operation, "mutation operation")[operation_key], "operation details")
        if "update" in details and resource != _object(details["update"], "update").get("resourceName"):
            raise RuntimeError("Google Ads mutation returned a different update target.")
        resources.append(resource)


def _single(rows: list[JSONObject], name: str) -> JSONObject:
    if len(rows) != 1:
        _fail(f"New {name} could not be uniquely verified. Kern did not enable the campaign.")
    return rows[0]


def _targeting_details(criterion: JSONObject) -> str:
    """Project only fixed labels and bounded numbers, never raw provider rows."""
    def enum(value: JSONValue, allowed: tuple[str, ...]) -> str:
        if value is None:
            return "MISSING"
        return value if isinstance(value, str) and value in allowed else "UNRECOGNIZED"

    # Documented v25 criterion labels; recognizing a label never permits it.
    kind = enum(criterion.get("type"), ("AD_SCHEDULE", "AGE_RANGE", "APP_PAYMENT_MODEL", "AUDIENCE", "BRAND", "BRAND_LIST",
        "CARRIER", "COMBINED_AUDIENCE", "CONTENT_LABEL", "CUSTOM_AFFINITY", "CUSTOM_AUDIENCE", "CUSTOM_INTENT", "DEVICE",
        "GENDER", "INCOME_RANGE", "IP_BLOCK", "KEYWORD", "KEYWORD_THEME", "LANGUAGE", "LIFE_EVENT", "LISTING_GROUP",
        "LISTING_SCOPE", "LOCAL_SERVICE_ID", "LOCATION", "LOCATION_GROUP", "MOBILE_APPLICATION", "MOBILE_APP_CATEGORY",
        "MOBILE_DEVICE", "NEGATIVE_KEYWORD_LIST", "OPERATING_SYSTEM_VERSION", "PARENTAL_STATUS", "PLACEMENT", "PLACEMENT_LIST",
        "PROXIMITY", "RETAIL_FILTER", "RETAIL_FILTER_BUNDLE", "SEARCH_THEME", "TOPIC", "UNKNOWN", "UNSPECIFIED", "USER_INTEREST",
        "USER_LIST", "VERTICAL_ADS_ITEM_BID", "VERTICAL_ADS_ITEM_GROUP_RULE", "VERTICAL_ADS_ITEM_GROUP_RULE_LIST", "VIDEO_LINEUP",
        "WEBPAGE", "WEBPAGE_LIST", "YOUTUBE_CHANNEL", "YOUTUBE_VIDEO"))
    status = enum(criterion.get("status"), ("ENABLED", "PAUSED", "REMOVED", "UNKNOWN", "UNSPECIFIED"))
    negative = criterion.get("negative", False)
    excluded = str(negative).lower() if type(negative) is bool else "UNRECOGNIZED"
    device = criterion.get("device")
    device_type = enum(device.get("type") if isinstance(device, dict) else None,
        ("DESKTOP", "MOBILE", "TABLET", "CONNECTED_TV", "OTHER", "UNKNOWN", "UNSPECIFIED"))
    modifier = criterion.get("bidModifier")
    bid_modifier = "MISSING" if modifier is None else "UNRECOGNIZED"
    if type(modifier) in (int, float) and 0 <= cast(float, modifier) <= 10:
        bid_modifier = format(cast(float, modifier), ".9g")
    return f"type={kind}, negative={excluded}, status={status}, device={device_type}, bid_modifier={bid_modifier}"


def _verify_launch(token: str, values: JSONObject, flight: JSONObject, resources: list[str]) -> JSONObject:
    customer = cast(str, values["customer_id"])
    budget_resource, campaign_resource = resources[:2]
    campaign_id = campaign_resource.rsplit("/", 1)[1]
    group_resource = resources[2 + len(cast(list[JSONValue], values["geo_target_ids"]))]
    group_id = group_resource.rsplit("/", 1)[1]
    fields = CAMPAIGN_FIELDS + ", campaign.bidding_strategy, campaign.target_spend.cpc_bid_ceiling_micros, campaign.target_spend.target_spend_micros, campaign.network_settings.target_google_search, campaign.network_settings.target_search_network, campaign.network_settings.target_content_network, campaign.network_settings.target_partner_search_network, campaign.geo_target_type_setting.positive_geo_target_type, campaign.contains_eu_political_advertising, campaign_budget.delivery_method"
    row = _single(_search(token, customer, f"SELECT {fields} FROM campaign WHERE campaign.id = {campaign_id} LIMIT 2"), "campaign")
    campaign = _object(row.get("campaign"), "new campaign")
    budget = _object(row.get("campaignBudget"), "new budget")
    network = _object(campaign.get("networkSettings"), "network settings")
    expected = {"id": campaign_id, "name": values["name"], "status": "PAUSED", "advertisingChannelType": "SEARCH", "campaignBudget": budget_resource,
        "startDateTime": flight["start_date_time"], "endDateTime": flight["end_date_time"], "biddingStrategyType": "TARGET_SPEND",
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}
    if any(campaign.get(key) != value for key, value in expected.items()):
        _fail("New campaign configuration differs from the approved plan.")
    spend = _object(campaign.get("targetSpend", {}), "automatic bidding")
    if (campaign.get("biddingStrategy") or _decimal(spend, "cpcBidCeilingMicros") != "0" or _decimal(spend, "targetSpendMicros") != "0"
            or not _bool(network, "targetGoogleSearch") or any(_bool(network, key) for key in ("targetSearchNetwork", "targetContentNetwork", "targetPartnerSearchNetwork"))
            or _object(campaign.get("geoTargetTypeSetting"), "geo setting").get("positiveGeoTargetType") != "PRESENCE"):
        _fail("New campaign bidding, network or location mode differs from approval.")
    if (_string(budget, "period") != "CUSTOM_PERIOD" or _decimal(budget, "totalAmountMicros") != str(values["total_budget_micros"])
            or _decimal(budget, "amountMicros") != "0" or _bool(budget, "explicitlyShared") or _decimal(budget, "referenceCount") != "1"
            or _string(budget, "deliveryMethod") != "STANDARD"):
        _fail("New campaign total budget differs from approval.")
    geo_rows = _search(token, customer, "SELECT campaign_criterion.type, campaign_criterion.negative, campaign_criterion.status, "
        "campaign_criterion.device.type, campaign_criterion.bid_modifier, campaign_criterion.location.geo_target_constant "
        f"FROM campaign_criterion WHERE campaign.id = {campaign_id} AND campaign_criterion.status != 'REMOVED' LIMIT 11")
    locations: list[str] = []
    for geo_row in geo_rows:
        criterion = _object(geo_row.get("campaignCriterion"), "campaign criterion")
        if criterion.get("type") != "LOCATION" or criterion.get("negative", False) is not False:
            _fail(f"New campaign has unexpected targeting criteria ({_targeting_details(criterion)}). Kern did not enable the campaign.")
        locations.append(_string(_object(criterion.get("location"), "location"), "geoTargetConstant"))
    if sorted(locations) != sorted(f"geoTargetConstants/{identifier}" for identifier in cast(list[str], values["geo_target_ids"])):
        _fail("New campaign locations differ from approval.")
    group_row = _single(_search(token, customer, "SELECT ad_group.resource_name, ad_group.name, ad_group.status, ad_group.type "
        f"FROM ad_group WHERE campaign.id = {campaign_id} AND ad_group.status != 'REMOVED' LIMIT 2"), "ad group")
    group = _object(group_row.get("adGroup"), "new ad group")
    if any(group.get(key) != value for key, value in {"resourceName": group_resource, "name": values["name"], "status": "ENABLED", "type": "SEARCH_STANDARD"}.items()):
        _fail("New ad group differs from approval.")
    keyword_rows = _search(token, customer, "SELECT ad_group_criterion.resource_name, ad_group_criterion.type, ad_group_criterion.status, ad_group_criterion.negative, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type "
        f"FROM ad_group_criterion WHERE ad_group.id = {group_id} AND ad_group_criterion.status != 'REMOVED' LIMIT 21")
    keywords: list[tuple[str, str]] = []
    keyword_resources: list[str] = []
    for keyword_row in keyword_rows:
        criterion = _object(keyword_row.get("adGroupCriterion"), "new keyword")
        if criterion.get("type") != "KEYWORD" or criterion.get("status") != "ENABLED" or _bool(criterion, "negative"):
            _fail("New ad group has unexpected keyword criteria.")
        keyword = _object(criterion.get("keyword"), "keyword")
        keywords.append((_string(keyword, "text"), _string(keyword, "matchType")))
        keyword_resources.append(_string(criterion, "resourceName"))
    if (sorted(keywords) != sorted((cast(str, item["text"]), cast(str, item["match_type"])) for item in cast(list[JSONObject], values["keywords"]))
            or sorted(keyword_resources) != sorted(resource for resource in resources if "/adGroupCriteria/" in resource)):
        _fail("New keywords differ from approval.")
    ad_row = _single(_search(token, customer, "SELECT ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2, ad_group_ad.policy_summary.review_status, ad_group_ad.policy_summary.approval_status "
        f"FROM ad_group_ad WHERE ad_group.id = {group_id} AND ad_group_ad.status != 'REMOVED' LIMIT 2"), "responsive ad")
    group_ad = _object(ad_row.get("adGroupAd"), "new ad")
    ad = _object(group_ad.get("ad"), "creative")
    rsa = _object(ad.get("responsiveSearchAd"), "responsive ad")
    if (group_ad.get("resourceName") != resources[-1] or group_ad.get("status") != "ENABLED" or ad.get("type") != "RESPONSIVE_SEARCH_AD"
            or ad.get("finalUrls") != [values["final_url"]] or rsa.get("path1") or rsa.get("path2")):
        _fail("New ad or landing page differs from approval.")
    for field in ("headlines", "descriptions"):
        texts = rsa.get(field)
        if not isinstance(texts, list):
            _fail("Google did not return the new ad copy.")
        records = [_object(item, "ad text") for item in texts]
        if ([_string(item, "text") for item in records] != values[field]
                or any(item.get("pinnedField") not in (None, "UNSPECIFIED") for item in records)):
            _fail("New ad copy differs from approval.")
    policy = _object(group_ad.get("policySummary", {}), "ad policy")
    if policy.get("approvalStatus") == "DISAPPROVED":
        _fail("Google disapproved the new ad. Inspect Google Ads before a fresh proposal.")
    return {"review_status": _string(policy, "reviewStatus"), "approval_status": _string(policy, "approvalStatus")}


def _launch_access(api: HostAPI, values: JSONObject, approved_identity: JSONObject, account: JSONObject) -> str:
    token = CREDENTIALS.access_token(api)
    identity = CREDENTIALS.refresh_identity(api, token)
    if identity["id"] != approved_identity.get("id") or _scope(token, values) != account:
        _fail("Google identity or Ads account changed before activation.")
    return token


def _launch(api: HostAPI, values: JSONObject, account: JSONObject, identity: JSONObject, flight: JSONObject, token: str) -> ApprovalResult:
    resources: list[str] = []
    phase = "creating the paused campaign"
    try:
        _mutate(token, values, _launch_operations(values, flight), resources)
        phase = "verifying the new campaign before enabling"
        token = _launch_access(api, values, identity, account)
        review = _verify_launch(token, values, flight, resources)
        if _flight(values, account) != flight:
            _fail("Approved flight changed before activation.")
        phase = "enabling the verified campaign"
        campaign_resource = resources[1]
        _mutate(token, values, _status_operation(campaign_resource, "ENABLED"), resources)
        return ApprovalExecuted(f"Google Ads new Search campaign configured ENABLED: {campaign_resource}. Maximize Clicks; total budget {_money(values['total_budget_micros'])} {account['currency_code']}. "
            f"Flight: {values['start_time']} to {values['end_time']}. Google review: {review['review_status'] or 'not yet reported'}; approval: {review['approval_status'] or 'not yet reported'}. "
            "Configured ENABLED is not confirmation of approval or actual delivery. Google may begin delivery during or after review within this flight without another Kern approval. Resources: " + ", ".join(dict.fromkeys(resources)))
    except ProviderWarning as exc:
        message = f"Google Ads launch failed while {phase}; outcome may be uncertain. Confirmed resources: {', '.join(resources) or 'none returned'}. Inspect Google Ads before another proposal."
        if phase == "enabling the verified campaign":
            message += " Google may already have started paid delivery."
        warning = ProviderWarning("Google Ads", f"{phase}: {exc.operation}", message, status=exc.status, body=ads_error_body(exc.response_body.encode("utf-8")))
        warning.diagnostic_context = ads_failure_context("google_ads", "launch_campaign", exc, phase=phase, confirmed=resources)
        raise warning from exc
    except (IntegrationReconnectRequired, ToolInputValidationError, RuntimeError) as exc:
        report_ads_failure("google_ads", "launch_campaign", exc, phase=phase, confirmed=resources)
        return ActionFailed(f"Google Ads launch failed while {phase}: {exc} Confirmed resources: {', '.join(resources) or 'none returned'}. Inspect Google Ads before another proposal; an uncertain enable may have started spending.", reconnect_required=isinstance(exc, IntegrationReconnectRequired))


def _money(value: JSONValue) -> str:
    micros = int(cast(int, value))
    fraction = f"{micros % 1_000_000:06d}".rstrip("0")
    return str(micros // 1_000_000) + ("." + fraction if fraction else "")


def _summary(action: str, values: JSONObject, account: JSONObject, target: JSONObject) -> str:
    account_name = clip_text(cast(str, account["name"]) or str(values["customer_id"]), 60)
    account_label = f"{account_name} ({values['customer_id']})"
    if action == "launch_campaign":
        name = clip_text(cast(str, values["name"]), 100)
        return f"Launch new Search campaign {name} in {account_label}, total budget {_money(values['total_budget_micros'])} {account['currency_code']}, Maximize Clicks. One approval permits paid delivery during the flight."
    campaign = _object(target.get("campaign"), "approval campaign")
    campaign_name = clip_text(cast(str, campaign["name"]), 80)
    return f"End {campaign_name} ({values['campaign_id']}) in {account_label}. Stop delivery and preserve reporting; no Kern resume."


class GoogleAdsTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return CREDENTIALS

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        validated = False
        try:
            values = _input(action, tool_input)
            validated = True
            if action == "list_locations":
                return ActionExecuted(list_locations(values))
            token = CREDENTIALS.access_token(api)
            if action == "list_accounts":
                ids = _direct_accounts(token)
                return ActionExecuted({"customer_ids": cast(list[JSONValue], ids[:50]), "truncated": len(ids) > 50})
            account = _scope(token, values)
            if ACTIONS[action].approval == "operator":
                identity = CREDENTIALS.refresh_identity(api, token)
                target = _target(token, action, values)
                payload: JSONObject = {"tool_id": MANIFEST.tool_id, "action": action, "google_account": {"id": identity["id"], "email": identity["label"]},
                    "input": values, "account": account, "current_target": target}
                if action == "launch_campaign":
                    payload["flight"] = _flight(values, account)
                pending = api.approvals.request(action_id=action, summary=_summary(action, values, account, target), payload=payload)
                return ActionPendingApproval(pending.approval_id, pending.summary)
            customer = cast(str, values["customer_id"])
            limit = cast(int, values["limit"])
            if action == "list_campaigns":
                raw = _search(token, customer, f"SELECT {CAMPAIGN_FIELDS} FROM campaign WHERE campaign.advertising_channel_type = 'SEARCH' AND campaign.status != 'REMOVED' ORDER BY campaign.id LIMIT {limit + 1}")
                rows = [_campaign_row(row) for row in raw]
            elif action == "list_ad_groups":
                _campaign(token, values, cast(str, values["campaign_id"]))
                raw = _search(token, customer,
                    "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, campaign.id "
                    f"FROM ad_group WHERE campaign.id = {values['campaign_id']} AND ad_group.status != 'REMOVED' ORDER BY ad_group.id LIMIT {limit + 1}")
                rows = [_group_row(row) for row in raw]
            else:
                rows = _report(token, values)
            return ActionExecuted({"account": account, "rows": cast(list[JSONValue], rows[:limit]), "truncated": len(rows) > limit})
        except IntegrationReconnectRequired as exc:
            report_ads_failure("google_ads", action, exc, phase="proposal" if ACTIONS[action].approval == "operator" else "read")
            return ActionFailed(str(exc), reconnect_required=True)
        except UnmappedProviderError as exc:
            exc.response_body = ads_error_body(exc.response_body.encode("utf-8")).decode("utf-8")
            exc.diagnostic_context.update(ads_failure_context("google_ads", action, exc, phase="proposal" if ACTIONS[action].approval == "operator" else "read"))
            raise
        except (ToolInputValidationError, RuntimeError) as exc:
            if validated:
                report_ads_failure("google_ads", action, exc, phase="proposal" if ACTIONS[action].approval == "operator" else "read")
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            payload = approval.payload
            action = payload.get("action")
            if (payload.get("tool_id") != MANIFEST.tool_id or not isinstance(action, str) or action != approval.action_id
                    or action not in ACTIONS or ACTIONS[action].approval != "operator"):
                _fail("Invalid Google Ads approval action.")
            values = _input(action, _object(payload.get("input"), "approval input"))
            token = CREDENTIALS.access_token(api)
            identity = CREDENTIALS.refresh_identity(api, token)
            approved_identity = _object(payload.get("google_account"), "approval identity")
            if approved_identity.get("id") != identity["id"]:
                _fail("Connected Google identity changed. Queue a new approval.")
            account = _scope(token, values)
            target = _target(token, action, values)
            approved_account = _object(payload.get("account"), "approval account")
            if action == "launch_campaign":
                flight = _flight(values, account)
                if account != approved_account or flight != payload.get("flight") or target != payload.get("current_target"):
                    _fail("Google Ads account or approved flight changed. Queue a new approval.")
                return _launch(api, values, account, approved_identity, flight, token)
            approved_target = _object(payload.get("current_target"), "approval target")
            approved_campaign = _object(approved_target.get("campaign"), "approval campaign")
            if approved_account.get("customer_id") != account["customer_id"] or approved_campaign.get("campaign_id") != values["campaign_id"]:
                _fail("Google Ads stop target does not match approval.")
            resources: list[str] = []
            resource = _resource(cast(str, values["customer_id"]), "campaigns", cast(str, values["campaign_id"]))
            try:
                _mutate(token, values, _status_operation(resource, "PAUSED"), resources)
            except ProviderWarning as exc:
                warning = ProviderWarning("Google Ads", f"ending campaign: {exc.operation}", f"Google Ads stop outcome is uncertain for {resource}. Inspect Google Ads before another proposal.", status=exc.status, body=ads_error_body(exc.response_body.encode("utf-8")))
                warning.diagnostic_context = ads_failure_context("google_ads", approval.action_id, exc, phase="ending campaign", confirmed=(resource,))
                raise warning from exc
            except RuntimeError as exc:
                report_ads_failure("google_ads", approval.action_id, exc, phase="ending campaign", confirmed=(resource,))
                return ActionFailed(f"Google Ads stop outcome could not be confirmed for {resource}: {exc} Inspect Google Ads before another proposal.")
            return ApprovalExecuted(f"Google Ads campaign set to PAUSED: {resource}. Stopping may take time; past delivery remains billable. Reporting is preserved. Kern offers no resume; Google Ads users can resume there.")

        except IntegrationReconnectRequired as exc:
            report_ads_failure("google_ads", approval.action_id, exc, phase="approval revalidation")
            return ActionFailed(str(exc), reconnect_required=True)
        except ProviderWarning as exc:
            exc.response_body = ads_error_body(exc.response_body.encode("utf-8")).decode("utf-8")
            if "phase" not in exc.diagnostic_context:
                exc.diagnostic_context.update(ads_failure_context("google_ads", approval.action_id, exc, phase="approval revalidation"))
            raise
        except (ToolInputValidationError, RuntimeError) as exc:
            report_ads_failure("google_ads", approval.action_id, exc, phase="approval revalidation")
            return ActionFailed(str(exc))


BUNDLED_TOOL = GoogleAdsTool()
