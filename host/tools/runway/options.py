"""Runway video choices and media inputs, shared by generation and editing.

Provider contract: runwayml/sdk-python types/*_create_params.py and
docs.dev.runwayml.com/assets/inputs (audited 2026-09-17).
"""

from __future__ import annotations

import math
from typing import cast

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared.inputs import ToolInputValidationError, provider_fetched_https_url, schema

SEEDANCE_MODELS = ("seedance2", "seedance2_fast", "seedance2_5")
GEN4_IMAGE_RATIOS = ("1280:720", "720:1280", "1104:832", "832:1104", "960:960", "1584:672", "672:1584")
SEEDANCE_480 = ("992:432", "864:496", "752:560", "640:640", "560:752", "496:864")
SEEDANCE_25_480 = ("992:432", "854:480", "752:560", "640:640", "560:752", "480:854")
SEEDANCE_720 = ("1470:630", "1280:720", "1112:834", "960:960", "834:1112", "720:1280")
SEEDANCE_1080 = ("2206:946", "1920:1080", "1664:1248", "1440:1440", "1248:1664", "1080:1920")
SEEDANCE_4K = ("3840:1646", "3840:2160", "3840:2880", "3840:3840", "2880:3840", "2160:3840")
MODEL_RATIOS = {
    "seedance2": SEEDANCE_480 + SEEDANCE_720 + SEEDANCE_1080 + SEEDANCE_4K,
    "seedance2_fast": SEEDANCE_480 + SEEDANCE_720,
    "seedance2_5": SEEDANCE_25_480 + SEEDANCE_720 + SEEDANCE_1080,
    "veo3.1": ("1280:720", "720:1280", "1920:1080", "1080:1920"),
    "veo3.1_fast": ("1280:720", "720:1280", "1920:1080", "1080:1920"),
}
ALL_VIDEO_RATIOS = tuple(dict.fromkeys(GEN4_IMAGE_RATIOS + tuple(r for ratios in MODEL_RATIOS.values() for r in ratios)))
OUTPUT_FORMATS = (
    "mp4", "prores", "png_sequence", "hdr10", "hlg", "sdr_rec709_10bit",
    "hdr_pq_12bit_master", "hdr_prores", "hdr_png_sequence", "hdr_exr_sequence",
    "hdr_exr_acescg_sequence_1_3", "hdr_exr_acescg_sequence_2_0",
)
ALEPH_FORMATS = ("mp4", "prores", "png_sequence", "sdr_rec709_10bit")
PRORES_PROFILES = ("422", "4444", "422 Proxy", "422 LT", "422 HQ", "4444 XQ")
ASPECT_RATIOS = ("16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16", "21:9")


def choice_schema(values: tuple[str, ...], description: str) -> JSONObject:
    return {"type": "string", "enum": list(values), "description": description}


MEDIA_PROPERTIES: JSONObject = {
    "uri": {"type": "string", "description": "Public HTTPS media URL or existing runway:// upload URI. Use exactly one of uri or asset_id; stage local files instead of inline base64."},
    "asset_id": {"type": "string", "description": "Built-in workspace media reference, uploaded only after the complete request validates."},
}


def media_array(description: str, extras: JSONObject | None = None, maximum: int = 30) -> JSONObject:
    return {"type": "array", "minItems": 1, "maxItems": maximum,
            "items": schema({**MEDIA_PROPERTIES, **(extras or {})}), "description": description}


FORMAT_PROPERTIES: JSONObject = {
    "output_format": choice_schema(OUTPUT_FORMATS, "Gen-4.5/Aleph only. Aleph: mp4, prores, png_sequence, sdr_rec709_10bit. Other formats are Gen-4.5 only and may require account access. Non-default formats cost extra. ZIP sequences are returned in get_task.output_urls; save_video saves MP4/MOV only."),
    "prores_profile": choice_schema(PRORES_PROFILES, "Only with prores or hdr_prores output. HDR supports 422, 422 HQ, or 4444 only."),
    "public_figure_threshold": choice_schema(("auto", "low"), "Gen-4/Aleph content moderation setting; omit for provider default."),
}
VIDEO_PROPERTIES: JSONObject = {
    "prompt_images": media_array("First/last keyframes: position first or last. Gen-4 accepts first only; H3 Max requires first before last. Seedance also accepts unpositioned reference images; do not mix them with keyframes.", {"position": choice_schema(("first", "last"), "Omit only for Seedance reference images.")}),
    "reference_images": media_array("Seedance text/video-to-video reference images: up to 9 for 2.0/Fast, 30 for 2.5. Cannot combine with prompt_images or first-frame shorthand."),
    "reference_videos": media_array("Seedance text/video-to-video video references. Provider checks combined duration: 15s for 2.0/Fast, 30s for 2.5."),
    "reference_audio": {"type": "array", "minItems": 1, "maxItems": 30,
                        "items": schema({"uri": MEDIA_PROPERTIES["uri"]}, ["uri"]),
                        "description": "Seedance audio reference URLs or runway:// URIs (workspace audio staging is not available). Provider checks combined duration: 15s for 2.0/Fast, under 30s for 2.5."},
    "video_url": {"type": "string", "description": "Seedance video-to-video source, public HTTPS URL or runway:// URI. Mutually exclusive with video_asset_id and image keyframes."},
    "video_asset_id": {"type": "string", "description": "Seedance video-to-video source from the workspace. Mutually exclusive with video_url and image keyframes."},
    "resolution": choice_schema(("480p", "768p"), "H3 Max only; default 768p. Seedance selects resolution through ratio pixel dimensions."),
    "prompt_expansion_mode": choice_schema(("disabled", "balanced", "quality"), "H3 Max only. Provider default balanced; disabled avoids rewriting for more repeatable seeds."),
    "audio": {"type": "boolean", "description": "Veo/Seedance native audio toggle. Omit for provider default; audio affects pricing."},
    "negative_prompt": {"type": "string", "description": "Veo only: what to avoid in the output."},
    "mode": choice_schema(("reference", "extend", "edit"), "Seedance 2.5 video-to-video only. Extend/edit require prompt and omit ratio; edit requires duration_seconds=auto."),
    **FORMAT_PROPERTIES,
}
EDIT_PROPERTIES: JSONObject = {
    **FORMAT_PROPERTIES,
    "ratio": {"type": "string", "description": "Aleph output width:height; omit to preserve the input dimensions. Provider validates supported dimensions."},
    "target_aspect_ratio": choice_schema(ASPECT_RATIOS, "Aleph expand/outpaint target; letterboxes the source and keyframes."),
    "keyframes": media_array("Aleph timed image guidance (up to 5). Set exactly one of seconds or at. All keyframes must specify range or none may.", {
        "seconds": {"type": "number", "description": "Non-negative absolute timestamp in seconds."},
        "at": {"type": "number", "description": "Fraction of the source duration, 0 to 1."},
        "range": schema({"start_seconds": {"type": "integer"}, "end_seconds": {"type": "integer"}}, ["start_seconds", "end_seconds"]),
    }, 5),
}


def choice(value: JSONValue, allowed: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(f"Runway {name} must be one of {', '.join(allowed)}.")
    return value


def text(value: JSONValue, name: str, api: HostAPI, limit: int | None = None, *, allow_longer_text: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputValidationError(f"Runway {name} must be a non-empty string.")
    value = value.strip()
    value = api.outbound.guard_request_parameter_string(value, allow_longer_text=allow_longer_text)
    if limit is not None and len(value.encode("utf-16-le")) // 2 > limit:
        raise ToolInputValidationError(f"Runway {name} must be at most {limit} UTF-16 characters.")
    return value


def media_uri(value: JSONObject, kind: str, api: HostAPI, uploads: dict[str, str]) -> str:
    uri, asset_id = value.get("uri"), value.get("asset_id")
    if (uri is None) == (asset_id is None):
        raise ToolInputValidationError("Runway media requires exactly one of uri or asset_id.")
    if asset_id is not None:
        if kind == "audio":
            raise ToolInputValidationError("Runway audio references require a URI; workspace audio staging is not available.")
        if not isinstance(asset_id, str) or not asset_id:
            raise ToolInputValidationError("Runway asset_id must be a non-empty string.")
        if not api.assets.describe(asset_id).media_type.startswith(f"{kind}/"):
            raise ToolInputValidationError(f"Runway asset_id does not refer to a staged {kind}.")
        uploads[asset_id] = kind
        return f"kern-asset:{asset_id}"
    if isinstance(uri, str) and uri.startswith("runway://") and len(uri) <= 2048 and len(uri) > 9:
        return api.outbound.guard_request_parameter_string(uri)
    return provider_fetched_https_url({"uri": uri}, "uri", api, provider="Runway")


def media_list(value: JSONValue, kind: str, api: HostAPI, uploads: dict[str, str], *, maximum: int = 30, extras: tuple[str, ...] = ()) -> list[JSONObject]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ToolInputValidationError(f"Runway {kind} references must contain 1 to {maximum} items.")
    result: list[JSONObject] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"uri", "asset_id", *extras}:
            raise ToolInputValidationError(f"Runway {kind} reference has unsupported fields.")
        result.append({"uri": media_uri(item, kind, api, uploads), **{k: item[k] for k in extras if k in item}})
    return result


def replace_assets(value: JSONValue, uploaded: dict[str, str]) -> JSONValue:
    if isinstance(value, list):
        return [replace_assets(v, uploaded) for v in value]
    if isinstance(value, dict):
        return {k: uploaded[v[len("kern-asset:"):]]
                if k in {"uri", "promptImage", "promptVideo", "videoUri"} and isinstance(v, str) and v.startswith("kern-asset:")
                else replace_assets(v, uploaded) for k, v in value.items()}
    return value


def format_options(tool_input: JSONObject, body: JSONObject, model: str) -> None:
    if "output_format" in tool_input:
        if model not in {"gen4.5", "aleph2"}:
            raise ToolInputValidationError("Runway output_format requires gen4.5 or aleph2.")
        body["outputFormat"] = choice(tool_input["output_format"], ALEPH_FORMATS if model == "aleph2" else OUTPUT_FORMATS, "output_format")
    if "prores_profile" in tool_input:
        if body.get("outputFormat") not in {"prores", "hdr_prores"}:
            raise ToolInputValidationError("Runway prores_profile requires prores or hdr_prores output_format.")
        allowed = ("422", "422 HQ", "4444") if body["outputFormat"] == "hdr_prores" else PRORES_PROFILES
        body["proresProfile"] = choice(tool_input["prores_profile"], allowed, "prores_profile")
    if "public_figure_threshold" in tool_input:
        if model not in {"gen4.5", "gen4_turbo", "aleph2"}:
            raise ToolInputValidationError("Runway public_figure_threshold requires Gen-4 or Aleph.")
        body["contentModeration"] = {"publicFigureThreshold": choice(tool_input["public_figure_threshold"], ("auto", "low"), "public_figure_threshold")}


def keyframes(value: JSONValue, api: HostAPI, uploads: dict[str, str]) -> list[JSONObject]:
    frames = media_list(value, "image", api, uploads, maximum=5, extras=("at", "seconds", "range"))
    for frame in frames:
        if ("at" in frame) == ("seconds" in frame):
            raise ToolInputValidationError("Runway keyframe requires exactly one of at or seconds.")
        field = "at" if "at" in frame else "seconds"
        number = frame[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0 or (field == "at" and number > 1):
            raise ToolInputValidationError("Runway keyframe timestamp is invalid.")
        if "range" in frame:
            window = frame["range"]
            if not isinstance(window, dict) or set(window) != {"start_seconds", "end_seconds"}:
                raise ToolInputValidationError("Runway keyframe range needs start_seconds and end_seconds.")
            start, end = window["start_seconds"], window["end_seconds"]
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)) or not 0 <= cast(int, start) < cast(int, end):
                raise ToolInputValidationError("Runway keyframe range must contain increasing non-negative whole seconds.")
            if field == "seconds" and not cast(int, start) <= number < cast(int, end):
                raise ToolInputValidationError("Runway keyframe seconds must fall inside its range.")
    if any("range" in f for f in frames) and not all("range" in f for f in frames):
        raise ToolInputValidationError("Runway keyframes must all have range or none may.")
    return frames
