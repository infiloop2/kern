"""Stored configuration for host-owned inference providers."""

from __future__ import annotations

from typing import Any
import math
import re
import time

from host.runtime.core import db, secretbox
from host.runtime.core.state._base import mutation, utc_now


PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _provider(provider: str) -> str:
    if not isinstance(provider, str) or PROVIDER_RE.fullmatch(provider) is None:
        raise ValueError("unknown host inference provider")
    return provider


def host_inference_provider_metadata(provider: str) -> dict[str, Any]:
    provider = _provider(provider)
    with db.transaction() as cur:
        cur.execute(
            "SELECT provider, enabled, features, api_key_encrypted, updated_at "
            "FROM host_inference_providers WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("host inference provider row is missing")
    return {
        "provider": str(row[0]),
        "enabled": bool(row[1]),
        "features": dict(row[2]) if isinstance(row[2], dict) else {},
        "configured": row[3] is not None,
        "updated_at": str(row[4]),
    }


def host_inference_providers() -> list[dict[str, Any]]:
    with db.transaction() as cur:
        cur.execute("SELECT provider FROM host_inference_providers ORDER BY provider")
        providers = [str(row[0]) for row in cur.fetchall()]
    return [
        {**host_inference_provider_metadata(provider), "usage": host_inference_usage(provider)}
        for provider in providers
    ]


def configure_host_inference_provider(
    provider: str,
    *,
    enabled: bool | None = None,
    api_key: str | None = None,
    features: dict[str, bool] | None = None,
) -> dict[str, Any]:
    provider = _provider(provider)
    encrypted = secretbox.encrypt(api_key) if api_key is not None else None
    with mutation() as cur:
        cur.execute(
            "SELECT enabled, features FROM host_inference_providers "
            "WHERE provider = %s FOR UPDATE",
            (provider,),
        )
        stored = cur.fetchone()
        if stored is None:
            raise RuntimeError("host inference provider row is missing")
        next_enabled = bool(stored[0]) if enabled is None else enabled
        next_features = dict(stored[1]) if features is None and isinstance(stored[1], dict) else (features or {})
        if encrypted is None:
            cur.execute(
                "UPDATE host_inference_providers SET enabled = %s, features = %s, "
                "updated_at = %s WHERE provider = %s",
                (next_enabled, db.jsonb(next_features), utc_now(), provider),
            )
        else:
            cur.execute(
                "UPDATE host_inference_providers SET enabled = %s, features = %s, "
                "api_key_encrypted = %s, updated_at = %s WHERE provider = %s",
                (next_enabled, db.jsonb(next_features), encrypted, utc_now(), provider),
            )
    return host_inference_provider_metadata(provider)


def clear_host_inference_provider(provider: str) -> dict[str, Any]:
    provider = _provider(provider)
    with mutation() as cur:
        cur.execute(
            "UPDATE host_inference_providers SET enabled = FALSE, "
            "api_key_encrypted = NULL, updated_at = %s WHERE provider = %s",
            (utc_now(), provider),
        )
    return host_inference_provider_metadata(provider)


def enabled_host_inference_provider(provider: str) -> dict[str, Any] | None:
    provider = _provider(provider)
    with db.transaction() as cur:
        cur.execute(
            "SELECT api_key_encrypted, features FROM host_inference_providers "
            "WHERE provider = %s AND enabled = TRUE",
            (provider,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return {
        "provider": provider,
        "api_key": secretbox.decrypt(str(row[0])),
        "features": dict(row[1]) if isinstance(row[1], dict) else {},
    }


def host_inference_provider_is_enabled(provider: str) -> bool:
    """Read only connection enablement; callers receive no credential data."""
    provider = _provider(provider)
    with db.transaction() as cur:
        cur.execute(
            "SELECT enabled FROM host_inference_providers WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row is not None else False


_USAGE_FIELDS = (
    "measured_requests",
    "priced_requests",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
)


def record_host_inference_usage(
    provider: str,
    model: str,
    usage: dict[str, int] | None,
    cost_usd: float | None,
) -> None:
    """Record one returned provider response without guessing missing cost.

    ``usage`` is None when the provider omitted or malformed its token record.
    ``cost_usd`` is None when the response model is outside Kern's fixed price
    catalog. Those gaps remain visible through the measured/priced request
    counts instead of being reported as zero spend.
    """
    provider = _provider(provider)
    if provider not in {"openai", "typesafe"}:
        raise ValueError("unknown host inference usage provider")
    if model not in {"gpt-6-luna", "jev"}:
        raise ValueError("unknown host inference usage model")
    counters = {field: 0 for field in _USAGE_FIELDS}
    if usage is not None:
        expected = {"input_tokens", "cached_input_tokens", "output_tokens"}
        if set(usage) != expected:
            raise ValueError("host inference usage has unsupported counters")
        counters["measured_requests"] = 1
        for field in expected:
            value = usage[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("host inference usage counters must be non-negative integers")
            counters[field] = value
        if counters["cached_input_tokens"] > counters["input_tokens"]:
            raise ValueError("cached input tokens cannot exceed input tokens")
    if cost_usd is not None:
        if (
            usage is None
            or isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(cost_usd)
            or cost_usd < 0
        ):
            raise ValueError("host inference usage cost is invalid")
        counters["priced_requests"] = 1
    stored_cost = float(cost_usd) if cost_usd is not None else 0.0
    columns = (*_USAGE_FIELDS, "cost_usd")
    assignments = ", ".join(
        f"{column} = host_inference_usage.{column} + EXCLUDED.{column}"
        for column in ("requests", *columns)
    )
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO host_inference_usage (provider, model, day, requests, "
            + ", ".join(columns)
            + ") VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (provider, model, day) DO UPDATE SET " + assignments,
            (
                provider,
                model,
                time.strftime("%Y-%m-%d", time.gmtime()),
                *(counters[field] for field in _USAGE_FIELDS),
                stored_cost,
            ),
        )


def host_inference_usage(provider: str, since_day: str | None = None) -> dict[str, Any]:
    provider = _provider(provider)
    since = since_day or time.strftime("%Y-%m-01", time.gmtime())
    with db.transaction() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(measured_requests), 0), "
            "COALESCE(SUM(priced_requests), 0), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(cached_input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cost_usd), 0) FROM host_inference_usage "
            "WHERE provider = %s AND day >= %s",
            (provider, since),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("host inference usage aggregate is missing")
    return {
        "month_to_date": round(float(row[6]), 9),
        "currency": "USD",
        "requests": int(row[0]),
        "measured_requests": int(row[1]),
        "priced_requests": int(row[2]),
        "input_tokens": int(row[3]),
        "cached_input_tokens": int(row[4]),
        "output_tokens": int(row[5]),
    }


def prune_host_inference_usage(cur: Any, cutoff_day: str) -> None:
    cur.execute("DELETE FROM host_inference_usage WHERE day < %s", (cutoff_day,))
