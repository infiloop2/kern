"""Magnific payloads from runwayml/sdk-python *_upscale_create_params.py."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject
from host.tools.manifest import ActionSpec, guarded_input, validated_input
from host.tools.runway import options
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError

MODELS = {"image": "magnific_precision_upscaler_v2", "video": "magnific_video_upscaler_creative"}
# Reviewed 2026-09-27: https://docs.dev.runwayml.com/guides/pricing/
# USD 0.01 per credit. Missing metadata uses explicit conservative assumptions,
# not a claim that these are provider-enforced upper bounds.
VIDEO_FRAME_USD = {"720p": "0.007", "1k": "0.007", "2k": "0.009", "4k": "0.012"}
ESTIMATE_FIELDS = {"image": ("source_width", "source_height"), "video": ("estimated_output_frames",)}


def properties(kind: str) -> JSONObject:
    common: JSONObject = {
        f"{kind}_asset_id": {"type": "string", "description": f"Reference from stage_{kind}(for_tool=runway); uploaded after all settings validate."},
        "sharpen": {"type": "integer", "description": "Sharpness, 0-100. Omit for provider default."},
        "smart_grain": {"type": "integer", "description": "Grain and texture, 0-100. Omit for provider default."},
    }
    if kind == "image":
        return {**common,
            "source_width": {"type": "integer", "description": "Optional measured source width (1-100000px), used only for cost estimation. Supply with source_height; otherwise use the $1.50 high tier."},
            "source_height": {"type": "integer", "description": "Optional measured source height (1-100000px), used only for cost estimation."},
            "scale_factor": {"type": "integer", "enum": [2, 4, 8, 16], "description": "Multiply each source dimension; default 2."},
            "flavor": options.choice_schema(("sublime", "photo", "photo_denoiser"), "Illustration, photography, or noisy photos; omit for provider default."),
            "ultra_detail": {"type": "integer", "description": "Fine detail enhancement, 0-100. Omit for provider default."},
        }
    return {**common,
        "estimated_output_frames": {"type": "integer", "description": "Expected total OUTPUT frames (1-1000000), including FPS boost. Used only for pricing. If unknown, assume 30s at 120 FPS, doubled with FPS boost; this is conservative, not a guaranteed upper bound."},
        "resolution": options.choice_schema(("720p", "1k", "2k", "4k"), "Output resolution; default 2k. Input clips must be at most 30 seconds (provider validates)."),
        "flavor": options.choice_schema(("vivid", "natural"), "Enhanced color/detail or faithful reproduction; omit for provider default."),
        "creativity": {"type": "integer", "description": "Invented detail, 0-100. Omit for provider default."},
        "fps_boost": {"type": "boolean", "description": "Increase output FPS; default false to preserve motion and avoid extra frame charges."},
    }


def actions(output_schema: JSONObject) -> tuple[ActionSpec, ...]:
    return tuple(ActionSpec(
        id=f"upscale_{kind}",
        description=f"Start paid Magnific {kind} upscaling through Runway. Poll get_task with output_kind={kind}, then save_{kind}. Publishes nothing.",
        data_policy="Sends the staged source media bytes and enhancement settings to Runway and Magnific for processing immediately. Uses the existing Runway account and credits; no social publishing.",
        cost_description=("Published price: 25 credits per image, or 150 when output exceeds 4096px. " if kind == "image" else
            "Published price per output frame: $0.007 at 720p/1k, $0.009 at 2k, $0.012 at 4k, rounded up to whole Runway credits. FPS boost can increase billed frames. ") +
            "Records a published-rate estimate when the task is accepted. Missing image dimensions use the $1.50 high tier; missing video frames assume 30s at 120 FPS, doubled with FPS boost. Assumptions are returned with the estimate. Actual bills/refunds are not reconciled.",
        input_schema=outputs.obj(properties(kind), [f"{kind}_asset_id"]), output_schema=output_schema,
        input_protections={key: validated_input("Listed choice, field-specific strict integer bounds, boolean, or host-scoped staged media reference.") for key in properties(kind)},
    ) for kind in ("image", "video"))


def request(kind: str, values: JSONObject, api: HostAPI, uploads: dict[str, str]) -> JSONObject:
    fields = properties(kind)
    if set(values) - set(fields):
        raise ToolInputValidationError("Runway upscaling received an unsupported field.")
    asset_key = f"{kind}_asset_id"
    if asset_key not in values:
        raise ToolInputValidationError(f"Provide {asset_key} from workspace staging.")
    body: JSONObject = {"model": MODELS[kind]}
    if kind == "image":
        body["scaleFactor"] = 2
    else:
        body.update({"resolution": "2k", "fpsBoost": False})
    mapping = {"scale_factor": "scaleFactor", "smart_grain": "smartGrain", "ultra_detail": "ultraDetail", "fps_boost": "fpsBoost"}
    for name, value in values.items():
        if name == asset_key:
            continue
        if name in ESTIMATE_FIELDS[kind]:
            maximum = 1_000_000 if name == "estimated_output_frames" else 100_000
            if type(value) is not int or not 1 <= value <= maximum:
                raise ToolInputValidationError(f"Runway {name} must be a positive integer at most {maximum}.")
            continue  # Pricing metadata is never sent to the media provider.
        if name == "scale_factor":
            if type(value) is not int or value not in (2, 4, 8, 16):
                raise ToolInputValidationError("Runway scale_factor must be 2, 4, 8, or 16.")
        elif name == "flavor":
            options.choice(value, ("sublime", "photo", "photo_denoiser") if kind == "image" else ("vivid", "natural"), name)
        elif name == "resolution":
            options.choice(value, ("720p", "1k", "2k", "4k"), name)
        elif name == "fps_boost":
            if type(value) is not bool:
                raise ToolInputValidationError("Runway fps_boost must be a boolean.")
        elif type(value) is not int or not 0 <= value <= 100:
            raise ToolInputValidationError(f"Runway {name} must be an integer from 0 to 100.")
        body[mapping.get(name, name)] = value
    media = {"asset_id": values[asset_key]}
    body[f"{kind}Uri"] = options.media_uri(media, kind, api, uploads)
    return body


def estimate(kind: str, values: JSONObject, body: JSONObject) -> tuple[str, str]:
    """After request validation, quote published rates; never assume unknown=free."""
    if kind == "image":
        width, height = values.get("source_width"), values.get("source_height")
        if isinstance(width, int) and isinstance(height, int):
            scale = int(str(body["scaleFactor"]))
            high = max(width, height) * scale > 4096
            return ("1.50" if high else "0.25", f"Published 2026-09-27 image tier; supplied source {width}x{height}, scale {scale}. Estimate, not final billing.")
        return "1.50", "Published 2026-09-27 high image tier; source dimensions unknown. Conservatively assumes output exceeds 4096px. Estimate, not final billing."
    frames = values.get("estimated_output_frames")
    if isinstance(frames, int):
        basis = f"Supplied estimate of {frames} output frames, including any FPS boost."
    else:
        fps = 240 if body["fpsBoost"] else 120
        frames = 30 * fps
        basis = f"Output frame count unknown: assumes 30 seconds at {fps} output FPS. Conservative assumption, not a guaranteed upper bound."
    resolution = str(body["resolution"])
    credits = max(Decimal(1), (Decimal(VIDEO_FRAME_USD[resolution]) * frames * 100).to_integral_value(rounding=ROUND_CEILING))
    return format(credits / 100, ".2f"), f"Published 2026-09-27 {resolution} rate, rounded up to whole credits. {basis} Estimate, not final billing."
