"""Bounded PPE proposals. Provider history and revenue share are not agent inputs."""

from datetime import datetime, timedelta, timezone
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
    values["effective_at"] = timestamp(values["effective_at"]).isoformat()
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


def history(actor):
    entries = actor.get("pricingInfos")
    if not isinstance(entries, list) or len(entries) > 100 or any(not isinstance(e, dict) for e in entries):
        raise ValueError("Complete bounded pricing history is unavailable; inspect monetization in Apify Console.")
    if len(json.dumps(entries, allow_nan=False).encode()) > 24000:
        raise ValueError("Pricing history exceeds the approval size limit; use Apify Console.")
    return entries


def check_time(actor, values, entries):
    current = now()
    effective = timestamp(values["effective_at"])
    if not current < effective <= current + timedelta(days=90):
        raise ValueError("Effective time must still be in the future and within 90 days; queue a new approval if it elapsed.")
    if actor.get("isPublic") is not False and effective < current + timedelta(days=14):
        raise ValueError("Public Actor pricing requires at least 14 days notice in this tool.")
    if any(timestamp(e.get("startedAt")) > current for e in entries):
        raise ValueError("Actor already has scheduled pricing; manage that change in Apify Console first.")


def proposal(actor, values) -> dict[str, Any]:
    entries = history(actor)
    check_time(actor, values, entries)
    # The public schema marks margin as platform-set. Never invent a margin
    # for an Actor without a provider-supplied pricing record.
    if len(entries) >= 100:
        raise ValueError("Pricing history is at the tool limit; use Apify Console.")
    if not entries:
        raise ValueError("No platform-set revenue share is available. Initialize monetization in Apify Console first.")
    latest = max(entries, key=lambda e: timestamp(e.get("startedAt")))
    margin = amount(latest.get("apifyMarginPercentage"), 1)
    events = {}
    for event in values["events"]:
        result = {"eventTitle": event["title"], "eventDescription": event["description"],
                  "isPrimaryEvent": event["primary"], "isOneTimeEvent": event["one_time"]}
        if "price_usd" in event:
            result["eventPriceUsd"] = event["price_usd"]
        else:
            result["eventTieredPricingUsd"] = {t: {"tieredEventPriceUsd": p} for t, p in event["tier_prices_usd"].items()}
        events[event["name"]] = result
    entry = {"pricingModel": "PAY_PER_EVENT", "apifyMarginPercentage": margin,
             "createdAt": now().isoformat(), "startedAt": values["effective_at"],
             "pricingPerEvent": {"actorChargeEvents": events},
             "minimalMaxTotalChargeUsd": values["minimum_run_budget_usd"]}
    return {"pricingInfos": [*entries, entry]}


def verified(actor, body):
    """Require independent readback, tolerating provider-added metadata only."""
    actual = history(actor)
    expected = body["pricingInfos"]
    def contains(value, subset):
        if isinstance(subset, dict):
            return isinstance(value, dict) and all(k in value and contains(value[k], v) for k, v in subset.items())
        return value == subset
    if len(actual) != len(expected):
        return False
    # Do not accept an extra billable event or silently changed unit price.
    desired = expected[-1]
    matches = [e for e in actual if e.get("pricingModel") == "PAY_PER_EVENT"
               and timestamp(e.get("startedAt")) == timestamp(desired["startedAt"])]
    if len(matches) != 1:
        return False
    saved = matches[0]
    if saved.get("pricingPerEvent") != desired["pricingPerEvent"] or saved.get("minimalMaxTotalChargeUsd") != desired["minimalMaxTotalChargeUsd"]:
        return False
    return saved.get("apifyMarginPercentage") == desired["apifyMarginPercentage"] and all(any(contains(e, old) for e in actual) for old in expected[:-1])
