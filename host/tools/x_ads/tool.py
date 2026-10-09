"""Bounded reads and exact, single-use approvals for existing-post promotion."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalResult
from host.tools.shared.inputs import clip_text, int_field
from host.tools.x_ads.api import Client, complete_list, object_data, page_data
from host.tools.x_ads.manifest import LAUNCH, LAUNCH_REQUIRED, MANIFEST, METRICS, OBJECTIVES
from host.tools.x_ads import shapes as s

ID_RE = re.compile(r"^[a-z0-9]{1,32}$")
POST_RE = re.compile(r"^[1-9][0-9]{0,24}$")
TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
WRITE_ROLES = {"ACCOUNT_ADMIN", "AD_MANAGER"}
MAX_MICROS = 1_000_000_000_000
LOOKUPS = {"LOCATION": "locations", "LANGUAGE": "languages", "INTEREST": "interests"}
TARGET_TYPES = {*LOOKUPS, "BROAD_KEYWORD", "PHRASE_KEYWORD", "SIMILAR_TO_FOLLOWERS_OF_USER"}
MISMATCH = "The approved advertiser, creative, audience or settings changed. Request a new approval."


def _digest(value: JSONValue) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _id(value: JSONValue | None, name: str, *, decimal: bool = False) -> str:
    if not isinstance(value, str) or not (POST_RE if decimal else ID_RE).fullmatch(value):
        raise ValueError(f"X Ads {name} must be a valid {'decimal' if decimal else 'base-36'} id.")
    return value


def _text(value: JSONValue | None, name: str, *, chars: int = 255, byte_limit: int = 1024) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > chars
            or len(value.encode("utf-8")) > byte_limit or any(ord(c) < 32 for c in value)):
        raise ValueError(f"X Ads {name} must be nonblank text of at most {chars} characters and {byte_limit} UTF-8 bytes, without control characters.")
    return value


def _micros(value: JSONValue | None, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,12}", value) or int(value) > MAX_MICROS:
        raise ValueError(f"X Ads {name} must be a positive integer micros string, at most {MAX_MICROS}.")
    return value


def _time(value: JSONValue | None, name: str) -> datetime:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValueError(f"X Ads {name} must be a UTC second in YYYY-MM-DDTHH:MM:SSZ format.")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"X Ads {name} is not a valid date.") from exc


def _flight(values: JSONObject, *, future_end: bool) -> None:
    start = _time(values.get("start_time"), "start_time")
    end = _time(values.get("end_time"), "end_time")
    if end <= start:
        raise ValueError("X Ads end_time must be later than start_time.")
    if future_end and end <= datetime.now(timezone.utc):
        raise ValueError("The X Ads flight has ended. Set a future end_time and request a new approval.")


def _budgets(values: JSONObject) -> None:
    daily = int(_micros(values.get("daily_budget_amount_local_micro"), "daily budget"))
    total = int(_micros(values.get("total_budget_amount_local_micro"), "total budget"))
    if daily > total:
        raise ValueError("X Ads daily budget must not exceed total budget.")


def _targets(value: JSONValue | None) -> list[JSONObject]:
    if not isinstance(value, list) or not 0 <= len(value) <= 20:
        raise ValueError("X Ads targeting needs an explicit array of 0-20 inclusion criteria.")
    result: list[JSONObject] = []
    seen = set()
    for criterion in value:
        if not isinstance(criterion, dict) or set(criterion) != {"type", "value"}:
            raise ValueError("Each X Ads targeting criterion needs type and value only.")
        kind, target = criterion["type"], criterion["value"]
        if not isinstance(kind, str) or kind not in TARGET_TYPES:
            raise ValueError("Unsupported X Ads targeting type.")
        target = _text(target, "targeting value", chars=80, byte_limit=320)
        patterns = {"LOCATION": r"[a-f0-9]{16}", "LANGUAGE": r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", "INTEREST": r"[0-9]{1,12}", "SIMILAR_TO_FOLLOWERS_OF_USER": POST_RE.pattern}
        if kind in patterns and not re.fullmatch(patterns[kind], target):
            raise ValueError(f"Use a numeric X user ID for {kind}." if kind == "SIMILAR_TO_FOLLOWERS_OF_USER" else f"Use a lookup_targeting value for {kind}.")
        if (kind, target) in seen:
            raise ValueError("X Ads targeting criteria must be unique.")
        seen.add((kind, target))
        result.append({"type": kind, "value": target})
    return result


def _page(tool_input: JSONObject, api: HostAPI) -> tuple[dict[str, str], int]:
    count = int_field(tool_input, "count", provider="X Ads", default=20, low=1, high=50)
    params = {"count": str(count)}
    if "cursor" in tool_input:
        cursor = _text(tool_input["cursor"], "cursor", chars=1024, byte_limit=1024)
        params["cursor"] = api.outbound.guard_request_parameter_string(cursor, allow_machine_tokens=True)
    return params, count


def _entity(client: Client, path: str, entity_id: str) -> JSONObject:
    row = object_data(client.request("GET", path))
    if row.get("id") != entity_id or row.get("deleted") is True:
        raise ValueError("X Ads did not confirm the selected entity's identity or it was deleted.")
    return row


def _account(client: Client, account_id: str, *, write: bool = False) -> tuple[JSONObject, JSONObject]:
    row = _entity(client, f"/accounts/{account_id}", account_id)
    access = object_data(client.request("GET", f"/accounts/{account_id}/authenticated_user_access"))
    _id(access.get("user_id"), "authenticated user", decimal=True)
    permissions = access.get("permissions")
    if not isinstance(permissions, list) or any(not isinstance(role, str) for role in permissions):
        raise RuntimeError("X Ads did not return authenticated-user permissions.")
    if write and not any(role in WRITE_ROLES for role in permissions):
        raise ValueError("This X Ads user needs ACCOUNT_ADMIN or AD_MANAGER permission for advertising writes.")
    return row, {"user_id": access["user_id"], "permissions": permissions}


def _promoter(client: Client, account_id: str, promoter_id: str) -> JSONObject:
    row = _entity(client, f"/accounts/{account_id}/promotable_users/{promoter_id}", promoter_id)
    _id(row.get("user_id"), "promotable user", decimal=True)
    if row.get("promotable_user_type") != "FULL":
        raise ValueError("Only a FULL promotable user can be selected in this initial X Ads flow.")
    return row


def _post_view(row: JSONObject, *, strict: bool) -> JSONObject:
    post_id = _id(row.get("tweet_id") or row.get("id_str"), "published post", decimal=True)
    user = row.get("user")
    if not isinstance(user, dict):
        raise RuntimeError("X Ads did not return the post author.")
    author_id = _id(user.get("id_str") or (str(user["id"]) if isinstance(user.get("id"), int) else None), "post author", decimal=True)
    full_text = row.get("full_text")
    if strict and (not isinstance(full_text, str) or row.get("truncated") is True):
        raise ValueError("X Ads must return complete untruncated post text for approval.")
    if not isinstance(full_text, str):
        full_text = s.text(row, "text")
    if len(full_text.encode("utf-8")) > 25000:
        raise ValueError("X Ads post exceeds the 25000-byte creative preview limit.")
    if strict and (row.get("is_quote_status") or row.get("quoted_status_id_str") or row.get("quoted_status_id")
                   or row.get("quoted_status") or row.get("retweeted_status")
                   or row.get("in_reply_to_status_id_str") or row.get("in_reply_to_status_id")):
        raise ValueError("Quote posts, reposts and replies are excluded from this initial X Ads creative flow.")
    if strict and row.get("nullcast") is not False:
        raise ValueError("Select an existing organic published post; promoted-only creative is excluded.")
    if strict and (row.get("card_uri") or row.get("card")):
        raise ValueError("Posts with cards are excluded because this flow cannot snapshot mutable card content.")
    # Reuse the prior promotion work's stable projection, expanding it with
    # edit history and rejecting quoted/reposted/reply content explicitly.
    content: JSONObject = {key: row.get(key) for key in (
        "entities", "extended_entities", "card", "card_uri", "edit_history_tweet_ids",
        "in_reply_to_status_id_str", "quoted_status_id_str", "nullcast", "conversation_settings",
    )}
    content.update({"id": post_id, "author_id": author_id, "text": full_text})
    return {"id": post_id, "author_id": author_id, "author_username": s.text(user, "screen_name"),
            "text": full_text, "created_at": s.text(row, "created_at"), "url": f"https://x.com/i/status/{post_id}", "content_sha256": _digest(content)}


def _creative(row: JSONObject, objective: str) -> None:
    if objective == "WEBSITE_CLICKS":
        entities = row.get("entities")
        urls = entities.get("urls") if isinstance(entities, dict) else None
        valid = False
        for entry in urls if isinstance(urls, list) else []:
            value = entry.get("expanded_url") if isinstance(entry, dict) else None
            if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
                continue
            try:
                parsed = urllib.parse.urlsplit(value)
                if (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
                        and parsed.hostname.lower() not in ("x.com", "www.x.com", "twitter.com", "www.twitter.com", "t.co")):
                    valid = True
            except ValueError:
                pass
        if not valid:
            raise ValueError("WEBSITE_CLICKS requires a complete usable website URL entity already in the existing post.")
    if objective == "VIDEO_VIEWS":
        entities = row.get("extended_entities")
        media = entities.get("media") if isinstance(entities, dict) else None
        if not isinstance(media, list) or not any(isinstance(item, dict) and item.get("type") == "video" for item in media):
            raise ValueError("VIDEO_VIEWS requires a real video already attached to the existing post; images, GIFs and cards are excluded.")


def _post(client: Client, account_id: str, promoter: JSONObject, post_id: str, *, objective: str = "ENGAGEMENTS") -> JSONObject:
    rows, cursor = page_data(client.request("GET", f"/accounts/{account_id}/tweets", {
        "tweet_ids": post_id, "tweet_type": "PUBLISHED", "timeline_type": "ORGANIC",
        "user_id": str(promoter["user_id"]), "count": "1", "trim_user": "false",
    }), limit=1)
    if cursor or len(rows) != 1:
        raise ValueError("X Ads did not confirm the selected published post.")
    result = _post_view(rows[0], strict=True)
    _creative(rows[0], objective)
    if result["id"] != post_id or result["author_id"] != promoter["user_id"]:
        raise ValueError("This post does not belong to the selected FULL promotable user.")
    return result


def _funding(client: Client, account_id: str, funding_id: str, *, live: bool = False) -> JSONObject:
    row = _entity(client, f"/accounts/{account_id}/funding_instruments/{funding_id}", funding_id)
    if not re.fullmatch(r"[A-Z]{3}", s.text(row, "currency")):
        raise ValueError("X Ads did not confirm the funding currency.")
    if live and (row.get("able_to_fund") is not True or row.get("cancelled") is True or row.get("entity_status") != "ACTIVE"):
        raise ValueError("The selected X Ads funding instrument cannot currently fund delivery.")
    return row


def _group_bundle(client: Client, account_id: str, line_item_id: str) -> JSONObject:
    group = _entity(client, f"/accounts/{account_id}/line_items/{line_item_id}", line_item_id)
    targets = complete_list(client, f"/accounts/{account_id}/targeting_criteria", {"line_item_ids": line_item_id})
    promoted = complete_list(client, f"/accounts/{account_id}/promoted_tweets", {"line_item_ids": line_item_id})
    if any(row.get("line_item_id") != line_item_id or row.get("deleted") is True for row in (*targets, *promoted)):
        raise ValueError("X Ads returned an association outside the selected ad group.")
    return {"ad_group": group, "targeting": cast(list[JSONValue], sorted(targets, key=lambda r: s.text(r, "id"))),
            "promoted_posts": cast(list[JSONValue], sorted(promoted, key=lambda r: s.text(r, "id")))}


def _bundle(client: Client, account_id: str, campaign_id: str) -> JSONObject:
    campaign = _entity(client, f"/accounts/{account_id}/campaigns/{campaign_id}", campaign_id)
    groups = complete_list(client, f"/accounts/{account_id}/line_items", {"campaign_ids": campaign_id})
    if len(groups) != 1 or groups[0].get("campaign_id") != campaign_id:
        raise ValueError("New campaign verification requires exactly one ad group.")
    bundle = _group_bundle(client, account_id, _id(groups[0].get("id"), "ad group"))
    if cast(JSONObject, bundle["ad_group"]).get("campaign_id") != campaign_id:
        raise ValueError("This ad group does not belong to the selected campaign.")
    bundle["campaign"] = campaign
    return bundle


def _snapshot(bundle: JSONObject) -> str:
    derived = {"updated_at", "effective_status", "servable", "reasons_not_servable"}
    def stable(value: JSONValue) -> JSONValue:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in derived}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    # Bind configured state, including unknown fields, without copying huge
    # provider snapshots. Derived delivery fields can change with the child
    # activation itself; configured entity_status remains bound separately.
    return _digest(stable(bundle))


def _launch_snapshot(bundle: JSONObject, initial_review: JSONValue | None) -> str:
    """Allow X to accept a pending creative, binding all other state exactly."""
    posts = cast(list[JSONObject], bundle["promoted_posts"])
    current_review = posts[0].get("approval_status")
    if current_review != initial_review and not (initial_review == "PENDING" and current_review == "ACCEPTED"):
        raise ValueError("The X Ads creative review state changed outside the approved launch flow.")
    adjusted: JSONObject = {**bundle, "promoted_posts": [{**posts[0], "approval_status": initial_review}]}
    return _snapshot(adjusted)


def _launch_plan(values: JSONObject) -> JSONObject:
    if not set(LAUNCH_REQUIRED) <= set(values) or not set(values) <= set(LAUNCH):
        raise ValueError("X Ads launch requires all declared launch fields and only supported options.")
    plan: JSONObject = dict(values)
    for key in ("account_id", "funding_instrument_id", "promotable_user_id"):
        _id(plan[key], key)
    _id(plan["post_id"], "post", decimal=True)
    plan["name"] = _text(plan["name"], "name")
    objective = plan.setdefault("objective", "ENGAGEMENTS")
    if not isinstance(objective, str) or objective not in OBJECTIVES:
        raise ValueError("Select ENGAGEMENTS, REACH, WEBSITE_CLICKS or VIDEO_VIEWS.")
    if "audience_expansion" in plan and plan["audience_expansion"] not in ("DEFINED", "EXPANDED", "BROAD"):
        raise ValueError("X Ads audience_expansion must be DEFINED, EXPANDED or BROAD; omit for no expansion.")
    _budgets(plan)
    _flight(plan, future_end=True)
    plan["targeting"] = cast(list[JSONValue], _targets(plan["targeting"]))
    return plan


def _references(client: Client, plan: JSONObject) -> tuple[JSONObject, JSONObject]:
    account_id = str(plan["account_id"])
    account, access = _account(client, account_id, write=True)
    if account.get("approval_status") != "ACCEPTED":
        raise ValueError("The advertiser is not currently accepted for X Ads delivery.")
    funding = _funding(client, account_id, str(plan["funding_instrument_id"]), live=True)
    promoter = _promoter(client, account_id, str(plan["promotable_user_id"]))
    post = _post(client, account_id, promoter, str(plan["post_id"]), objective=str(plan["objective"]))
    references: JSONObject = {"account": s.account(account), "access": access,
        "funding": {key: funding.get(key) for key in ("id", "currency", "type", "deleted")},
        "promotable_user": s.view(promoter, texts=s.PROMOTABLE_TEXTS, flags=("deleted",)), "post": post}
    return references, funding


def _geography(plan: JSONObject) -> str:
    return "Explicit location targeting" if any(row["type"] == "LOCATION" for row in cast(list[JSONObject], plan["targeting"])) else "Worldwide, no location restriction"


def _delivery(plan: JSONObject) -> JSONObject:
    goal, pay_by, billing = OBJECTIVES[str(plan["objective"])]
    return {"objective": plan["objective"], "goal": goal, "expected_pay_by": pay_by,
            "cost_basis": billing, "bid_strategy": "AUTO", "standard_delivery": True,
            "placements": ["TWITTER_TIMELINE"], "audience_expansion": plan.get("audience_expansion"),
            "expansion_description": str(plan.get("audience_expansion", "No expansion")), "geographic_scope": _geography(plan),
            "provider_review": "Configure ACTIVE once. If X review is PENDING, delivery is blocked until X accepts the post, then may start within the approved flight without another Kern write or approval. Rejected or unknown review states stop this launch."}


def _campaign_params(plan: JSONObject) -> dict[str, str]:
    return {**{key: str(plan[key]) for key in s.BUDGETS}, "name": str(plan["name"]),
            "funding_instrument_id": str(plan["funding_instrument_id"]), "budget_optimization": "LINE_ITEM", "entity_status": "PAUSED"}


def _group_params(plan: JSONObject, campaign_id: str) -> dict[str, str]:
    result = {**{key: str(plan[key]) for key in s.BUDGETS}, "campaign_id": campaign_id, "name": str(plan["name"]),
        "objective": str(plan["objective"]), "product_type": "PROMOTED_TWEETS", "placements": "TWITTER_TIMELINE",
        "bid_strategy": "AUTO", "goal": OBJECTIVES[str(plan["objective"])][0], "standard_delivery": "true", "entity_status": "PAUSED",
        "start_time": str(plan["start_time"]), "end_time": str(plan["end_time"])}
    if "audience_expansion" in plan:
        result["audience_expansion"] = str(plan["audience_expansion"])
    # X sets billing from objective/goal. The exact expected default is checked
    # in the response and at both activation barriers, rather than submitting
    # unsupported pay_by overrides for engagement/reach/video objectives.
    return result


def _confirm_settings(row: JSONObject, params: dict[str, str]) -> None:
    for key, value in params.items():
        actual = row.get(key)
        if key == "standard_delivery":
            matches = actual is True
        elif key == "placements":
            matches = actual == value.split(",")
        elif key.endswith("_local_micro"):
            matches = s.money(row, key) == value
        else:
            matches = actual == value
        if not matches:
            raise RuntimeError("X Ads did not confirm the exact requested settings.")


def _verify_created(bundle: JSONObject, plan: JSONObject, currency: JSONValue, *, group_status: str) -> None:
    campaign, group = cast(JSONObject, bundle["campaign"]), cast(JSONObject, bundle["ad_group"])
    campaign_id = _id(campaign.get("id"), "campaign")
    _confirm_settings(campaign, _campaign_params(plan))
    _confirm_settings(group, {**_group_params(plan, campaign_id), "entity_status": group_status})
    if (campaign.get("currency") != currency or group.get("currency") != currency
            or campaign.get("deleted") is not False or group.get("deleted") is not False
            or group.get("pay_by") != OBJECTIVES[str(plan["objective"])][1]
            or group.get("audience_expansion") != plan.get("audience_expansion")
            or group.get("automatic_tweet_promotion") or group.get("creative_source") not in (None, "MANUAL")
            or any(row.get(key) is not None for row in (campaign, group) for key in ("frequency_cap", "duration_in_days"))):
        raise ValueError("X Ads returned delivery or billing settings outside the approved launch terms.")
    rows = cast(list[JSONObject], bundle["targeting"])
    targets = _targets(cast(list[JSONValue], [{"type": row.get("targeting_type"), "value": row.get("targeting_value")} for row in rows]))
    expected = cast(list[JSONObject], plan["targeting"])
    if (sorted((str(r["type"]), str(r["value"])) for r in targets) != sorted((str(r["type"]), str(r["value"])) for r in expected)
            or any(row.get("operator_type") not in (None, "EQ") for row in rows)):
        raise ValueError(MISMATCH)
    promoted = cast(list[JSONObject], bundle["promoted_posts"])
    if (len(promoted) != 1 or promoted[0].get("tweet_id") != plan["post_id"]
            or promoted[0].get("entity_status") != "ACTIVE" or promoted[0].get("approval_status") not in ("ACCEPTED", "PENDING")):
        raise ValueError("Launch requires exactly one active post association with explicit ACCEPTED or PENDING X review. Rejected, missing or unknown review leaves the created parent paused; inspect its IDs.")


def _units(value: JSONValue | None) -> str:
    return format(Decimal(str(value)) / Decimal(1_000_000), "f") if value is not None else "unknown"


def _approval(api: HostAPI, client: Client, action: str, summary: str, payload: JSONObject) -> ActionPendingApproval:
    payload["credential_binding"] = client.binding
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 60_000:
        raise ValueError("This X Ads proposal exceeds the approval preview size limit. Manage it in X Ads.")
    record = api.approvals.request(action_id=action, summary=clip_text(summary, 500), payload=payload)
    return ActionPendingApproval(record.approval_id, record.summary)


def _performance(client: Client, account_id: str, values: JSONObject) -> JSONObject:
    campaign_id = _id(values.get("campaign_id"), "campaign")
    start, end = _time(values.get("start_time"), "start_time"), _time(values.get("end_time"), "end_time")
    if start.minute or start.second or end.minute or end.second or not 0 < (end - start).total_seconds() <= 7 * 86400:
        raise ValueError("Performance needs a whole-hour UTC window greater than zero and at most seven days.")
    campaign = _entity(client, f"/accounts/{account_id}/campaigns/{campaign_id}", campaign_id)
    placement_metrics: list[JSONObject] = []
    for placement in ("ALL_ON_TWITTER", "SPOTLIGHT", "TREND"):
        rows, cursor = page_data(client.request("GET", f"/stats/accounts/{account_id}", {
            "entity": "CAMPAIGN", "entity_ids": campaign_id, "start_time": str(values["start_time"]), "end_time": str(values["end_time"]),
            "granularity": "TOTAL", "placement": placement, "metric_groups": "ENGAGEMENT,BILLING,VIDEO",
        }), limit=1)
        if cursor or len(rows) > 1 or (rows and rows[0].get("id") != campaign_id):
            raise RuntimeError("X Ads returned metrics for a different campaign.")
        raw: JSONObject = {}
        if rows:
            segments = rows[0].get("id_data")
            if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict) or segments[0].get("segment") is not None:
                raise RuntimeError("X Ads returned unexpected segmented metrics.")
            candidate = segments[0].get("metrics")
            if not isinstance(candidate, dict):
                raise RuntimeError("X Ads did not return a metrics object.")
            raw = candidate
        metrics: JSONObject = {}
        for key in (*METRICS, "billed_charge_local_micro"):
            series = raw.get(key)
            value = series[0] if isinstance(series, list) and len(series) == 1 else None
            if key == "billed_charge_local_micro":
                metrics[key] = s.money({key: value}, key)
            else:
                metrics[key] = value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
        placement_metrics.append({"placement": placement, "metrics": metrics})
    totals: JSONObject = {}
    for key in (*METRICS, "billed_charge_local_micro"):
        reported = [cast(JSONObject, bucket["metrics"])[key] for bucket in placement_metrics if cast(JSONObject, bucket["metrics"])[key] is not None]
        if key == "billed_charge_local_micro":
            totals[key] = str(sum(int(str(value)) for value in reported)) if reported else None
        else:
            totals[key] = sum(cast(int | float, value) for value in reported) if reported else None
    return {"campaign_id": campaign_id, "currency": s.text(campaign, "currency"), "start_time": values["start_time"], "end_time": values["end_time"], "metrics": totals, "placement_metrics": cast(list[JSONValue], placement_metrics)}


class Writes:
    """Retain confirmed ids and the attempted stage in one terminal outcome."""
    def __init__(self, client: Client) -> None:
        self.client = client
        self.confirmed: list[str] = []
        self.attempted = ""

    def entity(self, method: str, path: str, params: dict[str, str], label: str, *, expected_id: str | None = None) -> JSONObject:
        self.attempted = label
        row = object_data(self.client.request(method, path, params))
        row_id = _id(row.get("id"), label)
        self.confirmed.append(f"{label} {row_id}")
        if expected_id is not None and row_id != expected_id:
            raise RuntimeError("X Ads did not confirm the requested entity id.")
        _confirm_settings(row, params)
        return row

    def failure(self, error: str) -> ActionFailed:
        if not self.attempted:
            return ActionFailed(error)
        confirmed = "; ".join(self.confirmed) or "none"
        return ActionFailed(f"{error} Last attempted step: {self.attempted}. Confirmed: {confirmed}. Partial changes may remain and the last request may have succeeded. Inspect X Ads and these ids before a new approval. No request was retried or rolled back.")


class XAdsTool:
    manifest = MANIFEST
    credentials = None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        spec = MANIFEST.action(action)
        if spec is None:
            return ActionFailed("Unsupported X Ads action.")
        properties = cast(JSONObject, spec.input_schema["properties"])
        if not set(tool_input) <= set(properties):
            return ActionFailed("X Ads input contains unknown fields.")
        try:
            client = Client(api)
            if spec.approval == "operator":
                return self._propose(client, action, tool_input, api)
            return ActionExecuted(self._read(client, action, tool_input, api))
        except (ValueError, RuntimeError) as exc:
            return ActionFailed(str(exc))

    def _read(self, client: Client, action: str, values: JSONObject, api: HostAPI) -> JSONObject:
        spec = MANIFEST.action(action)
        assert spec is not None
        params, count = _page(values, api) if "count" in cast(JSONObject, spec.input_schema["properties"]) else ({}, 0)
        if action == "list_accounts":
            rows, cursor = page_data(client.request("GET", "/accounts", params), limit=count)
            return {"accounts": [s.account(row) for row in rows], "next_cursor": cursor}
        if action == "lookup_targeting":
            kind = values.get("kind")
            if not isinstance(kind, str) or kind not in LOOKUPS:
                raise ValueError("Select LOCATION, LANGUAGE or INTEREST for lookup_targeting.")
            params["q"] = api.outbound.guard_request_parameter_string(_text(values.get("query"), "query", chars=80, byte_limit=320))
            if kind == "LOCATION":
                location_type = values.get("location_type", "COUNTRIES")
                if location_type not in ("COUNTRIES", "REGIONS", "METROS", "CITIES", "POSTAL_CODES"):
                    raise ValueError("Invalid X Ads location_type.")
                params["location_type"] = str(location_type)
                if "country_code" in values:
                    if not isinstance(values["country_code"], str) or not re.fullmatch(r"[A-Z]{2}", values["country_code"]):
                        raise ValueError("X Ads country_code must be two uppercase letters.")
                    params["country_code"] = str(values["country_code"])
            elif "location_type" in values or "country_code" in values:
                raise ValueError("Location options are only valid for LOCATION lookups.")
            rows, cursor = page_data(client.request("GET", "/targeting_criteria/" + LOOKUPS[kind], params), limit=count)
            return {"options": [{"name": s.text(row, "name"), "type": s.text(row, "targeting_type"), "value": s.text(row, "targeting_value"), "country_code": s.text(row, "country_code"), "location_type": s.text(row, "location_type")} for row in rows], "next_cursor": cursor}
        account_id = _id(values.get("account_id"), "account")
        account, access = _account(client, account_id)
        if action == "get_account":
            return {"account": s.account(account), **access}
        prefix = f"/accounts/{account_id}"
        if action in ("list_funding_sources", "list_promotable_users", "list_campaigns"):
            path, key, converter = {
                "list_funding_sources": ("funding_instruments", "funding_sources", s.funding),
                "list_promotable_users": ("promotable_users", "promotable_users", lambda row: s.view(row, texts=s.PROMOTABLE_TEXTS, flags=("deleted",))),
                "list_campaigns": ("campaigns", "campaigns", s.campaign),
            }[action]
            rows, cursor = page_data(client.request("GET", prefix + "/" + path, params), limit=count)
            return {key: [converter(row) for row in rows], "next_cursor": cursor}
        if action == "list_posts":
            promoter = _promoter(client, account_id, _id(values.get("promotable_user_id"), "promotable record"))
            params.update({"tweet_type": "PUBLISHED", "timeline_type": "ORGANIC", "user_id": str(promoter["user_id"]), "trim_user": "false"})
            rows, cursor = page_data(client.request("GET", prefix + "/tweets", params), limit=count)
            posts = [_post_view(row, strict=False) for row in rows]
            if any(post["author_id"] != promoter["user_id"] for post in posts):
                raise ValueError("X Ads returned a post outside the selected promotable user.")
            return {"posts": cast(list[JSONValue], posts), "next_cursor": cursor}
        if action == "get_campaign":
            campaign_id = _id(values.get("campaign_id"), "campaign")
            row = _entity(client, prefix + "/campaigns/" + campaign_id, campaign_id)
            params["campaign_ids"] = campaign_id
            groups, cursor = page_data(client.request("GET", prefix + "/line_items", params), limit=count)
            if any(group.get("campaign_id") != campaign_id for group in groups):
                raise ValueError("X Ads returned an ad group outside this campaign.")
            return {"campaign": s.campaign(row), "ad_groups": [s.group(group) for group in groups], "next_cursor": cursor}
        if action == "get_ad_group":
            bundle = _group_bundle(client, account_id, _id(values.get("line_item_id"), "ad group"))
            return {"ad_group": s.group(cast(JSONObject, bundle["ad_group"])), "targeting": [s.target(cast(JSONObject, row)) for row in cast(list[JSONValue], bundle["targeting"])], "promoted_posts": [s.promoted(cast(JSONObject, row)) for row in cast(list[JSONValue], bundle["promoted_posts"])]}
        if action == "get_performance":
            return _performance(client, account_id, values)
        raise ValueError("Unsupported X Ads read action.")

    def _propose(self, client: Client, action: str, values: JSONObject, api: HostAPI) -> ActionPendingApproval:
        if action == "launch_campaign":
            plan = _launch_plan(values)
            references, funding = _references(client, plan)
            billing = OBJECTIVES[str(plan["objective"])][2]
            summary = (f"Launch NEW X Ads {plan['objective']} with AUTO bids. {_geography(plan)}. "
                f"Account {plan['account_id']}; {funding['currency']} daily {_units(plan[s.BUDGETS[0]])}, total {_units(plan[s.BUDGETS[1]])}. "
                f"{plan['start_time']} to {plan['end_time']}. Post {plan['post_id']}; expansion {plan.get('audience_expansion', 'none')}. "
                f"Can spend now/at start or after X approves pending review, without another Kern approval; billed by {billing}. View exact request.")
            return _approval(api, client, action, summary, {"plan": plan, "plan_sha256": _digest(plan),
                "references": references, "reference_sha256": _digest(references), "currency": funding["currency"],
                "delivery": _delivery(plan)})
        account_id, campaign_id = _id(values.get("account_id"), "account"), _id(values.get("campaign_id"), "campaign")
        account, access = _account(client, account_id, write=True)
        campaign = _entity(client, f"/accounts/{account_id}/campaigns/{campaign_id}", campaign_id)
        return _approval(api, client, action, f"End X Ads '{clip_text(s.text(campaign, 'name'), 70)}' ({campaign_id}) in {clip_text(s.text(account, 'name'), 60)} ({account_id}): pause parent delivery and retain reporting. No Kern resume or permanent deletion. X Ads Manager can resume; stopping may take time. Past spend remains billable.",
            {"account_id": account_id, "campaign_id": campaign_id, "account": s.account(account), "user_id": access["user_id"],
             "campaign": s.campaign(campaign), "operation": "PAUSE_PARENT_DELIVERY_RETAIN_REPORTING"})

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        if approval.action_id not in ("launch_campaign", "end_campaign"):
            return ActionFailed("Unsupported X Ads approval action.")
        writes: Writes | None = None
        try:
            client = Client(api)
            writes = Writes(client)
            payload = approval.payload
            if payload.get("credential_binding") != client.binding:
                raise ValueError("X Ads credentials changed. Request a new approval with the current credentials.")
            if approval.action_id == "launch_campaign":
                plan = _launch_plan(cast(JSONObject, payload["plan"]))
                if _digest(plan) != payload.get("plan_sha256") or _delivery(plan) != payload.get("delivery"):
                    raise ValueError(MISMATCH)
                references, funding = _references(client, plan)
                if _digest(references) != payload.get("reference_sha256") or funding["currency"] != payload.get("currency"):
                    raise ValueError(MISMATCH)
                _flight(plan, future_end=True)
                created = self._create(writes, plan, funding)
                campaign_id = str(cast(JSONObject, created["campaign"])["id"])
                group_id = str(cast(JSONObject, created["ad_group"])["id"])
                prefix = f"/accounts/{plan['account_id']}"
                writes.attempted = "verify created paused campaign, audience and creative"
                current = _bundle(client, str(plan["account_id"]), campaign_id)
                _verify_created(current, plan, funding["currency"], group_status="PAUSED")
                snapshot = _snapshot(created)
                initial_review = cast(JSONObject, cast(list[JSONValue], created["promoted_posts"])[0]).get("approval_status")
                previous_review = cast(JSONObject, cast(list[JSONValue], current["promoted_posts"])[0])["approval_status"]
                if _launch_snapshot(current, initial_review) != snapshot:
                    raise ValueError(MISMATCH)
                current_references, _ = _references(client, plan)
                if _digest(current_references) != payload.get("reference_sha256"):
                    raise ValueError(MISMATCH)
                _flight(plan, future_end=True)
                writes.entity("PUT", prefix + "/line_items/" + group_id, {"entity_status": "ACTIVE"}, "activate ad group", expected_id=group_id)
                # The parent's paused state is the spending barrier. Confirm the
                # child, all settings and references before activating it last.
                writes.attempted = "verify active child under paused parent"
                current = _bundle(client, str(plan["account_id"]), campaign_id)
                _verify_created(current, plan, funding["currency"], group_status="ACTIVE")
                final_review = cast(JSONObject, cast(list[JSONValue], current["promoted_posts"])[0])["approval_status"]
                if previous_review == "ACCEPTED" and final_review != "ACCEPTED":
                    raise ValueError("X Ads returned a new creative review after acceptance. The parent remains paused.")
                cast(JSONObject, current["ad_group"])["entity_status"] = "PAUSED"
                if _launch_snapshot(current, initial_review) != snapshot:
                    raise ValueError(MISMATCH)
                current_references, _ = _references(client, plan)
                if _digest(current_references) != payload.get("reference_sha256"):
                    raise ValueError(MISMATCH)
                _flight(plan, future_end=True)
                writes.entity("PUT", prefix + "/campaigns/" + campaign_id, {"entity_status": "ACTIVE"}, "activate campaign", expected_id=campaign_id)
                review_message = "Last observed X review PENDING: delivery is blocked until X accepts, then may start within the approved flight without another Kern write or approval." if final_review == "PENDING" else "Last observed X review ACCEPTED: delivery can spend now or at its approved start."
                return ApprovalExecuted(f"Created new X Ads campaign {campaign_id}, ad group {group_id}, and post association {cast(JSONObject, cast(list[JSONValue], created['promoted_posts'])[0])['id']}; configured campaign and ad group ACTIVE. Objective {plan['objective']}, AUTO bidding; {funding['currency']} daily {_units(plan[s.BUDGETS[0]])}, total {_units(plan[s.BUDGETS[1]])}. {review_message} Configured ACTIVE is not proof of delivery; inspect X Ads/performance.")
            account_id, campaign_id = _id(payload.get("account_id"), "account"), _id(payload.get("campaign_id"), "campaign")
            if payload.get("operation") != "PAUSE_PARENT_DELIVERY_RETAIN_REPORTING":
                raise ValueError("The stored X Ads end approval is incomplete. Request a new approval.")
            _, access = _account(client, account_id, write=True)
            if access["user_id"] != payload.get("user_id"):
                raise ValueError(MISMATCH)
            prefix = f"/accounts/{account_id}"
            _entity(client, prefix + "/campaigns/" + campaign_id, campaign_id)
            writes.confirmed.append(f"existing campaign {campaign_id}")
            writes.entity("PUT", prefix + "/campaigns/" + campaign_id, {"entity_status": "PAUSED"}, "end campaign", expected_id=campaign_id)
            return ApprovalExecuted(f"Ended delivery for X Ads campaign {campaign_id} by pausing its parent. Campaign/reporting retained; no Kern resume or permanent deletion. X Ads Manager can resume it. Stopping may take time; past delivery can still be billed.")
        except (ValueError, RuntimeError) as exc:
            return writes.failure(str(exc)) if writes is not None else ActionFailed(str(exc))
        except (KeyError, TypeError):
            return writes.failure("The stored X Ads approval is incomplete. Request a new approval.") if writes is not None else ActionFailed("The stored X Ads approval is incomplete.")

    def _create(self, writes: Writes, plan: JSONObject, funding: JSONObject) -> JSONObject:
        prefix = f"/accounts/{plan['account_id']}"
        campaign = writes.entity("POST", prefix + "/campaigns", _campaign_params(plan), "create campaign")
        campaign_id = str(campaign["id"])
        if campaign.get("currency") != funding["currency"]:
            raise RuntimeError("X Ads did not confirm the campaign's funding currency.")
        group = writes.entity("POST", prefix + "/line_items", _group_params(plan, campaign_id), "create ad group")
        group_id = str(group["id"])
        if group.get("currency") != funding["currency"] or group.get("pay_by") != OBJECTIVES[str(plan["objective"])][1]:
            raise RuntimeError("X Ads did not confirm the approved ad-group currency and billing basis.")
        targets: list[JSONObject] = []
        for target in cast(list[JSONObject], plan["targeting"]):
            targets.append(writes.entity("POST", prefix + "/targeting_criteria", {"line_item_id": group_id,
                "targeting_type": str(target["type"]), "targeting_value": str(target["value"]), "operator_type": "EQ"}, "create targeting criterion"))
        writes.attempted = "create post association"
        rows, cursor = page_data(writes.client.request("POST", prefix + "/promoted_tweets", {"line_item_id": group_id, "tweet_ids": str(plan["post_id"])}), limit=1)
        # Retain any confirmed association id even if another response field
        # fails validation, so the operator can inspect that partial outcome.
        if len(rows) == 1:
            promoted_id = _id(rows[0].get("id"), "promoted post")
            writes.confirmed.append(f"promoted post {promoted_id}")
        if cursor or len(rows) != 1 or rows[0].get("line_item_id") != group_id or rows[0].get("tweet_id") != plan["post_id"]:
            raise RuntimeError("X Ads did not confirm the requested post association.")
        return {"campaign": campaign, "ad_group": group,
            "targeting": cast(list[JSONValue], sorted(targets, key=lambda row: s.text(row, "id"))), "promoted_posts": cast(list[JSONValue], rows)}
