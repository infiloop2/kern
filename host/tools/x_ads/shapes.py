"""Closed, provider-independent read views used in results and approvals."""

from __future__ import annotations

import re

from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared import outputs as out


def text(row: JSONObject, key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def money(row: JSONObject, key: str) -> str | None:
    value = row.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        return value
    return None


def strings(row: JSONObject, key: str) -> list[JSONValue]:
    value = row.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def view(row: JSONObject, *, texts: tuple[str, ...], amounts: tuple[str, ...] = (), flags: tuple[str, ...] = ()) -> JSONObject:
    result: JSONObject = {key: text(row, key) for key in texts}
    result.update({key: money(row, key) for key in amounts})
    result.update({key: row.get(key) if isinstance(row.get(key), bool) else None for key in flags})
    return result


def shape(*, texts: tuple[str, ...], amounts: tuple[str, ...] = (), flags: tuple[str, ...] = (), extra: JSONObject | None = None) -> JSONObject:
    props: JSONObject = {key: out.text(key.replace("_", " ") + "; empty if X omits it.") for key in texts}
    props.update({key: out.nullable({"type": "string"}, "Integer micros in the funding currency; null if unavailable.") for key in amounts})
    props.update({key: out.nullable({"type": "boolean"}, key.replace("_", " ") + "; null if unavailable.") for key in flags})
    props.update(extra or {})
    return out.obj(props, list(props))


ACCOUNT_TEXTS = ("id", "name", "timezone", "approval_status")
ACCOUNT_SCHEMA = shape(texts=ACCOUNT_TEXTS, flags=("deleted",))
FUNDING_TEXTS = ("id", "type", "currency", "entity_status", "start_time", "end_time")
FUNDING_AMOUNTS = ("credit_limit_local_micro", "credit_remaining_local_micro", "funded_amount_local_micro")
FUNDING_FLAGS = ("able_to_fund", "deleted", "cancelled")
FUNDING_SCHEMA = shape(texts=FUNDING_TEXTS, amounts=FUNDING_AMOUNTS, flags=FUNDING_FLAGS)
CAMPAIGN_TEXTS = ("id", "name", "currency", "funding_instrument_id", "entity_status", "budget_optimization", "updated_at")
BUDGETS = ("daily_budget_amount_local_micro", "total_budget_amount_local_micro")
SERVING_SCHEMA: JSONObject = {
    "effective_status": out.nullable({"type": "string"}, "X's effective status code, at most 64 characters; null if omitted. Eligibility is not proof of delivery."),
    "servable": out.nullable({"type": "boolean"}, "X's serving eligibility, null if unavailable. True does not prove impressions or spend."),
    "reasons_not_servable": out.nullable({"type": "array", "items": out.text("X reason code, at most 64 characters."), "maxItems": 32}, "Up to 32 X reason codes. Null means unavailable; [] means X returned no reasons, not proof of delivery."),
    "reasons_not_servable_truncated": {"type": "boolean", "description": "True when X returned more than 32 reasons."},
}
CAMPAIGN_SCHEMA = shape(texts=CAMPAIGN_TEXTS, amounts=BUDGETS, flags=("deleted", "standard_delivery"), extra=SERVING_SCHEMA)
GROUP_TEXTS = ("id", "campaign_id", "name", "currency", "entity_status", "objective", "product_type", "bid_strategy", "goal", "pay_by", "audience_expansion", "start_time", "end_time", "updated_at")
GROUP_AMOUNTS = (*BUDGETS, "bid_amount_local_micro")
GROUP_SCHEMA = shape(texts=GROUP_TEXTS, amounts=GROUP_AMOUNTS, flags=("deleted", "standard_delivery"), extra={
    **SERVING_SCHEMA,
    "placements": out.array_of(out.text("Placement enum."), "Placement list from X."),
})
PROMOTABLE_TEXTS = ("id", "user_id", "promotable_user_type")
PROMOTABLE_SCHEMA = shape(texts=PROMOTABLE_TEXTS, flags=("deleted",))
TARGET_TEXTS = ("id", "line_item_id", "name", "targeting_type", "targeting_value", "operator_type", "updated_at")
TARGET_SCHEMA = shape(texts=TARGET_TEXTS, flags=("deleted",))
PROMOTED_TEXTS = ("id", "line_item_id", "tweet_id", "entity_status", "approval_status", "updated_at")
PROMOTED_SCHEMA = shape(texts=PROMOTED_TEXTS, flags=("deleted",))
POST_SCHEMA = out.obj({
    "id": out.text("Existing published post id."),
    "author_id": out.text("Numeric author id."),
    "author_username": out.text("Handle when X provides it; empty otherwise."),
    "text": out.text("Full existing post text, up to 25000 UTF-8 bytes."),
    "created_at": out.text("Provider publication time."),
    "url": out.text("Public X post URL."),
    "content_sha256": out.text("Digest of returned text, author, attached media, entities, card references and destinations. Creation/launch reject card posts rather than relying on a mutable card URI."),
}, ["id", "author_id", "author_username", "text", "created_at", "url", "content_sha256"])


def account(row: JSONObject) -> JSONObject:
    return view(row, texts=ACCOUNT_TEXTS, flags=("deleted",))


def funding(row: JSONObject) -> JSONObject:
    return view(row, texts=FUNDING_TEXTS, amounts=FUNDING_AMOUNTS, flags=FUNDING_FLAGS)


def campaign(row: JSONObject) -> JSONObject:
    return {**view(row, texts=CAMPAIGN_TEXTS, amounts=BUDGETS, flags=("deleted", "standard_delivery")), **serving(row)}


def group(row: JSONObject) -> JSONObject:
    result = view(row, texts=GROUP_TEXTS, amounts=GROUP_AMOUNTS, flags=("deleted", "standard_delivery"))
    result["placements"] = strings(row, "placements")
    result.update(serving(row))
    return result


def serving(row: JSONObject) -> JSONObject:
    """Expose bounded provider eligibility without inferring actual delivery."""
    status, eligible, reasons = (row.get(key) for key in ("effective_status", "servable", "reasons_not_servable"))
    def code(value: JSONValue) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value, re.ASCII) is not None
    if status is not None and not code(status):
        raise RuntimeError("X Ads returned malformed effective_status.")
    if eligible is not None and not isinstance(eligible, bool):
        raise RuntimeError("X Ads returned malformed servable.")
    if reasons is not None and (not isinstance(reasons, list) or any(not code(item) for item in reasons)):
        raise RuntimeError("X Ads returned malformed reasons_not_servable.")
    return {"effective_status": status, "servable": eligible,
            "reasons_not_servable": reasons[:32] if isinstance(reasons, list) else None,
            "reasons_not_servable_truncated": isinstance(reasons, list) and len(reasons) > 32}


def target(row: JSONObject) -> JSONObject:
    return view(row, texts=TARGET_TEXTS, flags=("deleted",))


def promoted(row: JSONObject) -> JSONObject:
    return view(row, texts=PROMOTED_TEXTS, flags=("deleted",))
