"""Provider response metering for host-owned inference.

Costs are calculated only for models with a fixed, reviewed price. Each write
stores the calculated amount so later catalog changes never rewrite history.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import re
import threading
from typing import Any

from host.runtime.core import host_errors, state


# Prices reviewed 2026-09-22. Store calculated cost on every response so a
# future catalog edit changes only future calls.
# https://developers.openai.com/api/docs/models/gpt-6-luna
# https://typesafe.ai/ (Jev.Cost: $42 per billion input tokens)
_OPENAI_LUNA_RE = re.compile(r"^gpt-6-luna(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?$")
_TYPESAFE_JEV_RE = re.compile(r"^jev-(?:latest|[1-9][0-9]*\.[0-9]+\.[0-9]+)$")
_OPENAI_INPUT_PER_TOKEN = 0.10 / 1_000_000
_OPENAI_CACHED_INPUT_PER_TOKEN = 0.01 / 1_000_000
_OPENAI_OUTPUT_PER_TOKEN = 0.50 / 1_000_000
_TYPESAFE_INPUT_PER_TOKEN = 42.0 / 1_000_000_000
MAX_RESPONSE_TOKENS = 10_000_000
_WRITE_SLOTS = threading.BoundedSemaphore(16)
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="host-inference-usage")
_DROPPED_LOCK = threading.Lock()
_dropped_writes = 0
_last_prune_day: str | None = None


def _counter(value: Any) -> int | None:
    # Both clients bound their request and output far below this ceiling. It
    # rejects a malicious giant integer before cost math or NUMERIC storage.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_RESPONSE_TOKENS:
        return None
    return value


def _save(
    provider: str,
    model: str,
    usage: dict[str, int] | None,
    cost_usd: float | None,
) -> None:
    global _last_prune_day
    try:
        state.record_host_inference_usage(provider, model, usage, cost_usd)
        today = datetime.now(timezone.utc).date()
        if _last_prune_day != today.isoformat():
            with state.mutation() as cur:
                state.prune_host_inference_usage(
                    cur,
                    (today - timedelta(days=state.HOST_INFERENCE_USAGE_RETAIN_DAYS)).isoformat(),
                )
            _last_prune_day = today.isoformat()
    except Exception as exc:
        # Usage reporting must never turn a completed inference result into a
        # failed host feature. Keep the missing counter visible diagnostically.
        host_errors.report_warning(
            "host_inference.usage",
            exc,
            context={"provider": provider, "model": model},
            kind="unexpected_behavior",
        )


def _note_dropped_write() -> None:
    global _dropped_writes
    with _DROPPED_LOCK:
        _dropped_writes += 1


def _report_dropped_writes() -> None:
    global _dropped_writes
    with _DROPPED_LOCK:
        dropped = _dropped_writes
        _dropped_writes = 0
    if dropped:
        host_errors.report_warning(
            "host_inference.usage",
            RuntimeError(f"{dropped} host inference usage record(s) were dropped"),
            context={"dropped_records": dropped},
            kind="unexpected_behavior",
        )


def _schedule(
    provider: str,
    model: str,
    usage: dict[str, int] | None,
    cost_usd: float | None,
) -> None:
    """Queue best-effort metering without delaying a provider response."""
    if not _WRITE_SLOTS.acquire(blocking=False):
        _note_dropped_write()
        return

    def run() -> None:
        try:
            _save(provider, model, usage, cost_usd)
        finally:
            _WRITE_SLOTS.release()
            _report_dropped_writes()

    try:
        _EXECUTOR.submit(run)
    except RuntimeError:
        _WRITE_SLOTS.release()
        _note_dropped_write()


def record_openai_response(_requested_model: str, response: Any | None) -> None:
    # Bill against the model that actually served the response. The request
    # name can be an alias that OpenAI routes to a dated model revision.
    response_model = response.get("model") if isinstance(response, dict) else None
    match = _OPENAI_LUNA_RE.fullmatch(response_model) if isinstance(response_model, str) else None
    if match is None:
        host_errors.report_warning(
            "host_inference.usage",
            ValueError("OpenAI response model is missing or unsupported; usage was not recorded"),
            context={"provider": "openai"},
            kind="unexpected_behavior",
        )
        return
    raw = response.get("usage") if isinstance(response, dict) else None
    measured: dict[str, int] | None = None
    if isinstance(raw, dict):
        input_tokens = _counter(raw.get("prompt_tokens", raw.get("input_tokens")))
        output_tokens = _counter(raw.get("completion_tokens", raw.get("output_tokens")))
        details_present = "prompt_tokens_details" in raw or "input_tokens_details" in raw
        if "prompt_tokens_details" in raw:
            details = raw["prompt_tokens_details"]
        elif "input_tokens_details" in raw:
            details = raw["input_tokens_details"]
        else:
            details = {}
        # An omitted details object means no cache data was reported. A
        # present-but-malformed object is different: pricing it as zero cached
        # tokens could silently overstate cost, so leave the response visibly
        # unmeasured instead.
        cached_tokens: int | None
        if not details_present:
            cached_tokens = 0
        elif isinstance(details, dict) and "cached_tokens" in details:
            cached_tokens = _counter(details["cached_tokens"])
        else:
            cached_tokens = None
        if (
            input_tokens is not None
            and output_tokens is not None
            and cached_tokens is not None
            and cached_tokens <= input_tokens
        ):
            measured = {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "output_tokens": output_tokens,
            }
    cost: float | None = None
    if measured is not None:
        uncached = measured["input_tokens"] - measured["cached_input_tokens"]
        cost = (
            uncached * _OPENAI_INPUT_PER_TOKEN
            + measured["cached_input_tokens"] * _OPENAI_CACHED_INPUT_PER_TOKEN
            + measured["output_tokens"] * _OPENAI_OUTPUT_PER_TOKEN
        )
    _schedule("openai", "gpt-6-luna", measured, cost)


def record_typesafe_response(_requested_model: str, response: Any | None) -> None:
    response_model = response.get("model") if isinstance(response, dict) else None
    if not isinstance(response_model, str) or not _TYPESAFE_JEV_RE.fullmatch(response_model):
        host_errors.report_warning(
            "host_inference.usage",
            ValueError("TypeSafe response model is missing or unsupported; usage was not recorded"),
            context={"provider": "typesafe"},
            kind="unexpected_behavior",
        )
        return
    raw = response.get("usage") if isinstance(response, dict) else None
    measured: dict[str, int] | None = None
    if isinstance(raw, dict):
        input_tokens = _counter(raw.get("input_tokens"))
        output_tokens = _counter(raw.get("output_tokens"))
        if input_tokens is not None and output_tokens is not None:
            measured = {
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "output_tokens": output_tokens,
            }
    # TypeSafe currently publishes only an input-token price. Output tokens are
    # retained for usage visibility but do not contribute to the estimate.
    cost = (
        measured["input_tokens"] * _TYPESAFE_INPUT_PER_TOKEN
        if measured is not None
        else None
    )
    _schedule("typesafe", "jev", measured, cost)
