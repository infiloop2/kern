"""Topaz image/video enhancement through pinned fal queue endpoints."""
from __future__ import annotations

import math
import re
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink,
    SetupStep, ToolManifest, protect_inputs, validated_input,
)
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult, StreamingAsset
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.fal_ai import media as fal_media
from host.tools.shared.media import open_downloaded_image, open_downloaded_video
from host.tools.shared.web import WebRequestError, json_request

QUEUE_BASE = "https://queue.fal.run/topaz/upscale"
LIFECYCLE = '{"expiration_duration_seconds":86400}'
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
TASK_RE = re.compile(rf"^topaz_(image|video)_({UUID})$")
IMAGE_MODELS = ("Standard V2", "High Fidelity V2", "Low Resolution V2", "CGI", "Text Refine")
PRECISION_MODELS = ("Proteus", "Proteus Natural", "Artemis High Quality", "Artemis Medium Quality",
                    "Artemis Low Quality", "Gaia HQ", "Gaia CG", "Gaia 2", "Rhea")
GENERATIVE_MODELS = ("Starlight Precise 2.6", "Starlight HQ", "Starlight Mini", "Starlight Sharp", "Starlight Fast 2")
BOUNDS = {"upscale_factor": (1, 4), "target_fps": (16, 60), "grain": (0, .1), "softness": (1, 5)}


def choice(values: tuple[str, ...], description: str) -> JSONObject:
    return {"type": "string", "enum": list(values), "description": description}


def number(description: str, *, integer: bool = False) -> JSONObject:
    return {"type": "integer" if integer else "number", "description": description}


COMMON_VIDEO: JSONObject = {
    "model": choice(PRECISION_MODELS + GENERATIVE_MODELS, "Proteus by default. Gaia CG/2 suit animation. Starlight invents detail; opt in deliberately."),
    "upscale_factor": number("Scale multiplier, 1-4; default 2. Proteus Natural and Gaia 2 require 2."),
    "target_fps": number("Optional target FPS, 16-60. Omit to preserve source FPS; changing it adds interpolation and cost.", integer=True),
    "compression": number("Precision models only: compression cleanup, 0-1; omitted uses provider default."),
    "noise": number("Precision models only: noise reduction, 0-1; omitted uses provider default."),
    "halo": number("Precision models only: halo reduction, 0-1; omitted uses provider default."),
    "grain": number("Precision models only: grain, 0-0.1; omitted uses provider default."),
    "recover_detail": number("Precision models only: original detail recovery, 0-1."),
    "softness": number("Starlight Precise 2.6 only: 1 sharpest to 5 softest; omitted uses provider default."),
    "h264_output": {"type": "boolean", "description": "Default true for H.264 MP4 playback compatibility; false requests provider H.265."},
}
COMMON_IMAGE: JSONObject = {
    "model": choice(IMAGE_MODELS, "Standard V2 by default. CGI is intended for artwork and rendered graphics."),
    "upscale_factor": number("Scale multiplier, 1-4; default 2. Output dimensions follow the source aspect ratio."),
    "output_format": choice(("png", "jpeg"), "Default png to avoid another lossy keyframe encode."),
    "face_enhancement": {"type": "boolean", "description": "Default false to preserve drawn faces. Enable only when face recovery is intended."},
    "sharpen": number("Sharpening, 0-1; omitted uses provider default."),
    "denoise": number("Denoising, 0-1; omitted uses provider default."),
    "fix_compression": number("Compression cleanup, 0-1; unavailable with CGI."),
}


def input_fields(kind: str) -> JSONObject:
    return {
        f"{kind}_asset_id": {"type": "string", "description": f"Opaque id from stage_{kind}(for_tool=fal_ai). Workspace bytes upload only when this action runs."},
        **(COMMON_IMAGE if kind == "image" else COMMON_VIDEO),
    }


TASK_INPUT = outputs.obj({"task_id": outputs.text("Unchanged task_id returned by upscale_image or upscale_video.")}, ["task_id"])
TASK_OUTPUT = outputs.obj({
    "task_id": outputs.text("Pass unchanged to get_task and the matching save action."),
    "task_status": choice(("queued", "running", "succeeded", "failed", "unknown"), "Stable task lifecycle."),
    "output_kind": choice(("image", "video"), "Result media kind."),
    "message": outputs.text("Status guidance, including whether the result can be saved."),
    "output_url": outputs.text("Temporary provider media URL on success. Save promptly; requested lifetime is 24 hours."),
}, ["task_id", "task_status", "output_kind", "message"])


def enhancement_action(kind: str) -> ActionSpec:
    fields = input_fields(kind)
    return ActionSpec(
        id=f"upscale_{kind}",
        description=f"Start paid async Topaz {kind} enhancement through fal. Returns a task_id; poll get_task, then save_{kind}. No social publishing.",
        data_policy="Sends the staged media bytes plus enhancement settings to fal/Topaz immediately, without approval. Uploaded media is URL-accessible. Requests a 24-hour media lifetime and disables fal request-history storage.",
        cost_description="Paid fal inference. Price depends on delivered image pixels or video duration, resolution and FPS. Exact cost is not available from this queue response and is not tracked by Kern; consult fal billing.",
        input_schema=outputs.obj(fields, [f"{kind}_asset_id"]), output_schema=TASK_OUTPUT,
        input_protections={name: validated_input(
            "Host-scoped staged media id, type and size checked before upload." if name.endswith("_asset_id")
            else "Listed choice, strict boolean, or finite number within the declared range; model compatibility checked before upload."
        ) for name in fields},
    )


MANIFEST = ToolManifest(
    tool_id="fal_ai", display_name="falAI",
    description="Upscale and clean up image keyframes and videos with Topaz through fal, with optional video frame interpolation.",
    connection="enable_only", reports_cost=False,
    config=(ConfigRequirement(key="FAL_API_KEY", description="API-scoped fal key. This is a fal integration, not a Topaz desktop license or direct Topaz API key."),),
    actions=(enhancement_action("image"), enhancement_action("video"), *protect_inputs(tuple(
        ActionSpec(
            id=action, description=description,
            data_policy="Sends the validated request id to fal. Save actions also download the authoritative result and stream it to a private /tool_assets workspace file.",
            cost_description="No new enhancement is submitted. Original task charges remain in fal billing.",
            input_schema=TASK_INPUT, output_schema=TASK_OUTPUT if action == "get_task" else {},
            returns_asset=action != "get_task",
        ) for action, description in (
            ("get_task", "Poll an existing Topaz task; completed failures are reported without submitting another paid job."),
            ("save_image", "Save the completed enhanced JPEG/PNG/WebP to the workspace before the provider URL expires."),
            ("save_video", "Save the completed enhanced MP4/MOV to the workspace before the provider URL expires. Outputs must fit Kern's 200 MB asset limit."),
        )
    ), {action: {"task_id": validated_input("topaz_image_ or topaz_video_ followed by a UUID.")} for action in ("get_task", "save_image", "save_video")})),
    setup_steps=(
        SetupStep(title="Fund fal and create an API key", description="Topaz processing is billed by fal. Create an API-scoped key for the intended account; a Topaz desktop subscription does not cover these calls.", link_url="https://fal.ai/dashboard/keys", link_label="Open fal API keys"),
        SetupStep(title="Configure Topaz in Kern", description="Save FAL_API_KEY under Home > Integrations > falAI, then enable the tool. H3 Max and Topaz share this falAI key.", show_config=True),
        SetupStep(title="Check usage charges", description="Kern does not calculate Topaz charges. Check fal billing and current model prices before starting batches.", link_url="https://fal.ai/dashboard/billing", link_label="Open fal billing"),
    ),
    protections=("Fixed Topaz endpoints; keys stay in write-only tool config. Settings validate before any workspace upload.",
                 "Uploads stream with bounded sizes and generic filenames. Saving never publishes to social accounts.",
                 "Requests disable fal history and request 24-hour media expiry. Anyone holding a provider media URL may access it until expiry.", PARAM_GUARD_PROTECTION),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,
                       "Queue submissions select /topaz/upscale/image/precision or /video/precision or /video/generative. Polling uses the fal app root /topaz/upscale/requests/{uuid}, not the submission subroute. No automatic paid submission retries.",
                       "Workspace media is streamed via fal's documented storage upload initialization and signed GCS PUT protocol. Downloaded outputs are limited to 200 MB. Remote input media limits are enforced by fal/Topaz."),
    data_summary=DataSummary(cards=(
        DataSummaryCard(title="What leaves this host", description="The selected source image/video bytes, enhancement settings, and request ids. Workspace paths and original filenames are not sent. Caller-supplied media URLs are not accepted."),
        DataSummaryCard(title="Where it can go", description="fal's queue and storage services, Topaz processing, and their infrastructure providers. Inputs are uploaded to fal storage internally.", links=(DataSummaryLink(label="Topaz on fal", url="https://fal.ai/topaz"),)),
        DataSummaryCard(title="What providers can do with it", description="fal and Topaz process the media to provide enhancement under their terms and privacy policies. This integration makes no extra confidentiality or training-use guarantees.", links=(DataSummaryLink(label="fal privacy", url="https://fal.ai/privacy"), DataSummaryLink(label="Topaz privacy", url="https://www.topazlabs.com/privacy-policy"))),
        DataSummaryCard(title="How long it is retained", description="Kern sends X-Fal-Store-IO: 0 and requests 24-hour expiry for uploaded and generated media. This does not erase provider billing records or promise deletion from all downstream systems. Saved workspace files remain until removed.", links=(DataSummaryLink(label="fal retention", url="https://fal.ai/docs/documentation/model-apis/media-expiration"),)),
    )),
    agent_notes="Start with CGI for illustrated keyframes and Gaia CG or Gaia 2 for animation. Preserve source FPS unless interpolation is wanted. Starlight can alter artwork; compare motion before choosing it. Stage local files for_tool=fal_ai and pass the id, never base64. One successful submission consumes the staged id. Save promptly. Topaz costs are NOT tracked; check fal billing.",
)


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Key {key}", "X-Fal-Store-IO": "0", "X-Fal-Object-Lifecycle-Preference": LIFECYCLE}


def _request(api: HostAPI, kind: str, values: JSONObject) -> tuple[str, JSONObject, str]:
    fields = input_fields(kind)
    if set(values) - set(fields):
        raise ToolInputValidationError("Topaz received an unsupported field.")
    asset_key = f"{kind}_asset_id"
    if asset_key not in values:
        raise ToolInputValidationError(f"Provide {asset_key} from workspace staging.")
    body: JSONObject = {"model": "Standard V2" if kind == "image" else "Proteus", "upscale_factor": 2}
    body.update({"output_format": "png", "face_enhancement": False} if kind == "image" else {"H264_output": True})
    for name, value in values.items():
        if name == asset_key:
            continue
        schema = cast(JSONObject, fields[name])
        if "enum" in schema:
            if not isinstance(value, str) or value not in cast(list, schema["enum"]):
                raise ToolInputValidationError(f"Topaz {name} must be a listed choice.")
        elif schema["type"] == "boolean":
            if type(value) is not bool:
                raise ToolInputValidationError(f"Topaz {name} must be a boolean.")
        else:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (schema["type"] == "integer" and not isinstance(value, int))
                or not math.isfinite(value) or not BOUNDS.get(name, (0, 1))[0] <= value <= BOUNDS.get(name, (0, 1))[1]):
                raise ToolInputValidationError(f"Topaz {name} must be a finite number within the declared range.")
        body["H264_output" if name == "h264_output" else name] = value
    model = body["model"]
    if kind == "image" and model == "CGI" and "fix_compression" in body:
        raise ToolInputValidationError("CGI does not support fix_compression.")
    if model in ("Gaia 2", "Proteus Natural") and body["upscale_factor"] != 2:
        raise ToolInputValidationError("Gaia 2 and Proteus Natural require upscale_factor 2.")
    generative = model in GENERATIVE_MODELS
    if generative and any(k in body for k in ("compression", "noise", "halo", "grain", "recover_detail")):
        raise ToolInputValidationError("Precision cleanup controls cannot be used with Starlight models.")
    if "softness" in body and model != "Starlight Precise 2.6":
        raise ToolInputValidationError("softness requires Starlight Precise 2.6.")
    asset_id = fal_media.staged_asset_id(values[asset_key], kind, api)
    return f"{QUEUE_BASE}/{kind}/{'generative' if generative else 'precision'}", body, asset_id


def _task(values: JSONObject) -> tuple[str, str, str]:
    value = values.get("task_id")
    if set(values) != {"task_id"} or not isinstance(value, str) or not (match := TASK_RE.fullmatch(value)):
        raise ToolInputValidationError("Topaz requires exactly one unchanged task_id.")
    kind, request_id = match.groups()
    return value, kind, request_id


def _poll(task_id: str, kind: str, request_id: str, headers: dict[str, str]) -> JSONObject:
    # fal queues all subroutes under owner/app, as in fal-client's from_request_id.
    base = f"{QUEUE_BASE}/requests/{request_id}"
    status = json_request("GET", base + "/status", headers=headers,
        failure_message="fal status request failed.", invalid_response_message="fal returned an invalid status.")
    state = {"IN_QUEUE": "queued", "IN_PROGRESS": "running", "COMPLETED": "succeeded"}.get(str(status.get("status")), "unknown")
    result: JSONObject = {"task_id": task_id, "output_kind": kind, "task_status": state, "message": "Poll get_task again shortly."}
    if state != "succeeded":
        return result
    if status.get("error") is not None or status.get("error_type") is not None:
        result.update(task_status="failed", message="Topaz processing failed. Inspect the request in fal before deciding whether to submit another paid job.")
        return result
    try:
        response = json_request("GET", base, headers=headers,
            failure_message="fal result request failed.", invalid_response_message="fal returned an invalid result.")
    except WebRequestError as exc:
        if exc.status in (400, 422):
            result.update(task_status="failed", message="Topaz processing rejected the media or enhancement settings.")
            return result
        raise
    media = response.get(kind)
    if not isinstance(media, dict) or not media.get("url"):
        result.update(task_status="failed", message="Topaz completed without the expected media result.")
        return result
    result.update(output_url=fal_media.provider_url(media.get("url")), message=f"Enhancement succeeded. Call save_{kind} promptly to retain it.")
    return result


def _failure(exc: WebRequestError) -> str:
    return {
        401: "fal rejected FAL_API_KEY. Update it under Home > Integrations.",
        402: "fal billing credit is insufficient for Topaz processing.",
        403: "fal denied this request. Check account access and provider policy.",
        404: "fal task or media was not found; it may have expired.",
        422: "fal rejected the media or enhancement settings.",
        429: "fal rate or concurrency limit reached.",
    }.get(exc.status, "Topaz provider request failed. Submission failures may have an unknown outcome; check fal before resubmitting.")


class TopazTool:
    manifest = MANIFEST
    credentials = None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action not in {a.id for a in MANIFEST.actions}:
                return ActionFailed("Unsupported Topaz action.")
            try:
                key = api.config["FAL_API_KEY"]
            except (KeyError, RuntimeError):
                return ActionFailed("FAL_API_KEY is not set. Configure it under Home > Integrations > falAI.")
            if not key:
                return ActionFailed("FAL_API_KEY must not be empty.")
            headers = _headers(key)
            if action in ("upscale_image", "upscale_video"):
                kind = action.removeprefix("upscale_")
                endpoint, body, asset_id = _request(api, kind, tool_input)
                if asset_id:
                    body[f"{kind}_url"] = fal_media.upload(asset_id, api, headers)
                response = json_request("POST", endpoint, headers=headers, body=body,
                    failure_message="Topaz submission failed; check fal before retrying.", invalid_response_message="fal returned an invalid submission response; check fal before retrying.")
                request_id = response.get("request_id")
                if not isinstance(request_id, str) or not re.fullmatch(UUID, request_id):
                    return ActionFailed("fal returned no valid request id. Check fal before resubmitting to avoid duplicate charges.")
                if asset_id:
                    api.assets.delete(asset_id)
                return ActionExecuted({"task_id": f"topaz_{kind}_{request_id}", "task_status": "queued", "output_kind": kind,
                    "message": "Topaz task accepted. Poll get_task. Paid usage is billed by fal and is not tracked by Kern."})
            task_id, kind, request_id = _task(tool_input)
            if action.startswith("save_") and action != f"save_{kind}":
                return ActionFailed("Use the save action matching this task's output kind.")
            task = _poll(task_id, kind, request_id, headers)
            if action == "get_task":
                return ActionExecuted(task)
            if task["task_status"] != "succeeded":
                return ActionFailed(str(task["message"]))
            url = cast(str, task["output_url"])
            opener = open_downloaded_image if kind == "image" else open_downloaded_video
            return StreamingAsset(lambda: opener(url, provider="Topaz", filename_stem=f"topaz-{request_id}", map_failure=_failure))
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            return ActionFailed(_failure(exc))
        except ValueError as exc:
            # Host asset validation and parameter-guard errors are value-free.
            return ActionFailed(str(exc))
        except Exception:
            return ActionFailed("Topaz request failed. If submission was attempted, check fal before resubmitting.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        return ActionFailed("Topaz has no approval-gated actions.")


