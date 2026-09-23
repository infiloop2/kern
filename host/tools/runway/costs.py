"""Runway prices reviewed 2026-09-22: https://docs.dev.runwayml.com/guides/pricing/.

No host-side pricing. Only settings with a calculable price produce a cost report.
"""
from __future__ import annotations

from decimal import Decimal

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject
from host.tools.runway import options


def quote_usd(body: JSONObject) -> str | None:
    model = body.get("model")
    if model in {"gpt_image_2_5_sunburst", "gpt_image_2_5_flare"}:
        credits = {"low": 1, "medium": 5, "high": 16, "xhigh": 28, "max": 63}.get(str(body.get("quality")))
        return str(Decimal(credits) / 100) if credits is not None else None
    if model in {"eleven_multilingual_v2", "eleven_v3"}:
        text = body.get("promptText")
        if not isinstance(text, str):
            return None
        speech_credits = Decimal(len(text)) / 50
        if model == "eleven_v3":
            speech_credits = max(Decimal(1), speech_credits)
        return str(speech_credits / 100)
    duration = body.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        return None
    # Input/reference video adds duration-dependent charges; the task API does
    # not return that duration. This version cannot price those jobs exactly.
    if body.get("promptVideo") or body.get("referenceVideos") or model == "aleph2":
        return None
    ratio = str(body.get("ratio", "1280:720"))
    dims = [int(value) for value in ratio.split(":") if value.isdecimal()]
    # Native Seedance audio changes the rate. Until its rate is known, these
    # calls cannot be reported with a reliable dollar amount.
    if model in options.SEEDANCE_MODELS and "audio" in body:
        return None
    if model in options.SEEDANCE_MODELS and ratio not in options.MODEL_RATIOS[str(model)]:
        return None
    tier_1080 = ratio in options.SEEDANCE_1080
    tier_4k = ratio in options.SEEDANCE_4K
    tier_720 = ratio in options.SEEDANCE_720
    rates = {"gen4.5": 12, "gen4_turbo": 5,
             "veo3.1": 40 if body.get("audio", True) else 20,
             "veo3.1_fast": 15 if body.get("audio", True) else 10,
             "seedance2": 150 if tier_4k else 40 if tier_1080 else 36,
             "seedance2_fast": 29,
             "seedance2_5": 68 if tier_1080 else 30 if tier_720 else 20,
             "h3_max": 5 if body.get("resolution") == "480p" else 8}
    rate = rates.get(str(model))
    if rate is None:
        return None
    fmt = body.get("outputFormat")
    if fmt in {"prores", "png_sequence"}:
        rate += 5
    elif fmt and fmt != "mp4":
        rate += 40 if len(dims) == 2 and dims[0] * dims[1] > 4_000_000 else 20
    credits = rate * duration
    if model == "seedance2_5":
        credits = max(80, credits)
    return str(Decimal(credits) / 100)


def submitted(api: HostAPI, task_id: str | None, body: JSONObject) -> None:
    amount = quote_usd(body)
    if amount is not None:
        if task_id is None:
            api.costs.record(amount)
        else:
            api.costs.record(amount, charge_id=f"task:{task_id}")
