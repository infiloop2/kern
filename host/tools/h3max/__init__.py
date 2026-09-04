"""H3 Max video generation through fal's asynchronous Model API.

H3 Max is fal's post-trained variant of the open-weight MiniMax H3 model. fal
states that it performs the post-training and hosts inference itself. The model
has separate text, keyframe-image, and multimodal-reference routes; this package
selects one of those three pinned routes from the supplied inputs and encodes the
route into the task id so later polls cannot be pointed at an arbitrary endpoint.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from typing import cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec,
    ConfigRequirement,
    DataSummary,
    DataSummaryCard,
    DataSummaryLink,
    DataSummaryPoint,
    SetupStep,
    ToolManifest,
)
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionResult,
    ApprovalResult,
    StreamingAsset,
)
from host.tools.shared import outputs
from host.tools.shared.inputs import (
    ToolInputValidationError,
    guard_url_parameter_string,
    provider_fetched_https_url,
)
from host.tools.shared.media import open_downloaded_video
from host.tools.shared.web import (
    WebRequestError,
    is_public_https_url,
    json_request,
    known_provider_transport_error,
    unmapped_provider_error,
)

QUEUE_BASE = "https://queue.fal.run"
MODEL_PREFIX = "minimax/h3-max"
ROUTES = {
    "text": f"{MODEL_PREFIX}/text-to-video",
    "image": f"{MODEL_PREFIX}/image-to-video",
    "reference": f"{MODEL_PREFIX}/reference-to-video",
}

# Prompts pass through the host guard, whose hard wire limit is 1024 bytes.
# Advertising a larger character cap would promise a value that often cannot
# leave this host. Multi-byte text can still reach the guard's byte limit first.
MAX_PROMPT_CHARS = 1_000
MIN_DURATION_SECONDS = 5
MAX_DURATION_SECONDS = 15
DEFAULT_DURATION_SECONDS = 5
RESOLUTIONS = ("480P", "768P")
DEFAULT_RESOLUTION = "768P"
TEXT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
REFERENCE_RATIOS = ("adaptive", *TEXT_RATIOS)
DEFAULT_TEXT_RATIO = "16:9"
DEFAULT_REFERENCE_RATIO = "adaptive"
PROMPT_EXPANSION_MODES = ("balanced", "quality")
DEFAULT_PROMPT_EXPANSION_MODE = "balanced"
MAX_REFERENCE_FILES = 12

# fal documents UUID request ids. Requiring that grammar before interpolation
# keeps a provider value or agent-supplied task id from walking the queue path.
REQUEST_ID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
TASK_ID_RE = re.compile(
    r"^(text|image|reference)_([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$"
)
ERROR_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# fal stores request JSON for 30 days by default. This tool opts out on every
# request and gives generated public CDN objects a short, explicit lifecycle.
OUTPUT_LIFETIME_SECONDS = 24 * 60 * 60
OBJECT_LIFECYCLE_HEADER = json.dumps(
    {"expiration_duration_seconds": OUTPUT_LIFETIME_SECONDS}, separators=(",", ":")
)

QUEUE_STATUSES = frozenset({"IN_QUEUE", "IN_PROGRESS", "COMPLETED"})

GENERATE_POLICY = (
    "Sends the prompt, generation settings, and any public reference-media URLs to fal to "
    "generate a native-audio H3 Max video, billed to the deployment's fal account. This "
    "runs directly with no approval and publishes nothing. fal's optional safety checker is disabled; "
    "request-history storage is disabled, and the generated public CDN object expires after 24 hours."
)
POLL_POLICY = (
    "Read-only poll. Sends only the validated H3 Max task id to fal and returns its bounded "
    "status plus the temporary public video URL, seed, and inference time after success."
)
SAVE_POLICY = (
    "Read-only handoff. Sends only the validated task id to fal, downloads the completed video "
    "from the authoritative temporary output URL, and streams it through the agent-side bridge "
    "into a host-generated /tool_assets path in the agent workspace."
)

GENERATE_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("Confirmation that fal queued the task and what to poll."),
        "task_id": outputs.text("Kern H3 Max task id; pass unchanged to get_task and save_video."),
        "task_status": outputs.text("Always queued for a task this call just created."),
        "generation_mode": {
            "type": "string",
            "enum": ["text", "image", "reference"],
            "description": "Pinned H3 Max route selected from the supplied reference inputs.",
        },
        "model": outputs.text("Exact fal model endpoint that will render the video."),
        "output_kind": outputs.text("Always video for this tool."),
    },
    ["message", "task_id", "task_status", "generation_mode", "model", "output_kind"],
)
GET_TASK_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("What the task is doing or how its terminal result ended."),
        "task_id": outputs.text("The validated H3 Max task id that was polled."),
        "task_status": {
            "type": "string",
            "enum": ["queued", "running", "succeeded", "failed", "unknown"],
            "description": "fal queue state narrowed to Kern's stable task lifecycle.",
        },
        "generation_mode": {
            "type": "string",
            "enum": ["text", "image", "reference"],
            "description": "Pinned H3 Max route encoded in the task id.",
        },
        "video_url": outputs.text(
            "Temporary public fal CDN URL, present only after a successful result; expires within 24 hours."
        ),
        "seed": outputs.integer("Base seed returned by fal when the route reports one."),
        "inference_seconds": outputs.number("fal-reported model inference time in seconds."),
    },
    ["message", "task_id", "task_status", "generation_mode"],
)


def _url_array_schema(description: str) -> JSONObject:
    return {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": MAX_REFERENCE_FILES,
        "description": description,
    }


MANIFEST = ToolManifest(
    tool_id="h3max",
    display_name="H3 Max Video Generation",
    description=(
        "Connect fal and let your agent generate fal's H3 Max native-audio video from text, "
        "first/last frames, or multimodal references."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="generate_video",
            description=(
                "Start an async H3 Max generation. With no media it uses text-to-video; image_url "
                "selects image-to-video and optional end_image_url adds a last keyframe; any "
                "reference_*_urls list selects reference-to-video. Returns a task_id to poll. "
                "Clips include synchronized audio, run 5-15 seconds, and currently cost $0.05/s "
                "at 480P or $0.08/s at 768P, plus reference-input charges above fal's allowance."
            ),
            data_policy=GENERATE_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Visual, motion, dialogue, and sound direction for the clip (up to 1000 chars). "
                            "For reference mode, name files as Image 1, Video 1, Audio 1, and so on."
                        ),
                    },
                    "image_url": {
                        "type": "string",
                        "description": "Optional public HTTPS opening-frame image; selects image-to-video.",
                    },
                    "end_image_url": {
                        "type": "string",
                        "description": "Optional public HTTPS final-frame image; requires image_url.",
                    },
                    "reference_image_urls": _url_array_schema(
                        "Public HTTPS subject/style image URLs for reference mode, ordered as Image 1, Image 2, and so on."
                    ),
                    "reference_video_urls": _url_array_schema(
                        "Public HTTPS motion-reference video URLs for reference mode, ordered as Video 1, Video 2, and so on. Each must be 2-15 seconds; combined video length at most 15 seconds."
                    ),
                    "reference_audio_urls": _url_array_schema(
                        "Public HTTPS audio-reference URLs for reference mode, ordered as Audio 1, Audio 2, and so on. Each must be 2-15 seconds; combined audio length at most 15 seconds and audio cannot be the only reference type."
                    ),
                    "resolution": {
                        "type": "string",
                        "enum": list(RESOLUTIONS),
                        "description": "Native output resolution: 768P (default, $0.08/s) or 480P ($0.05/s).",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": list(REFERENCE_RATIOS),
                        "description": (
                            "Text/reference output ratio. Text defaults 16:9; reference defaults adaptive. "
                            "Image-to-video always follows image_url and rejects this field."
                        ),
                    },
                    "duration_seconds": {
                        "type": "string",
                        "description": "Video length in seconds, 5-15 (default 5); billing scales per second.",
                    },
                    "prompt_expansion_mode": {
                        "type": "string",
                        "enum": list(PROMPT_EXPANSION_MODES),
                        "description": "balanced (default, about 1s) or quality (may spend up to about 30s rewriting the prompt).",
                    },
                    "seed": {
                        "type": "string",
                        "description": "Optional unsigned 32-bit integer seed; prompt expansion can still vary the final render.",
                    },
                },
                "additionalProperties": False,
            },
            output_schema=GENERATE_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_task",
            description=(
                "Poll a task_id returned by generate_video. A successful task returns a public fal CDN "
                "video_url that expires within 24 hours; save it promptly if it should persist."
            ),
            data_policy=POLL_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Kern H3 Max task id returned by generate_video.",
                    }
                },
                "additionalProperties": False,
            },
            output_schema=GET_TASK_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="save_video",
            description=(
                "Save a completed H3 Max video under /tool_assets before its public fal CDN URL expires. "
                "The agent-side bridge creates the filename and returns the durable path."
            ),
            data_policy=SAVE_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Completed Kern H3 Max task id returned by generate_video.",
                    }
                },
                "additionalProperties": False,
            },
            returns_asset=True,
        ),
    ),
    config=(
        ConfigRequirement(
            key="H3MAX_FAL_KEY",
            description="API-scoped key from the fal dashboard (fal.ai/dashboard/keys).",
        ),
    ),
    protections=(
        "Your fal key stays in write-only tool config. H3 Max's model endpoints, media lifetime, "
        "and resolution/duration ceilings are pinned in code rather than chosen by the agent.",
        "fal's optional content safety checker is disabled for this integration. The operator and agent "
        "remain responsible for prompts, reference media, generated content, and compliance with fal's terms.",
        "fal normally stores request JSON for 30 days. Kern sends X-Fal-Store-IO: 0 on generation "
        "and polling so prompts and reference URLs do not appear in fal request history, and caps the "
        "public generated-media URL at 24 hours.",
        "Generation is billed to your fal account and never publishes the result. Saving is a separate "
        "read-only handoff into the private agent workspace; any later social publish remains separately approval-gated.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        PARAM_GUARD_TECHNICAL_DETAIL,
        "The tool calls fal's persistent queue with no webhooks. Its opaque task id encodes one of three "
        "fixed H3 Max routes plus a UUID; task polling cannot select another fal model or URL. sync_mode "
        "is forced off so large base64 video cannot enter model context, and enable_safety_checker is always false.",
    ),
    setup_steps=(
        SetupStep(
            title="Create and fund a fal account",
            description=(
                "Sign in to fal and add billing credit to the personal or team account that should own "
                "H3 Max usage. At current published pricing, output is billed per second and multimodal "
                "reference inputs can add token-based charges above the included allowance."
            ),
            link_url="https://fal.ai/dashboard/billing",
            link_label="Open fal billing",
        ),
        SetupStep(
            title="Create an API-scoped key",
            description=(
                "In the fal keys dashboard, select the intended personal or team account and create an API "
                "scope key (ADMIN is unnecessary). Copy it immediately; fal does not show the secret again."
            ),
            link_url="https://fal.ai/dashboard/keys",
            link_label="Open fal API keys",
        ),
        SetupStep(
            title="Configure and enable H3 Max",
            show_config=True,
            description=(
                "Open H3 Max Video Generation under Home > Integrations, save the key as H3MAX_FAL_KEY, "
                "then enable the tool. Never put the key in a prompt or reference URL."
            ),
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(
                        label="Generation request",
                        text=(
                            "fal receives the prompt; duration, resolution, ratio, expansion mode, and optional "
                            "seed; plus every public first/last-frame or multimodal reference URL. The prompt and "
                            "each complete URL first pass Kern's parameter guard, which denies secret-, credential-, "
                            "and high-risk-identifier-shaped values before transmission."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Reference media",
                        text=(
                            "Kern sends URLs, not workspace bytes. fal fetches the referenced image, video, or audio "
                            "from its existing public host, so the media and anything encoded in the URL path/query "
                            "become available to fal. Kern does not fetch or inspect those files itself."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(
                        label="fal-hosted model",
                        text=(
                            "Requests go to fal's Model API and CDN. fal says H3 Max is its post-trained variant of "
                            "open-weight MiniMax H3 and that fal hosts and runs the inference; this integration does "
                            "not call MiniMax's hosted API."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Service providers",
                        text=(
                            "fal's privacy policy says it can use vendors and service providers including GPU and web "
                            "hosting, infrastructure, security, analytics, and service monitoring. fal does not publish "
                            "a model-specific processing region for this endpoint."
                        ),
                    ),
                ),
                links=(
                    DataSummaryLink(
                        label="fal privacy policy", url="https://fal.ai/legal/privacy-policy"
                    ),
                    DataSummaryLink(
                        label="H3 Max model guide",
                        url="https://fal.ai/learn/tools/how-to-use-minimax-h3-max",
                    ),
                ),
            ),
            DataSummaryCard(
                title="What fal can do with it",
                description=(
                    "fal's API Services terms say it will not use client content to create, train, or develop its "
                    "products or services. Its general terms separately allow anonymized or aggregated Usage Data, "
                    "which may be derived from customer input, for analytics, service improvement, and product/model "
                    "development. fal processes inputs to provide generation, but Kern disables fal's optional content "
                    "safety checker for this integration. You keep rights in your input; fal does not promise generated "
                    "output is unique, original, non-infringing, or safe."
                ),
                links=(
                    DataSummaryLink(
                        label="fal API Services terms", url="https://fal.ai/legal/api-services"
                    ),
                    DataSummaryLink(
                        label="fal terms of service", url="https://fal.ai/legal/terms-of-service"
                    ),
                    DataSummaryLink(
                        label="fal acceptable use policy", url="https://fal.ai/legal/acceptable-use-policy"
                    ),
                ),
            ),
            DataSummaryCard(
                title="How long fal retains it",
                description=(
                    "fal normally retains request input/output JSON for 30 days, but Kern opts every H3 Max request "
                    "out of that storage with X-Fal-Store-IO: 0. The persistent queue still holds what it needs while "
                    "the task runs. Kern sets generated media to expire from fal's public CDN after 24 hours; expired "
                    "files are permanently deleted. A video saved into /tool_assets is then retained in the private "
                    "agent workspace independently of fal until the operator removes it."
                ),
                links=(
                    DataSummaryLink(
                        label="fal data retention and storage",
                        url="https://fal.ai/docs/documentation/model-apis/media-expiration",
                    ),
                ),
            ),
        )
    ),
    agent_notes=(
        "Use one reference mode at a time: image_url/end_image_url for keyframes, or reference_*_urls "
        "for identity/style/motion/audio conditioning. Poll the returned task_id unchanged. H3 Max always "
        "generates audio, so put dialogue, effects, ambience, music, or 'no music' directly in the prompt."
    ),
)


def _prompt(tool_input: JSONObject, api: HostAPI) -> str:
    value = tool_input.get("prompt")
    if not isinstance(value, str) or not value.strip():
        raise ToolInputValidationError("H3 Max tool_input.prompt is required.")
    value = value.strip()
    if len(value) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(
            f"H3 Max prompt must be at most {MAX_PROMPT_CHARS} characters."
        )
    return api.outbound.guard_request_parameter_string(value)


def _choice(
    tool_input: JSONObject, key: str, allowed: tuple[str, ...], default: str
) -> str:
    value = tool_input.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(
            f"H3 Max tool_input.{key} must be one of {', '.join(allowed)}."
        )
    return value


def _unsigned_integer(
    tool_input: JSONObject,
    key: str,
    *,
    default: int | None,
    low: int,
    high: int,
) -> int | None:
    value = tool_input.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 10:
            raise ToolInputValidationError(
                f"H3 Max tool_input.{key} must be between {low} and {high}."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolInputValidationError(
            f"H3 Max tool_input.{key} must be between {low} and {high}."
        )
    return value


def _guarded_url(tool_input: JSONObject, key: str, api: HostAPI) -> str:
    url = provider_fetched_https_url(tool_input, key, api, provider="H3 Max")
    # The first pass covers what is literally on the wire; decoded path/query
    # views cover values a downstream fetcher may unwrap one or more times.
    return guard_url_parameter_string(url, api)


def _guarded_url_list(tool_input: JSONObject, key: str, api: HostAPI) -> list[str]:
    value = tool_input.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ToolInputValidationError(f"H3 Max tool_input.{key} must be a list of HTTPS URLs.")
    if len(value) > MAX_REFERENCE_FILES:
        raise ToolInputValidationError(
            f"H3 Max tool_input.{key} must contain at most {MAX_REFERENCE_FILES} URLs."
        )
    guarded: list[str] = []
    for item in value:
        guarded.append(_guarded_url({key: item}, key, api))
    return guarded


def _generation_request(api: HostAPI, tool_input: JSONObject) -> tuple[str, JSONObject]:
    allowed = {
        "prompt",
        "image_url",
        "end_image_url",
        "reference_image_urls",
        "reference_video_urls",
        "reference_audio_urls",
        "resolution",
        "aspect_ratio",
        "duration_seconds",
        "prompt_expansion_mode",
        "seed",
    }
    if set(tool_input) - allowed:
        raise ToolInputValidationError("H3 Max generate_video received an unsupported field.")

    body: JSONObject = {
        "prompt": _prompt(tool_input, api),
        "duration": cast(
            int,
            _unsigned_integer(
                tool_input,
                "duration_seconds",
                default=DEFAULT_DURATION_SECONDS,
                low=MIN_DURATION_SECONDS,
                high=MAX_DURATION_SECONDS,
            ),
        ),
        "resolution": _choice(
            tool_input, "resolution", RESOLUTIONS, DEFAULT_RESOLUTION
        ),
        "prompt_expansion_mode": _choice(
            tool_input,
            "prompt_expansion_mode",
            PROMPT_EXPANSION_MODES,
            DEFAULT_PROMPT_EXPANSION_MODE,
        ),
        # Large base64 output must never enter active model context. The optional
        # provider-side content safety checker is deliberately disabled by policy.
        "enable_safety_checker": False,
        "sync_mode": False,
    }
    seed = _unsigned_integer(tool_input, "seed", default=None, low=0, high=4_294_967_295)
    if seed is not None:
        body["seed"] = seed

    reference_keys = (
        "reference_image_urls",
        "reference_video_urls",
        "reference_audio_urls",
    )
    reference_supplied = any(key in tool_input for key in reference_keys)
    keyframe_supplied = "image_url" in tool_input or "end_image_url" in tool_input
    if reference_supplied and keyframe_supplied:
        raise ToolInputValidationError(
            "H3 Max keyframe URLs and reference_*_urls cannot be used in the same request."
        )

    if reference_supplied:
        images = _guarded_url_list(tool_input, "reference_image_urls", api)
        videos = _guarded_url_list(tool_input, "reference_video_urls", api)
        audio = _guarded_url_list(tool_input, "reference_audio_urls", api)
        count = len(images) + len(videos) + len(audio)
        if count == 0:
            raise ToolInputValidationError("H3 Max reference mode requires at least one reference URL.")
        if count > MAX_REFERENCE_FILES:
            raise ToolInputValidationError(
                f"H3 Max accepts at most {MAX_REFERENCE_FILES} reference files in total."
            )
        if audio and not (images or videos):
            raise ToolInputValidationError(
                "H3 Max reference audio requires at least one reference image or video."
            )
        if images:
            body["reference_image_urls"] = cast(list[JSONValue], images)
        if videos:
            body["reference_video_urls"] = cast(list[JSONValue], videos)
        if audio:
            body["reference_audio_urls"] = cast(list[JSONValue], audio)
        body["aspect_ratio"] = _choice(
            tool_input,
            "aspect_ratio",
            REFERENCE_RATIOS,
            DEFAULT_REFERENCE_RATIO,
        )
        return "reference", body

    if keyframe_supplied:
        if "image_url" not in tool_input:
            raise ToolInputValidationError("H3 Max end_image_url requires image_url.")
        if "aspect_ratio" in tool_input:
            raise ToolInputValidationError(
                "H3 Max image-to-video follows image_url and does not accept aspect_ratio."
            )
        body["image_url"] = _guarded_url(tool_input, "image_url", api)
        if "end_image_url" in tool_input:
            body["end_image_url"] = _guarded_url(tool_input, "end_image_url", api)
        return "image", body

    body["aspect_ratio"] = _choice(
        tool_input, "aspect_ratio", TEXT_RATIOS, DEFAULT_TEXT_RATIO
    )
    return "text", body


def _headers(api_key: str) -> dict[str, str]:
    return {
        "authorization": f"Key {api_key}",
        "X-Fal-Store-IO": "0",
        "X-Fal-Object-Lifecycle-Preference": OBJECT_LIFECYCLE_HEADER,
    }


def _endpoint(mode: str) -> str:
    return f"{QUEUE_BASE}/{ROUTES[mode]}"


def _task_parts(tool_input: JSONObject) -> tuple[str, str, str]:
    if set(tool_input) != {"task_id"}:
        raise ToolInputValidationError("H3 Max action requires exactly one task_id.")
    value = tool_input.get("task_id")
    if not isinstance(value, str):
        raise ToolInputValidationError("H3 Max tool_input.task_id must be a valid task id.")
    match = TASK_ID_RE.fullmatch(value.strip())
    if match is None:
        raise ToolInputValidationError("H3 Max tool_input.task_id must be a valid task id.")
    mode, request_id = match.groups()
    return value.strip(), mode, request_id


def _task_url(mode: str, request_id: str, suffix: str = "") -> str:
    encoded = urllib.parse.quote(request_id, safe="")
    return f"{_endpoint(mode)}/requests/{encoded}{suffix}"


def _status(mode: str, request_id: str, headers: dict[str, str]) -> JSONObject:
    return json_request(
        "GET",
        _task_url(mode, request_id, "/status"),
        headers=headers,
        failure_message="fal queue status request failed.",
        invalid_response_message="fal queue returned an invalid status response.",
    )


def _result(mode: str, request_id: str, headers: dict[str, str]) -> JSONObject:
    return json_request(
        "GET",
        _task_url(mode, request_id),
        headers=headers,
        failure_message="fal result request failed.",
        invalid_response_message="fal returned an invalid H3 Max result.",
    )


def _video_url(response: JSONObject) -> str:
    video = response.get("video")
    if not isinstance(video, dict):
        return ""
    value = video.get("url")
    return value if isinstance(value, str) and is_public_https_url(value) else ""


def _safe_seed(response: JSONObject) -> int | None:
    value = response.get("seed")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4_294_967_295:
        return value
    return None


def _inference_seconds(response: JSONObject) -> float | None:
    timings = response.get("timings")
    value = timings.get("inference") if isinstance(timings, dict) else None
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def _failed_task_result(task_id: str, mode: str, status: JSONObject) -> JSONObject:
    raw_error_type = status.get("error_type")
    code = (
        f" (type: {raw_error_type})"
        if isinstance(raw_error_type, str) and ERROR_TYPE_RE.fullmatch(raw_error_type)
        else ""
    )
    return {
        "message": (
            f"H3 Max generation failed{code}. Content safety and unsupported reference media can "
            "both cause this; adjust the inputs and submit a new task."
        ),
        "task_id": task_id,
        "task_status": "failed",
        "generation_mode": mode,
    }


def _task_result(
    task_id: str, mode: str, request_id: str, headers: dict[str, str]
) -> JSONObject:
    status_response = _status(mode, request_id, headers)
    raw_status = status_response.get("status")
    status = raw_status.strip().upper() if isinstance(raw_status, str) else ""
    if status not in QUEUE_STATUSES:
        return {
            "message": "fal returned an unrecognized task status. Poll get_task again shortly.",
            "task_id": task_id,
            "task_status": "unknown",
            "generation_mode": mode,
        }
    if status == "IN_QUEUE":
        return {
            "message": "H3 Max task is queued. Poll get_task again shortly.",
            "task_id": task_id,
            "task_status": "queued",
            "generation_mode": mode,
        }
    if status == "IN_PROGRESS":
        return {
            "message": "H3 Max task is running. Poll get_task again shortly.",
            "task_id": task_id,
            "task_status": "running",
            "generation_mode": mode,
        }
    if status_response.get("error") is not None or status_response.get("error_type") is not None:
        return _failed_task_result(task_id, mode, status_response)

    response = _result(mode, request_id, headers)
    url = _video_url(response)
    result: JSONObject = {
        "task_id": task_id,
        "generation_mode": mode,
    }
    if not url:
        result.update(
            {
                "message": "fal reported completion but returned no valid H3 Max video URL. Submit a new task.",
                "task_status": "failed",
            }
        )
        return result
    result.update(
        {
            "message": (
                "Generation succeeded. The video_url is public and expires within 24 hours; "
                "call save_video promptly if it should persist."
            ),
            "task_status": "succeeded",
            "video_url": url,
        }
    )
    seed = _safe_seed(response)
    if seed is not None:
        result["seed"] = seed
    inference = _inference_seconds(response)
    if inference is not None:
        result["inference_seconds"] = inference
    return result


def _failure_from_status(exc: WebRequestError, *, creating: bool = False) -> str:
    if exc.status == 401:
        return "fal rejected the configured API key."
    if exc.status == 402:
        return "fal account credit is insufficient for H3 Max generation."
    if exc.status == 403:
        return "fal denied the request. The API key may lack model access or the request may violate fal policy."
    if exc.status == 404:
        return "fal did not find the H3 Max model endpoint." if creating else "fal H3 Max task was not found."
    if exc.status == 429:
        return "fal rate or concurrency limit was reached."
    if exc.status in {400, 409, 422}:
        return (
            "fal rejected the H3 Max request. Check its duration, resolution, reference-media limits, "
            "and whether the referenced public files are reachable and supported."
        )
    if exc.status:
        return f"fal API returned HTTP {exc.status}."
    message = known_provider_transport_error(exc)
    if not message:
        raise unmapped_provider_error("fal", "H3 Max API", exc) from None
    return message


def _download_failure(exc: WebRequestError) -> str:
    if exc.status in {401, 403, 404}:
        return "fal's generated video is no longer available; its public CDN URL may have expired."
    if exc.status:
        return f"H3 Max video download failed with HTTP {exc.status}."
    message = known_provider_transport_error(exc)
    if not message:
        raise unmapped_provider_error("fal", "H3 Max video download", exc) from None
    return message


def _save_video(
    task_id: str, mode: str, request_id: str, headers: dict[str, str]
) -> ActionResult:
    task = _task_result(task_id, mode, request_id, headers)
    if task.get("task_status") != "succeeded":
        return ActionFailed(str(task.get("message") or "H3 Max video is not complete."))
    url = task.get("video_url")
    if not isinstance(url, str):
        return ActionFailed("fal reported success but returned no valid H3 Max video URL.")

    def open_video():
        return open_downloaded_video(
            url,
            provider="H3 Max",
            filename_stem=f"h3max-{request_id}",
            map_failure=_download_failure,
        )

    return StreamingAsset(open_video)


class H3MaxTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            api_key = api.config["H3MAX_FAL_KEY"]
            headers = _headers(api_key)
            if action == "generate_video":
                mode, body = _generation_request(api, tool_input)
                response = json_request(
                    "POST",
                    _endpoint(mode),
                    headers=headers,
                    body=body,
                    failure_message="fal H3 Max queue submission failed.",
                    invalid_response_message="fal returned an invalid queue submission response.",
                )
                request_id = response.get("request_id")
                if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                    return ActionFailed("fal returned no valid H3 Max request id.")
                task_id = f"{mode}_{request_id}"
                return ActionExecuted(
                    {
                        "message": "H3 Max task queued. Poll get_task until it succeeds.",
                        "task_id": task_id,
                        "task_status": "queued",
                        "generation_mode": mode,
                        "model": ROUTES[mode],
                        "output_kind": "video",
                    }
                )
            if action == "get_task":
                task_id, mode, request_id = _task_parts(tool_input)
                return ActionExecuted(_task_result(task_id, mode, request_id, headers))
            if action == "save_video":
                task_id, mode, request_id = _task_parts(tool_input)
                return _save_video(task_id, mode, request_id, headers)
            return ActionFailed("Unsupported H3 Max action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            return ActionFailed(_failure_from_status(exc, creating=action == "generate_video"))
        except (ValueError, RuntimeError) as exc:
            return ActionFailed(str(exc) or "H3 Max tool request failed.")
        except Exception:
            return ActionFailed("H3 Max tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("H3 Max has no approval-gated actions.")


BUNDLED_TOOL = H3MaxTool()
