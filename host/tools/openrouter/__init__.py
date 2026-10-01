"""OpenRouter video generation through its provider-neutral asynchronous API.

Only fixed API paths are used. Remote polling/content URLs are intentionally
ignored so authentication can never be sent to a provider-controlled host.
"""

from __future__ import annotations

import base64
import re
from typing import cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.openrouter.manifest import MANIFEST
from host.tools.openrouter.costs import quote_usd
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult, StreamingAsset
from host.tools.shared.cost_reporting import report_provider_usd
from host.tools.shared.inputs import ToolInputValidationError, int_field
from host.tools.shared.media import open_downloaded_video
from host.tools.shared.web import (
    ProviderWarning, WebRequestError, json_request, known_provider_transport_error,
    unmapped_provider_error,
)

API_BASE = "https://openrouter.ai/api/v1/videos"
HEYGEN_MODEL = "heygen/heygen-video-1"
# Current documented generation IDs and the earlier 20-character job IDs
# shown in OpenRouter's cookbook. Neither grammar can address another route.
TASK_RE = re.compile(r"(?:gen-vid-[0-9]{10,13}-[A-Za-z0-9]{20}|[A-Za-z0-9]{20})")
RESOLUTION_RE = re.compile(r"[1-9][0-9]{0,3}(?:p|K)")
RATIO_RE = re.compile(r"[1-9][0-9]{0,2}:[1-9][0-9]{0,2}")
ASSET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
MEDIA_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "video/mp4", "video/quicktime", "audio/mpeg", "audio/wav"}
STATUS_MAP = {
    "pending": "queued", "in_progress": "running", "completed": "succeeded",
    "failed": "failed", "cancelled": "cancelled", "expired": "expired",
}
MESSAGES = {
    "queued": "Video queued. Poll get_task in about 30 seconds.",
    "running": "Video is rendering. Poll get_task in about 30 seconds.",
    "succeeded": "Video is ready. Use save_video promptly to keep a durable copy.",
    "failed": "Video generation failed. Check this job in OpenRouter before deciding whether to submit another paid generation.",
    "cancelled": "Video generation was cancelled.",
    "expired": "Video job expired and can no longer be retrieved.",
    "unknown": "OpenRouter returned an unrecognized job state. Poll this job again; do not submit a duplicate generation.",
}


def _identifier(value: JSONValue, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolInputValidationError(f"OpenRouter {field} has an invalid format.")
    return value


def _task_id(value: JSONValue) -> str:
    task_id = _identifier(value, TASK_RE, "task_id")
    if task_id == "models":
        raise ToolInputValidationError("OpenRouter task_id cannot address the model catalog.")
    return task_id


def _check_fields(tool_input: JSONObject, action: str) -> None:
    spec = next((spec for spec in MANIFEST.actions if spec.id == action), None)
    if spec is None:
        raise ToolInputValidationError("Unsupported OpenRouter action.")
    fields = cast(JSONObject, spec.input_schema["properties"])
    if set(tool_input) - fields.keys():
        raise ToolInputValidationError(f"OpenRouter {action} received an unsupported field.")


def _failure(exc: WebRequestError, operation: str) -> str:
    messages = {
        400: "OpenRouter rejected the request. Check HeyGen input compatibility and your OpenRouter account restrictions.",
        401: "OpenRouter authentication failed. Replace OPENROUTER_API_KEY under Home > Integrations.",
        402: "OpenRouter credits or the API key's spending limit are exhausted. Add credit or adjust the key limit in OpenRouter.",
        403: "OpenRouter denied access. Check the API key permissions and account provider/privacy restrictions.",
        404: "OpenRouter could not find this model or video job in the configured account.",
        409: "OpenRouter video content is not ready. Poll the existing job with get_task before saving it.",
        410: "OpenRouter video content has expired. The existing job cannot be downloaded.",
        413: "OpenRouter rejected the media payload as too large. Reduce the source file sizes before submitting again.",
        429: "OpenRouter rate limited the request. Wait before retrying; do not repeat a possibly accepted generation.",
    }
    if exc.status in messages:
        return messages[exc.status]
    # Submission faults can occur after acceptance. Do not invite an automatic
    # POST retry, even for an apparent server or transport failure.
    if operation == "create_heygen_video":
        raise ProviderWarning(
            "OpenRouter", operation,
            "OpenRouter submission outcome is unknown. Check OpenRouter activity before submitting again; a paid video job may already exist.",
            status=exc.status,
        )
    known = known_provider_transport_error(exc)
    if known:
        return known
    raise unmapped_provider_error("OpenRouter", operation, exc)


def _request(method: str, url: str, headers: dict[str, str], *, body: JSONObject | None = None) -> JSONObject:
    try:
        return json_request(
            method, url, headers=headers, body=body,
            failure_message="OpenRouter request failed.",
            invalid_response_message="OpenRouter returned invalid JSON. For a submission, check OpenRouter activity before retrying; a paid job may already exist.",
            timeout=60,
        )
    except WebRequestError as exc:
        operation = "create_heygen_video" if method == "POST" else "heygen_capabilities" if url.endswith("/models") else "get_task"
        raise RuntimeError(_failure(exc, operation)) from exc


def _capabilities(headers: dict[str, str]) -> JSONObject:
    response = _request("GET", f"{API_BASE}/models", headers)
    data = response.get("data")
    if not isinstance(data, list) or len(data) > 2_000:
        raise RuntimeError("OpenRouter returned an invalid video model catalog.")
    selected = next((row for row in data if isinstance(row, dict) and row.get("id") == HEYGEN_MODEL), None)
    if not isinstance(selected, dict):
        raise ToolInputValidationError("HeyGen Video 1 is unavailable in OpenRouter's video catalog.")
    for key in ("supported_durations", "supported_resolutions", "supported_aspect_ratios", "supported_frame_images"):
        value = selected.get(key)
        if value is not None and (not isinstance(value, list) or len(value) > 128):
            raise RuntimeError("OpenRouter returned invalid HeyGen capabilities.")
    return selected


def _staged_asset(value: JSONValue, kind: str, api: HostAPI) -> str:
    asset_id = _identifier(value, ASSET_RE, "staged asset ID")
    metadata = api.assets.describe(asset_id)
    if metadata.media_type not in MEDIA_TYPES or not metadata.media_type.startswith(kind + "/"):
        raise ToolInputValidationError(f"OpenRouter requires a staged {kind} of a supported type.")
    limit = 5_000_000 if kind == "image" else 16_000_000
    if not 1 <= metadata.size_bytes <= limit:
        raise ToolInputValidationError(f"OpenRouter staged {kind} exceeds its inline byte limit.")
    return asset_id


def _generation_body(tool_input: JSONObject, api: HostAPI, headers: dict[str, str]) -> tuple[JSONObject, dict[str, str], str]:
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 5_120:
        raise ToolInputValidationError("OpenRouter prompt must contain 1-5120 UTF-8 bytes.")
    prompt = api.outbound.guard_request_parameter_string(prompt, allow_longer_text=True)
    uploads: dict[str, str] = {}
    frames: list[JSONValue] = []
    refs: list[JSONValue] = []
    total_bytes = 0
    if "image_asset_id" in tool_input:
        asset_id = _staged_asset(tool_input["image_asset_id"], "image", api)
        uploads[asset_id] = "image"
        total_bytes += api.assets.describe(asset_id).size_bytes
        frames.append({"type": "image_url", "image_url": {"url": asset_id}, "frame_type": "first_frame"})
    visual_references = 0
    for kind, maximum in (("image", 9), ("video", 3), ("audio", 3)):
        field = f"reference_{kind}_asset_ids"
        values = tool_input.get(field, [])
        if not isinstance(values, list) or len(values) > maximum:
            raise ToolInputValidationError(f"OpenRouter {field} exceeds its supported count or is not an array.")
        if kind != "audio":
            visual_references += len(values)
        for value in values:
            asset_id = _staged_asset(value, kind, api)
            uploads[asset_id] = kind
            total_bytes += api.assets.describe(asset_id).size_bytes
            refs.append({"type": kind + "_url", kind + "_url": {"url": asset_id}})
    if len(refs) > 12 or total_bytes > 60_000_000:
        raise ToolInputValidationError("OpenRouter references exceed 12 files or 60 MB combined.")
    if frames and refs:
        raise ToolInputValidationError("OpenRouter opening frames cannot be mixed with reference lists.")
    if refs and not visual_references:
        raise ToolInputValidationError("OpenRouter HeyGen reference mode requires an image or video; audio alone is insufficient.")
    mode = "image" if frames else "reference" if refs else "text"
    requested_mode = tool_input.get("mode", "auto")
    if requested_mode not in ("auto", mode):
        raise ToolInputValidationError("OpenRouter mode does not match the supplied media.")
    body: JSONObject = {
        "model": HEYGEN_MODEL, "prompt": prompt,
        "duration": int_field(tool_input, "duration_seconds", provider="OpenRouter", default=5, low=5, high=15),
        "resolution": _identifier(tool_input.get("resolution", "768p"), RESOLUTION_RE, "resolution"),
    }
    if tool_input.get("duration_seconds", "5") is None:
        raise ToolInputValidationError("OpenRouter duration_seconds must be an integer.")
    if "aspect_ratio" in tool_input:
        if mode == "image":
            raise ToolInputValidationError("HeyGen first-frame mode follows the image's shape. Omit aspect_ratio and crop the image before staging if needed.")
        body["aspect_ratio"] = _identifier(tool_input["aspect_ratio"], RATIO_RE, "aspect_ratio")
    elif mode == "text":
        body["aspect_ratio"] = "16:9"
    if "seed" in tool_input:
        if tool_input["seed"] is None:
            raise ToolInputValidationError("OpenRouter seed must be an integer.")
        body["seed"] = int_field(tool_input, "seed", provider="OpenRouter", default=0, low=0, high=4_294_967_295)
    selected = _capabilities(headers)
    for wire, capability in (("duration", "supported_durations"), ("resolution", "supported_resolutions"), ("aspect_ratio", "supported_aspect_ratios")):
        if wire in body and body[wire] not in cast(list[JSONValue], selected.get(capability) or []):
            raise ToolInputValidationError(f"OpenRouter HeyGen does not support this {wire} in its live catalog.")
    if frames and "first_frame" not in cast(list[JSONValue], selected.get("supported_frame_images") or []):
        raise ToolInputValidationError("OpenRouter HeyGen does not advertise opening-frame support in its live catalog.")
    if "seed" in body and selected.get("seed") is not True:
        raise ToolInputValidationError("OpenRouter HeyGen does not advertise seed support.")
    if frames:
        body["frame_images"] = frames
    if refs:
        body["input_references"] = refs
    return body, uploads, quote_usd(body, selected, api)


def _encode_media(body: JSONObject, uploads: dict[str, str], api: HostAPI) -> None:
    encoded: dict[str, str] = {}
    for asset_id in uploads:
        metadata = api.assets.describe(asset_id)
        with api.assets.open(asset_id) as source:
            data = source.read(metadata.size_bytes + 1)
        if len(data) != metadata.size_bytes:
            raise ToolInputValidationError("OpenRouter staged media byte count changed; stage the file again.")
        media_type = "image/jpeg" if metadata.media_type == "image/jpg" else metadata.media_type
        encoded[asset_id] = f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")
    for key in ("frame_images", "input_references"):
        for raw in cast(list[JSONValue], body.get(key, [])):
            row = cast(JSONObject, raw)
            reference = cast(JSONObject, row[cast(str, row["type"])])
            reference["url"] = encoded[cast(str, reference["url"])]


def _task_result(response: JSONObject, task_id: str) -> JSONObject:
    raw_status = response.get("status")
    status = STATUS_MAP.get(raw_status, "unknown") if isinstance(raw_status, str) else "unknown"
    result: JSONObject = {"task_id": task_id, "task_status": status, "message": MESSAGES[status]}
    # Never echo provider error text, prompts, URLs, or opaque response fields.
    return result


def _poll(task_id: str, headers: dict[str, str]) -> JSONObject:
    response = _request("GET", f"{API_BASE}/{task_id}", headers)
    if response.get("id") != task_id:
        raise RuntimeError("OpenRouter returned a mismatched video job ID.")
    return _task_result(response, task_id)


class OpenRouterTool:
    manifest = MANIFEST
    credentials = None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            _check_fields(tool_input, action)
            headers = {"Authorization": f"Bearer {api.config['OPENROUTER_API_KEY']}"}
            if action == "create_heygen_video":
                body, uploads, estimate = _generation_body(tool_input, api, headers)
                _encode_media(body, uploads, api)
                response = _request("POST", API_BASE, headers, body=body)
                try:
                    task_id = _task_id(response.get("id"))
                except ToolInputValidationError:
                    return ActionFailed("OpenRouter returned no valid video job ID. Check OpenRouter activity before submitting again; a paid job may already exist.")
                report_provider_usd(api, estimate, charge_id=f"openrouter-video:{task_id}")
                result = _task_result(response, task_id)
                for asset_id in uploads:
                    try:
                        api.assets.delete(asset_id)
                    except (ValueError, RuntimeError, OSError):
                        # Cleanup must never hide an already accepted paid job.
                        result["message"] = str(result["message"]) + " A staged copy could not be removed; its normal expiry still applies. Continue using this job ID."
                return ActionExecuted(result)
            task_id = _task_id(tool_input.get("task_id"))
            result = _poll(task_id, headers)
            if action == "get_task":
                return ActionExecuted(result)
            if result["task_status"] != "succeeded":
                return ActionFailed(f"OpenRouter video cannot be saved yet. {result['message']}")
            return StreamingAsset(lambda: open_downloaded_video(
                f"{API_BASE}/{task_id}/content?index=0", provider="OpenRouter",
                filename_stem=f"openrouter-{task_id}", headers=headers,
                map_failure=lambda exc: _failure(exc, "save_video"),
            ))
        except WebRequestError as exc:
            return ActionFailed(_failure(exc, action))
        except ProviderWarning:
            raise
        except (ValueError, RuntimeError) as exc:
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        return ActionFailed("OpenRouter has no approval-gated actions.")


BUNDLED_TOOL = OpenRouterTool()
