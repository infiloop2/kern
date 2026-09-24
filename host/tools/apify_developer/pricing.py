"""Bounded PPE proposals containing only the new approved pricing period."""

from datetime import datetime, timezone
import json
import math
import re
from typing import Any

TIERS = ("FREE", "BRONZE", "SILVER", "GOLD", "PLATINUM", "DIAMOND")


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Pricing timestamps must be ISO 8601 with a timezone.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Pricing timestamps must be ISO 8601 with a timezone.") from None
    if result.tzinfo is None:
        raise ValueError("Pricing timestamps must include a timezone.")
    return result.astimezone(timezone.utc)


def amount(value: Any, maximum: float) -> float:
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"Pricing amounts must be finite numbers between zero and {maximum} USD.")
    return float(value)


def validate(values, guard):
    """Normalize only the documented, operator-editable subset."""
    values = dict(values)
    values["minimum_run_budget_usd"] = amount(values["minimum_run_budget_usd"], 100)
    events = values["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 20:
        raise ValueError("Provide 1–20 charge events.")
    normalized = []
    seen = set()
    for event in events:
        required = {"name", "title", "description", "primary", "one_time"}
        if not isinstance(event, dict) or not required <= event.keys() or set(event) - required - {"price_usd", "tier_prices_usd"}:
            raise ValueError("Each event requires name, title, description, primary, one_time and exactly one price format.")
        name = event["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) or name in seen:
            raise ValueError("Event names must be unique lowercase identifiers up to 64 characters.")
        if name.startswith("apify-") and name not in ("apify-actor-start", "apify-default-dataset-item"):
            raise ValueError("Unrecognized reserved Apify event name.")
        seen.add(name)
        for key, limit in (("title", 100), ("description", 500)):
            value = event[key]
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit or "\x00" in value:
                raise ValueError("Event titles/descriptions must be nonempty bounded UTF-8 text.")
            guard(value)
        if type(event["primary"]) is not bool or type(event["one_time"]) is not bool:
            raise ValueError("Event primary and one_time must be booleans.")
        if name == "apify-actor-start" and (not event["one_time"] or event["primary"]):
            raise ValueError("The Apify start event must be one-time and cannot be the primary result event.")
        if ("price_usd" in event) == ("tier_prices_usd" in event):
            raise ValueError("Choose exactly one of price_usd or tier_prices_usd.")
        result = dict(event)
        if "price_usd" in event:
            result["price_usd"] = amount(event["price_usd"], 100)
        else:
            tiers = event["tier_prices_usd"]
            if not isinstance(tiers, dict) or set(tiers) != set(TIERS):
                raise ValueError("Tier prices must specify all six subscription tiers.")
            result["tier_prices_usd"] = {tier: amount(tiers[tier], 100) for tier in TIERS}
        normalized.append(result)
    if sum(e["primary"] for e in normalized) != 1:
        raise ValueError("Select exactly one primary charge event.")
    values["events"] = normalized
    return values


def current_and_scheduled(actor):
    """Read the current and next period without returning an expanding history."""
    current = now()
    active = None
    scheduled = None
    for entry in actor.get("pricingInfos") or []:
        started = timestamp(entry.get("startedAt"))
        if started <= current:
            if active is None or started > timestamp(active["startedAt"]):
                active = entry
        elif scheduled is None or started < timestamp(scheduled["startedAt"]):
            scheduled = entry
    entries = [e for e in (active, scheduled) if e is not None]
    if len(json.dumps(entries, allow_nan=False).encode()) > 24000:
        raise ValueError("Current pricing exceeds the output size limit; inspect it in Apify Console.")
    return entries


def proposal(values) -> dict[str, Any]:
    events = {}
    for event in values["events"]:
        result = {"eventTitle": event["title"], "eventDescription": event["description"],
                  "isPrimaryEvent": event["primary"], "isOneTimeEvent": event["one_time"]}
        if "price_usd" in event:
            result["eventPriceUsd"] = event["price_usd"]
        else:
            result["eventTieredPricingUsd"] = {t: {"tieredEventPriceUsd": p} for t, p in event["tier_prices_usd"].items()}
        events[event["name"]] = result
    # Required by the update schema; Apify documents the standard PPE split
    # as 80% developer / 20% platform. No prior pricing record is needed.
    entry = {"pricingModel": "PAY_PER_EVENT", "apifyMarginPercentage": 0.2,
             "pricingPerEvent": {"actorChargeEvents": events},
             "minimalMaxTotalChargeUsd": values["minimum_run_budget_usd"]}
    return {"pricingInfos": [entry]}


def verified(actor, body):
    """Require independent readback, tolerating provider-added metadata only."""
    desired = body["pricingInfos"][0]
    actual = current_and_scheduled(actor)
    if len(actual) != 1:
        return False
    # Do not accept an extra billable event or silently changed unit price.
    saved = actual[0]
    if saved.get("pricingModel") != "PAY_PER_EVENT" or timestamp(saved.get("startedAt")) != timestamp(desired["startedAt"]):
        return False
    if saved.get("pricingPerEvent") != desired["pricingPerEvent"] or saved.get("minimalMaxTotalChargeUsd") != desired["minimalMaxTotalChargeUsd"]:
        return False
    return saved.get("apifyMarginPercentage") == desired["apifyMarginPercentage"]
