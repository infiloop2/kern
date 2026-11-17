"""Explicit turn recovery; error text selects a check, usage determines limits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import time
from typing import Any

from host.runtime.agent_runtime import codex_app_server, grok_agent
from host.runtime.core import state
from host.session_options import session_config_error

DELAYS = (300, 1200, 3600, 14400, 43200)
RESET_BUFFER = 60


def classify(runtime: str, error: str) -> str | None:
    if runtime == "script":
        return None
    text = error.casefold()
    if "limit" in text:
        return "limit" if runtime in (*codex_app_server.CODEX_RUNTIME_TYPES,
                                       *grok_agent.GROK_RUNTIME_TYPES, "claude_code") else None
    if any(word in text for word in ("timed", "timeout", "capacity")):
        return "transient"
    return None


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


@dataclass(frozen=True)
class Usage:
    windows: tuple[tuple[float, float | None], ...]

    @property
    def exhausted(self) -> bool:
        return any(percent >= 100 for percent, _ in self.windows)

    def reset(self, now: float) -> float | None:
        resets = [reset for percent, reset in self.windows if percent >= 100]
        if not resets or any(reset is None or reset <= now for reset in resets):
            return None
        return max(reset for reset in resets if reset is not None) + RESET_BUFFER


def usage_snapshot(runtime: str, model: str, account: dict[str, Any], since: float) -> Usage | None:
    key = "claude_usage" if runtime == "claude_code" else (
        "codex_usage" if runtime in codex_app_server.CODEX_RUNTIME_TYPES else "grok_usage")
    usage = account.get(key)
    if not isinstance(usage, dict):
        return None
    try:
        checked = datetime.fromisoformat(usage["last_checked_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if checked < int(since):
        return None
    if runtime in codex_app_server.CODEX_RUNTIME_TYPES:
        limits = usage.get("rate_limits")
        if not isinstance(limits, dict):
            return None
        raw = []
        # Both subscription windows participate. Purchased credits do not
        # authorize spending through an exhausted subscription.
        for name in ("primary", "secondary"):
            value = limits.get(name)
            if not isinstance(value, dict):
                return None
            raw.append((value.get("used_percent"), value.get("resets_at")))
    elif runtime == "claude_code":
        prefixes = ["current_session", "weekly"]
        if "fable" in model.casefold():
            prefixes.append("fable_weekly")
        raw = [(usage.get(prefix + "_used_percent"), usage.get(prefix + "_resets_at")) for prefix in prefixes]
    else:
        # The Grok billing adapter exposes one subscription period. Missing
        # percentages remain unknown, including unified-billing accounts.
        raw = [(usage.get("usage_percent"), usage.get("resets_at"))]
    windows = []
    for percent, reset in raw:
        parsed = number(percent)
        if parsed is None:
            return None
        windows.append((parsed, number(reset)))
    return Usage(tuple(windows))


def fresh_usage(runtime: str, model: str) -> Usage | None:
    # Orchestrator owns provider refresh and imports retries.
    from host.runtime.agent_runtime import orchestrator
    started = time.time()
    if not orchestrator.runtime_network_enabled(runtime):
        return None
    if orchestrator.refresh_runtime_status(runtime, force_provider_probe=True) != "active":
        return None
    if runtime == "claude_code":
        account = state.read_claude_account()
    elif runtime in codex_app_server.CODEX_RUNTIME_TYPES:
        account = state.read_openai_account(runtime_type=runtime)
    else:
        account = state.read_xai_account(runtime_type=runtime)
    return usage_snapshot(runtime, model, account or {}, started)


def limit_recovery(runtime: str, model: str, effort: str) -> tuple[str, float] | None:
    current = fresh_usage(runtime, model)
    if current is None or not current.exhausted:
        return None
    candidates = (codex_app_server.CODEX_RUNTIME_TYPES if runtime in codex_app_server.CODEX_RUNTIME_TYPES
                  else grok_agent.GROK_RUNTIME_TYPES if runtime in grok_agent.GROK_RUNTIME_TYPES else ())
    for candidate in candidates:
        if candidate == runtime or session_config_error(candidate, model, effort) is not None:
            continue
        if not state.runtime_account_enabled(candidate):
            continue
        alternative = fresh_usage(candidate, model)
        if alternative is not None and not alternative.exhausted:
            return candidate, time.time()
    # Keep this subscription even if an exhausted alternative resets sooner.
    reset = current.reset(time.time())
    return (runtime, reset) if reset is not None else None
