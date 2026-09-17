"""Runway media generation tool package (Runway Developer API)."""

from __future__ import annotations

import re
import secrets
import urllib.parse
from typing import BinaryIO, Iterator, cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.json_types import JSONObject
from host.tools.manifest import protect_inputs, guarded_input, validated_input, ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionResult,
    ApprovalResult,
    StreamingAsset,
)
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.runway import options
from host.tools.shared import outputs
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.media import open_downloaded_audio, open_downloaded_video
from host.tools.shared.web import (
    UnmappedProviderError,
    WebRequestError,
    is_public_https_url,
    json_request,
    known_provider_transport_error,
    stream_request_bytes,
    unmapped_provider_error,
)

# Runway's Developer API is a single Bearer-authenticated JSON surface. Every
# generation is an async task: POST a generation endpoint to get a task id, then
# poll GET /v1/tasks/{id} until a terminal status. One pinned API version rides
# on every request as a header.
RUNWAY_API_BASE = "https://api.dev.runwayml.com"
RUNWAY_API_VERSION = "2024-11-06"
TEXT_TO_VIDEO_ENDPOINT = f"{RUNWAY_API_BASE}/v1/text_to_video"
IMAGE_TO_VIDEO_ENDPOINT = f"{RUNWAY_API_BASE}/v1/image_to_video"
VIDEO_TO_VIDEO_ENDPOINT = f"{RUNWAY_API_BASE}/v1/video_to_video"
TEXT_TO_IMAGE_ENDPOINT = f"{RUNWAY_API_BASE}/v1/text_to_image"
TEXT_TO_SPEECH_ENDPOINT = f"{RUNWAY_API_BASE}/v1/text_to_speech"
TASKS_ENDPOINT = f"{RUNWAY_API_BASE}/v1/tasks"
UPLOADS_ENDPOINT = f"{RUNWAY_API_BASE}/v1/uploads"

MAX_PROMPT_CHARS = 1_000

# Video generation models Runway exposes on text_to_video / image_to_video.
# Runway is now a model aggregator, so this spans first-party Gen-4 models,
# Google Veo, ByteDance Seedance, and fal's MiniMax H3 Max variant.
# gen4_turbo is image-to-video only.
SUPPORTED_VIDEO_MODELS = (
    "gen4.5",
    "gen4_turbo",
    "veo3.1",
    "veo3.1_fast",
    "seedance2",
    "seedance2_fast",
    "seedance2_5",
    "h3_max",
)
IMAGE_ONLY_VIDEO_MODELS = frozenset({"gen4_turbo"})
DEFAULT_TEXT_MODEL = "gen4.5"
DEFAULT_IMAGE_MODEL = "gen4_turbo"

# The video-editing model (Aleph 2) drives the video_to_video endpoint; it edits
# an existing video from an instruction prompt rather than generating from
# scratch.
EDIT_MODEL = "aleph2"
IMAGE_MODELS = ("gpt_image_2_5_sunburst", "gpt_image_2_5_flare")
DEFAULT_IMAGE_GENERATION_MODEL = "gpt_image_2_5_sunburst"
SPEECH_MODELS = ("eleven_multilingual_v2", "eleven_v3")
DEFAULT_SPEECH_MODEL = "eleven_multilingual_v2"

IMAGE_RATIOS = ("1920:1920", "1920:1280", "1280:1920")
DEFAULT_IMAGE_RATIO = "1920:1920"
IMAGE_QUALITIES = ("low", "medium", "high", "xhigh", "max")
DEFAULT_IMAGE_QUALITY = "low"
SPEECH_VOICES = (
    "Maya", "Arjun", "Serene", "Bernard", "Billy", "Mark", "Clint", "Mabel",
    "Chad", "Leslie", "Eleanor", "Elias", "Elliot", "Grungle", "Brodie", "Sandra",
    "Kirk", "Kylie", "Lara", "Lisa", "Malachi", "Marlene", "Martin", "Miriam",
    "Monster", "Paula", "Pip", "Rusty", "Ragnar", "Xylar", "Maggie", "Jack",
    "Katie", "Noah", "James", "Rina", "Ella", "Mariah", "Frank", "Claudia", "Niki",
    "Vincent", "Kendrick", "Myrna", "Tom", "Wanda", "Benjamin", "Kiana", "Rachel",
)
DEFAULT_SPEECH_VOICE = "Maya"

# Keep the common landscape/portrait ratios for models that accept a ratio.
# H3 Max instead accepts resolution and follows its first-frame image's aspect.
SUPPORTED_RATIOS = (
    "1280:720",
    "720:1280",
)
DEFAULT_RATIO = "1280:720"
DEFAULT_DURATION_SECONDS = 5
VEO_DURATION_SECONDS = frozenset({4, 6, 8})
VIDEO_DURATION_RANGES = {
    "gen4.5": (2, 10),
    "gen4_turbo": (2, 10),
    "seedance2": (4, 15),
    "seedance2_fast": (4, 15),
    "seedance2_5": (4, 30),
    "h3_max": (5, 15),
}

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

RUNWAY_GENERATE_POLICY = (
    "The prompt, generation settings, and optional image/video/audio inputs supplied by the user or "
    "agent are sent to Runway's Developer API to render a video, billed as "
    "credits against the deployment's Runway organization. This action runs "
    "directly with no approval; it publishes nothing anywhere, and the result is a "
    "task id returned to active model context to poll with get_task."
)
RUNWAY_EDIT_POLICY = (
    "The source video, optional guidance keyframes, editing prompt, and output settings supplied by the user or "
    "agent are sent to Runway's Developer API (Aleph 2) to render an "
    "edited video, billed as credits against the deployment's Runway "
    "organization. This action runs directly with no approval; it publishes "
    "nothing anywhere; the result is a task id returned to active model context to poll with get_task."
)
RUNWAY_IMAGE_POLICY = (
    "The image prompt and rendering parameters supplied by the user or agent are sent "
    "to Runway's Developer API and forwarded by Runway to OpenAI's GPT Image 2.5 Sunburst or Flare. The generation is billed as "
    "Runway credits. This action runs directly with no approval and publishes nothing; "
    "it returns a task id to active model context to poll with get_task."
)
RUNWAY_SPEECH_POLICY = (
    "The speech text, selected Runway voice preset, and optional delivery settings supplied by the user or agent are "
    "sent to Runway's Developer API and forwarded by Runway to ElevenLabs Multilingual v2 or Eleven v3. The generation "
    "is billed as Runway credits. This action runs directly with no approval and publishes "
    "nothing; it returns a task id to active model context to poll with get_task."
)
RUNWAY_POLL_POLICY = (
    "Read-only poll. Sends only the task id to Runway's Developer API and "
    "returns the task status and, once finished, a temporary download URL for "
    "the generated video, image, or audio into active model context. Runs directly with no approval."
)
RUNWAY_SAVE_VIDEO_POLICY = (
    "Read-only handoff. Sends the task id to Runway, downloads the completed video from "
    "Runway's authoritative temporary output URL, and streams it through the agent-side "
    "bridge into a host-generated path under /tool_assets in the agent workspace."
)

RUNWAY_SAVE_AUDIO_POLICY = (
    "Read-only handoff. Sends the task id to Runway, downloads completed MP3 speech from "
    "Runway's authoritative temporary output URL, and streams it through the agent-side "
    "bridge into a host-generated path under /tool_assets in the agent workspace."
)


# Runway generation is asynchronous: creating a task returns its id, and
# get_task returns whatever Runway knows so far. The output URL appears only on
# the poll that finds the task SUCCEEDED, under the key matching output_kind.
TASK_CREATED_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("Confirmation that the task was created, and which output_kind to poll with."),
        "task_id": outputs.text("Runway task id; pass to get_task."),
        "task_status": outputs.text("Always PENDING for a task this call just created."),
        "model": outputs.text("Runway model the task runs on."),
        "output_kind": outputs.text("video, image, or audio; pass the same value to get_task."),
    },
    ["message", "task_id", "task_status", "model", "output_kind"],
)
GET_TASK_OUTPUT_SCHEMA: JSONObject = outputs.obj(
    {
        "message": outputs.text("What the task is doing and what to do next, including progress while it runs."),
        "task_id": outputs.text("The task that was polled."),
        "task_status": outputs.text("Runway's status: PENDING, RUNNING, THROTTLED, SUCCEEDED, FAILED, or CANCELLED. \"unknown\" when Runway sent no status."),
        "video_url": outputs.text("Temporary video URL, present only on a SUCCEEDED video task."),
        "image_url": outputs.text("Temporary image URL, present only on a SUCCEEDED image task."),
        "audio_url": outputs.text("Temporary audio URL, present only on a SUCCEEDED audio task."),
        "output_urls": {"type": "array", "items": {"type": "string"}, "description": "All temporary output URLs on success, including ZIP sequences and separate audio/metadata artifacts. save_video handles MP4/MOV only."},
    },
    ["message", "task_id", "task_status"],
)


MANIFEST = ToolManifest(
    tool_id="runway",
    display_name="Runway Media Generation",
    description="Connect Runway and let your agent generate images, speech, and short videos, and edit videos.",
    connection="enable_only",
    actions=protect_inputs((
        ActionSpec(
            id="generate_video",
            description=(
                "Start an async Runway video generation task from a text prompt and optional "
                "media from public URLs, existing Runway uploads, or the agent workspace. Supports keyframes and model-specific references. Returns a "
                "task_id to poll with get_task; renders "
                "typically take one to three minutes. This runs immediately, spends Runway "
                "credits, and creates no public post."
            ),
            data_policy=RUNWAY_GENERATE_POLICY,
            input_schema={
                "type": "object",
                "required": [],
                "properties": {
                    "prompt": {"type": "string", "description": "What to render. Gen-4/Veo: 1000 UTF-16 chars; Seedance 2.0/Fast: 3500; 2.5: 15000. Uses allow_longer_text: up to 5 KB (5120 UTF-8 bytes), subject to the model limit. Required for text-only routes except Seedance 2.5, and always for Gen-4.5/H3 Max."},
                    "model": {
                        "type": "string",
                        "enum": list(SUPPORTED_VIDEO_MODELS),
                        "description": "Default: gen4.5 (or gen4_turbo with image inputs). gen4_turbo is image-to-video only. Select the model explicitly to follow operator preferences.",
                    },
                    "image_url": {"type": "string", "description": "Optional public HTTPS image URL used as the first frame (image-to-video)."},
                    "image_asset_id": {"type": "string", "description": "Built-in reference for a JPEG, PNG, or WebP from the agent workspace. Use at most one of image_url or image_asset_id."},
                    "ratio": options.choice_schema(options.ALL_VIDEO_RATIOS, "Output dimensions, default 1280:720. Seedance 2.5: 480p/720p/1080p (portrait 480:854, 720:1280, 1080:1920); 2.0 also 4K; Fast only 480p/720p. Gen-4 text: landscape/portrait 720p; image also other listed Gen-4 shapes. Veo: portrait/landscape 720p or 1080p. Omit for h3_max; use resolution and a first frame for framing."),
                    "duration_seconds": {"type": "string", "description": "Gen-4: 2-10; Seedance 2.0/Fast: 4-15; Seedance 2.5: 4-30; H3 Max: 5-15 (default 5). Seedance also accepts auto, billed at the maximum up front with unused credits refunded. Veo: 4, 6, or 8 (default 4)."},
                    "seed": {"type": "string", "description": "Optional integer seed. H3 Max prompt_expansion_mode=disabled is also needed for repeatability."},
                    **options.VIDEO_PROPERTIES,
                },
                "additionalProperties": False,
            },
            output_schema=TASK_CREATED_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="edit_video",
            description=(
                "Start an async Runway video-editing task (Aleph 2): restyle or modify an "
                "existing video from a public HTTPS URL or the agent workspace. Returns a "
                "task_id to poll with get_task. A workspace video is uploaded to Runway only "
                "when this action runs. This spends Runway credits "
                "and creates no public post."
            ),
            data_policy=RUNWAY_EDIT_POLICY,
            input_schema={
                "type": "object",
                "required": [],
                "properties": {
                    "video_url": {"type": "string", "description": "Public HTTPS URL of the source video to edit."},
                    "video_asset_id": {"type": "string", "description": "Built-in reference for an MP4 or MOV from the agent workspace. Use exactly one of video_url or video_asset_id."},
                    "prompt": {"type": "string", "description": "Optional editing instruction, e.g. 'make it night time'. Uses allow_longer_text: up to 5 KB (5120 UTF-8 bytes)."},
                    "seed": {"type": "string", "description": "Optional integer seed for reproducible output."},
                    **options.EDIT_PROPERTIES,
                },
                "additionalProperties": False,
            },
            output_schema=TASK_CREATED_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="generate_image",
            description=(
                "Start an async GPT Image 2.5 text-to-image task through Runway and return a "
                "task_id. Poll get_task with output_kind=image for the temporary image URL. "
                "This runs immediately, spends Runway credits, and publishes nothing."
            ),
            data_policy=RUNWAY_IMAGE_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "What to render (up to 1000 chars). Uses allow_longer_text: a 5 KB (5120 UTF-8 bytes) guard ceiling; the model character limit still applies."},
                    "model": {"type": "string", "enum": list(IMAGE_MODELS), "description": "Default gpt_image_2_5_sunburst; choose gpt_image_2_5_flare for faster everyday generation."},
                    "ratio": {"type": "string", "enum": list(IMAGE_RATIOS), "description": "Output resolution: square 1920:1920 (default), landscape 1920:1280, or portrait 1280:1920."},
                    "quality": {"type": "string", "enum": list(IMAGE_QUALITIES), "description": "Rendering quality (default low); higher quality spends more Runway credits."},
                },
                "additionalProperties": False,
            },
            output_schema=TASK_CREATED_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="generate_speech",
            description=(
                "Start an async ElevenLabs text-to-speech task through Runway. Select eleven_v3 "
                "for expressive audio tags and optional stability, style, and speed controls. "
                "Returns a task_id. Poll get_task with output_kind=audio for the temporary "
                "audio URL, then use save_audio to keep the completed MP3 in the workspace. "
                "This runs immediately, spends Runway credits, and publishes nothing."
            ),
            data_policy=RUNWAY_SPEECH_POLICY,
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Words to speak, up to 1000 characters. With eleven_v3, include delivery tags such as [whispers] or [laughs] in the script; tags are part of this limit."},
                    "model": {"type": "string", "enum": list(SPEECH_MODELS), "description": "Default eleven_multilingual_v2. Choose eleven_v3 for expressive delivery and audio tags."},
                    "stability": {"type": "number", "description": "Eleven v3 only, 0 to 1. Lower values allow more emotional variation; higher values are steadier. Omit for the provider default."},
                    "style": {"type": "number", "description": "Eleven v3 only, 0 to 1. Style exaggeration; higher values amplify the speaker style. Omit for the provider default."},
                    "speed": {"type": "number", "description": "Eleven v3 only, 0.7 to 1.2. Speech speed multiplier; 1 is normal. Omit for the provider default."},
                    "voice": {"type": "string", "enum": list(SPEECH_VOICES), "description": "Runway's ElevenLabs voice preset (default Maya)."},
                },
                "additionalProperties": False,
            },
            output_schema=TASK_CREATED_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_task",
            description="Poll a task_id returned by any Runway generation action. Set output_kind to the originating action's media type; pending tasks have no output, while success returns a temporary video, image, or audio URL valid about 24-48 hours.",
            data_policy=RUNWAY_POLL_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "Runway task id returned when generation or editing starts."},
                    "output_kind": {"type": "string", "enum": ["video", "image", "audio"], "description": "Originating action's output type (default video); controls whether success returns video_url, image_url, or audio_url."},
                },
                "additionalProperties": False,
            },
            output_schema=GET_TASK_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="save_video",
            description=(
                "Save a completed Runway video under /tool_assets in the agent workspace. "
                "The agent-side bridge creates the filename and returns the durable path."
            ),
            data_policy=RUNWAY_SAVE_VIDEO_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "Completed Runway video task id."},
                },
                "additionalProperties": False,
            },
            returns_asset=True,
        ),
        ActionSpec(
            id="save_audio",
            description=(
                "Save completed Runway MP3 speech under /tool_assets in the agent workspace. "
                "The agent-side bridge creates the filename and returns the durable path."
            ),
            data_policy=RUNWAY_SAVE_AUDIO_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "Completed Runway speech task id."},
                },
                "additionalProperties": False,
            },
            returns_asset=True,
        ),
    ), {
        "generate_video": {
            "prompt": guarded_input(allow_longer_text=True),
            "model": validated_input("One of the listed choices."),
            "image_url": guarded_input(),
            "image_asset_id": validated_input("Staged image reference; tool ownership, expiry and supported image format checked."),
            "ratio": validated_input("One of the listed choices."),
            **{key: (guarded_input() if key in {"prompt_images", "reference_images", "reference_videos", "reference_audio", "video_url", "negative_prompt"}
                     else validated_input("Validated for the selected model; workspace references also check media type."))
               for key in options.VIDEO_PROPERTIES},
            "duration_seconds": validated_input("Integer within the selected model’s documented duration range or fixed choices."),
            "seed": validated_input("Integer from 0 to 4294967295."),
        },
        "edit_video": {
            "video_url": guarded_input(),
            "video_asset_id": validated_input("Staged video reference; tool ownership, expiry and supported video format checked."),
            "prompt": guarded_input(allow_longer_text=True),
            **{key: (guarded_input() if key == "keyframes" else validated_input("Validated Aleph setting."))
               for key in options.EDIT_PROPERTIES},
            "seed": validated_input("Integer from 0 to 4294967295."),
        },
        "generate_image": {
            "prompt": guarded_input(allow_longer_text=True),
            "model": validated_input("One of the listed choices."),
            "ratio": validated_input("One of the listed choices."),
            "quality": validated_input("One of the listed choices."),
        },
        "generate_speech": {
            "model": validated_input("One of the listed choices."),
            "stability": validated_input("Number from 0 to 1; Eleven v3 only."),
            "style": validated_input("Number from 0 to 1; Eleven v3 only."),
            "speed": validated_input("Number from 0.7 to 1.2; Eleven v3 only."),
            "text": guarded_input(),
            "voice": validated_input("One of the listed choices."),
        },
        "get_task": {
            "task_id": validated_input("1–128 ASCII letters, digits, dots, underscores, colons or hyphens."),
            "output_kind": validated_input("One of the listed choices."),
        },
        "save_audio": {
            "task_id": validated_input("1–128 ASCII letters, digits, dots, underscores, colons or hyphens."),
        },
        "save_video": {
            "task_id": validated_input("1–128 ASCII letters, digits, dots, underscores, colons or hyphens."),
        },
    }),
    config=(ConfigRequirement(key="RUNWAY_API_SECRET", description="Runway Developer API key (org-scoped) from the dev.runwayml.com dashboard."),),
    protections=(
        "Your Runway key stays in write-only tool config. Inputs are bounded, and local images and videos are uploaded to Runway only when used as inputs.",
        "Generation is billed to your Runway organization. Kern does not publish the media. Completed video and MP3 speech can be saved from Runway's authoritative temporary URL into the agent workspace for durable operator review and later approval-gated publishing.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,),
    setup_steps=(
        SetupStep(
            title="Create a Runway developer account",
            description="Open dev.runwayml.com, sign in, and create or select the developer organization that should own all agent-generated media. Confirm the organization name before funding it; Developer API credits, keys, tasks, and billing are separate from Runway's consumer web-application plan and credits.",
            link_url="https://dev.runwayml.com/",
            link_label="Open the Runway developer portal",
        ),
        SetupStep(
            title="Fund the API organization and create a key",
            description="In the developer portal, add API credits to the selected organization, open its API Keys area, create a clearly named organization-scoped secret, and copy it immediately to a password manager or Kern. Generation, editing, GPT Image, and ElevenLabs speech actions spend the same Runway organization balance; web-app credits do not cover these calls.",
            link_url="https://docs.dev.runwayml.com/guides/using-the-api/",
            link_label="View Runway's API guide",
        ),
        SetupStep(
            title="Configure and enable Runway",
            show_config=True,
            description="Open Runway Media Generation under Home > Integrations, save the developer secret as RUNWAY_API_SECRET, then enable the tool. There is no OAuth or separate OpenAI/ElevenLabs key. Never place the Runway key in a prompt, source URL, or media filename.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Generation requests", text="The prompt or speech text, generation options (including model, dimensions, duration, audio, output format, quality, voice, and seed), and image/video/audio input URLs go to Runway. These free-text values (prompt, speech text, external media URL) first pass the host parameter guard (see Technical notes), which denies secret- or credential-shaped values before anything is sent."),
                    DataSummaryPoint(label="Workspace media", text="When an image or video file from the agent workspace is used as an input, its bytes and original filename upload to Runway. Its local workspace path is not sent. Audio references use public HTTPS URLs or existing Runway upload URIs; workspace audio staging is not available."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Runway models", text="Every request first goes to Runway. Gen-4.5, Gen-4 Turbo, and Aleph 2 generations use Runway's own models."),
                    DataSummaryPoint(label="Third-party video models", text="When the agent explicitly selects Google Veo 3.1, ByteDance Seedance 2.0/2.5, or fal's MiniMax H3 Max, Runway sends that provider the prompt, generation settings, and any supplied keyframes or reference images, videos, and audio. Kern does not let Runway silently choose one of these models."),
                    DataSummaryPoint(label="Image and speech models", text="For image generation, Runway sends the prompt, ratio, and quality to OpenAI's GPT Image 2.5 Sunburst or Flare. For speech generation, Runway sends the speech text, selected voice, and optional delivery settings to ElevenLabs Multilingual v2 or Eleven v3."),
                ),
            ),
            DataSummaryCard(
                title="What Runway can do with it",
                description=(
                    "Runway treats prompts and media as user content: its terms let it review Inputs and Outputs and use them to "
                    "train and improve Runway models under a broad license, alongside service operation, research, analytics, "
                    "vendor processing, safety, and legal uses. Ordinary API accounts have no self-service training opt-out. "
                    "Enterprise accounts use separate negotiated terms; Runway says its third-party model providers do not train "
                    "on Enterprise customer inputs or outputs, but the treatment of Runway's own models depends on that contract."
                ),
                links=(
                    DataSummaryLink(label="Runway privacy policy", url="https://runwayml.com/privacy-policy"),
                    DataSummaryLink(label="Runway terms of use", url="https://runwayml.com/terms-of-use"),
                    DataSummaryLink(label="Runway data security", url="https://runwayml.com/data-security"),
                    DataSummaryLink(label="Runway Enterprise third-party model policy", url="https://help.runwayml.com/hc/en-us/articles/51248305153683-Enterprise-FAQ-Third-party-Models-in-Runway"),
                ),
            ),
            DataSummaryCard(
                title="How long Runway retains it",
                description=(
                    "Runway keeps content as long as necessary for its stated purposes, with no fixed period. Generated output "
                    "URLs are temporary and typically expire within 24 to 48 hours."
                ),
                links=(
                    DataSummaryLink(label="Runway privacy policy", url="https://runwayml.com/privacy-policy"),
                ),
            ),
        ),
    ),
    agent_notes=(
        "Video inputs use shared {uri or asset_id} media objects. prompt_images adds position first/last; "
        "Seedance unpositioned images use reference mode. Choose Seedance resolution with ratio pixel dimensions; "
        "H3 Max uses resolution 480p/768p and no ratio. New model-specific options are omitted unless selected. "
        "Seedance video input uses generate_video; Aleph uses edit_video. Public URL media dimensions and "
        "combined durations are validated by Runway. Prompt fields use allow_longer_text (5 KB/5120 UTF-8 bytes); other free text/URLs keep the 1024-byte guard. "
        "Use staged workspace files instead of inline base64. get_task.output_urls returns every artifact; "
        "save_video handles MP4/MOV, not ZIP frame sequences. Non-MP4 output formats can cost extra."
    ),
)


def _duration_seconds(tool_input: JSONObject, model: str) -> int | str:
    value = tool_input.get("duration_seconds")
    if value == "auto" and model in options.SEEDANCE_MODELS:
        return "auto"
    if value is None:
        return 4 if model in {"veo3.1", "veo3.1_fast"} else DEFAULT_DURATION_SECONDS
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 2:
            raise ToolInputValidationError(
                "Runway tool_input.duration_seconds is outside the supported range."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError("Runway tool_input.duration_seconds must be an integer or digit string.")
    if model in {"veo3.1", "veo3.1_fast"}:
        if value not in VEO_DURATION_SECONDS:
            raise ToolInputValidationError("Runway Veo duration_seconds must be 4, 6, or 8.")
        return value
    low, high = VIDEO_DURATION_RANGES[model]
    if not low <= value <= high:
        raise ToolInputValidationError(
            f"Runway {model} duration_seconds must be between {low} and {high}."
        )
    return value


def _optional_seed(tool_input: JSONObject) -> int | None:
    value = tool_input.get("seed")
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        digits = value.strip()
        if len(digits.lstrip("-")) > 10:
            raise ToolInputValidationError(
                "Runway tool_input.seed must be between 0 and 4294967295."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError("Runway tool_input.seed must be an integer or digit string.")
    if not 0 <= value <= 4_294_967_295:
        raise ToolInputValidationError(
            "Runway tool_input.seed must be between 0 and 4294967295."
        )
    return value


def _string_choice(tool_input: JSONObject, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = tool_input.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(f"Runway tool_input.{key} must be one of {', '.join(allowed)}.")
    return value


def _prompt_text(tool_input: JSONObject, api: HostAPI) -> str:
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolInputValidationError("Runway tool_input.prompt is required.")
    prompt = prompt.strip()
    # Reject rather than truncate: a silently cut prompt would spend credits on a
    # render of an input the agent did not ask for.
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(f"Runway prompt must be at most {MAX_PROMPT_CHARS} characters.")
    return api.outbound.guard_request_parameter_string(prompt, allow_longer_text=True)


def _generation_request(
    api: HostAPI, tool_input: JSONObject, uploads: dict[str, str]
) -> tuple[str, JSONObject]:
    """Validate the entire request before uploading any workspace media."""
    allowed = {"prompt", "model", "image_url", "image_asset_id", "ratio", "duration_seconds", "seed", *options.VIDEO_PROPERTIES}
    if set(tool_input) - allowed:
        raise ToolInputValidationError("Runway generate_video received unsupported fields.")
    image_keys = [k for k in ("image_url", "image_asset_id", "prompt_images") if tool_input.get(k) is not None]
    if len(image_keys) > 1:
        raise ToolInputValidationError("Runway generate_video supports at most one of image_url, image_asset_id, or prompt_images.")
    video_keys = [k for k in ("video_url", "video_asset_id") if tool_input.get(k) is not None]
    if len(video_keys) > 1 or (video_keys and image_keys):
        raise ToolInputValidationError("Runway video input cannot be combined with image keyframes or a second video source.")
    has_image = bool(image_keys)
    model = _string_choice(tool_input, "model", SUPPORTED_VIDEO_MODELS, DEFAULT_IMAGE_MODEL if has_image else DEFAULT_TEXT_MODEL)
    seedance = model in options.SEEDANCE_MODELS
    veo = model in {"veo3.1", "veo3.1_fast"}
    if not has_image and model in IMAGE_ONLY_VIDEO_MODELS:
        raise ToolInputValidationError(f"Runway model {model} is image-to-video only; supply image_url, image_asset_id, or prompt_images.")
    if video_keys and not seedance:
        raise ToolInputValidationError("Runway generate_video video input requires a Seedance model; use edit_video for Aleph.")
    mode = None
    if "mode" in tool_input:
        if model != "seedance2_5" or not video_keys:
            raise ToolInputValidationError("Runway mode requires Seedance 2.5 video input.")
        mode = options.choice(tool_input["mode"], ("reference", "extend", "edit"), "mode")
    prompt_optional = model == "seedance2_5" or (has_image and model != "gen4.5" and model != "h3_max") or bool(video_keys)
    body: JSONObject = {"model": model, "duration": _duration_seconds(tool_input, model)}
    if "prompt" in tool_input or not prompt_optional or mode in {"extend", "edit"}:
        limit = 15000 if model == "seedance2_5" else 3500 if seedance else None if model == "h3_max" else 1000
        body["promptText"] = options.text(tool_input.get("prompt"), "prompt", api, limit, allow_longer_text=True)
    if model == "h3_max":
        if tool_input.get("ratio") is not None:
            raise ToolInputValidationError("Runway h3_max does not accept ratio; omit it and use a first-frame image to control the aspect ratio.")
        body["resolution"] = _string_choice(tool_input, "resolution", ("480p", "768p"), "768p")
        if "prompt_expansion_mode" in tool_input:
            body["promptExpansionMode"] = options.choice(tool_input["prompt_expansion_mode"], ("disabled", "balanced", "quality"), "prompt_expansion_mode")
    else:
        if "resolution" in tool_input or "prompt_expansion_mode" in tool_input:
            raise ToolInputValidationError("Runway resolution and prompt_expansion_mode require h3_max; Seedance resolution is selected through ratio.")
        ratios = options.MODEL_RATIOS.get(model, options.GEN4_IMAGE_RATIOS if has_image else SUPPORTED_RATIOS)
        if mode in {"extend", "edit"}:
            if "ratio" in tool_input:
                raise ToolInputValidationError("Runway Seedance extend/edit follows the source aspect ratio; omit ratio.")
            if mode == "edit" and body["duration"] != "auto":
                raise ToolInputValidationError("Runway Seedance edit requires duration_seconds=auto.")
        else:
            body["ratio"] = _string_choice(tool_input, "ratio", ratios, DEFAULT_RATIO)
    if mode is not None:
        body["mode"] = mode
    if "audio" in tool_input:
        if not (seedance or veo) or not isinstance(tool_input["audio"], bool):
            raise ToolInputValidationError("Runway audio must be a boolean and requires Seedance or Veo.")
        body["audio"] = tool_input["audio"]
    if "negative_prompt" in tool_input:
        if not veo:
            raise ToolInputValidationError("Runway negative_prompt requires Veo.")
        body["negativePrompt"] = options.text(tool_input["negative_prompt"], "negative_prompt", api)
    options.format_options(tool_input, body, model)
    endpoint = TEXT_TO_VIDEO_ENDPOINT
    if has_image:
        endpoint = IMAGE_TO_VIDEO_ENDPOINT
        if "prompt_images" in image_keys:
            maximum = 30 if model == "seedance2_5" else 9 if seedance else 2 if (veo or model == "h3_max") else 1
            frames = options.media_list(tool_input["prompt_images"], "image", api, uploads, maximum=maximum, extras=("position",))
            positions: list[str] = []
            for frame in frames:
                if "position" not in frame:
                    if not seedance:
                        raise ToolInputValidationError("Runway image keyframes require position.")
                    continue
                positions.append(options.choice(frame["position"], ("first", "last") if (seedance or veo or model == "h3_max") else ("first",), "position"))
            if positions and (len(positions) != len(frames) or len(set(positions)) != len(positions)):
                raise ToolInputValidationError("Runway keyframes cannot mix with unpositioned references or repeat positions.")
            if model == "h3_max" and "first" not in positions:
                raise ToolInputValidationError("Runway H3 Max last frame requires a first frame.")
            body["promptImage"] = cast(list, frames)
        else:
            body["promptImage"] = options.media_uri({"uri": tool_input.get("image_url"), "asset_id": tool_input.get("image_asset_id")}, "image", api, uploads)
    if video_keys:
        endpoint = VIDEO_TO_VIDEO_ENDPOINT
        body["promptVideo"] = options.media_uri({"uri": tool_input.get("video_url"), "asset_id": tool_input.get("video_asset_id")}, "video", api, uploads)
    for key, wire, kind in (("reference_images", "references", "image"), ("reference_videos", "referenceVideos", "video"), ("reference_audio", "referenceAudio", "audio")):
        if key not in tool_input:
            continue
        if not seedance or (has_image and kind != "audio"):
            raise ToolInputValidationError(f"Runway {key} requires Seedance text/video-to-video (audio is also accepted with image input).")
        maximum = 30 if model == "seedance2_5" else 9 if kind == "image" else 30
        references = options.media_list(tool_input[key], kind, api, uploads, maximum=maximum)
        if kind != "image":
            for reference in references:
                reference["type"] = kind
        body[wire] = cast(list, references)
    seed = _optional_seed(tool_input)
    if seed is not None:
        body["seed"] = seed
    return endpoint, body


def _edit_request(api: HostAPI, tool_input: JSONObject, uploads: dict[str, str]) -> JSONObject:
    if set(tool_input) - {"video_url", "video_asset_id", "prompt", "seed", *options.EDIT_PROPERTIES}:
        raise ToolInputValidationError("Runway edit_video received unsupported fields.")
    if (tool_input.get("video_url") is None) == (tool_input.get("video_asset_id") is None):
        raise ToolInputValidationError("Runway edit_video requires exactly one of video_url or video_asset_id.")
    body: JSONObject = {
        "model": EDIT_MODEL,
        "videoUri": options.media_uri({"uri": tool_input.get("video_url"), "asset_id": tool_input.get("video_asset_id")}, "video", api, uploads),
    }
    if "prompt" in tool_input:
        body["promptText"] = options.text(tool_input["prompt"], "prompt", api, allow_longer_text=True)
    if "keyframes" in tool_input:
        body["keyframes"] = cast(list, options.keyframes(tool_input["keyframes"], api, uploads))
    if "ratio" in tool_input:
        ratio = tool_input["ratio"]
        if not isinstance(ratio, str) or not re.fullmatch(r"[1-9][0-9]{0,4}:[1-9][0-9]{0,4}", ratio):
            raise ToolInputValidationError("Runway Aleph ratio must be width:height pixel dimensions.")
        body["ratio"] = ratio
    if "target_aspect_ratio" in tool_input:
        body["targetAspectRatio"] = options.choice(tool_input["target_aspect_ratio"], options.ASPECT_RATIOS, "target_aspect_ratio")
    options.format_options(tool_input, body, EDIT_MODEL)
    seed = _optional_seed(tool_input)
    if seed is not None:
        body["seed"] = seed
    return body


def _multipart_parts(
    fields: JSONObject,
    *,
    filename: str,
    media_type: str,
    boundary: str,
) -> tuple[bytes, bytes]:
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise RuntimeError("Runway upload initialization returned invalid form fields.")
        if any(ord(character) < 32 or ord(character) == 127 for character in name + value):
            raise RuntimeError("Runway upload initialization returned invalid form fields.")
        safe_name = name.replace('"', "")
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise RuntimeError("Runway staged filename contains invalid control characters.")
    safe_filename = filename.replace('"', "")
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode("utf-8")
    )
    return b"".join(chunks), f"\r\n--{boundary}--\r\n".encode("ascii")


def _multipart_stream(prefix: bytes, source: BinaryIO, suffix: bytes) -> Iterator[bytes]:
    yield prefix
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        yield chunk
    yield suffix


def _upload_staged_asset(
    asset_id: str, headers: dict[str, str], api: HostAPI, *, kind: str
) -> str:
    metadata = api.assets.describe(asset_id)
    if not metadata.media_type.startswith(f"{kind}/"):
        raise ToolInputValidationError(
            f"Runway {kind}_asset_id does not refer to a staged {kind}."
        )
    initialized = json_request(
        "POST",
        UPLOADS_ENDPOINT,
        headers=headers,
        body={"filename": metadata.filename, "type": "ephemeral"},
        failure_message=f"Runway {kind} upload initialization failed.",
        invalid_response_message=f"Runway {kind} upload initialization returned an invalid response.",
    )
    upload_url = initialized.get("uploadUrl")
    runway_uri = initialized.get("runwayUri")
    fields = initialized.get("fields")
    if not isinstance(upload_url, str) or not _is_public_https_url(upload_url):
        raise RuntimeError(f"Runway {kind} upload initialization returned no HTTPS upload URL.")
    if (
        not isinstance(runway_uri, str)
        or len(runway_uri) > 2_048
        or not runway_uri.startswith("runway://")
    ):
        raise RuntimeError(f"Runway {kind} upload initialization returned no runway URI.")
    if not isinstance(fields, dict):
        raise RuntimeError(f"Runway {kind} upload initialization returned invalid form fields.")
    boundary = f"kern-{secrets.token_hex(16)}"
    prefix, suffix = _multipart_parts(
        cast(JSONObject, fields),
        filename=metadata.filename,
        media_type=metadata.media_type,
        boundary=boundary,
    )
    with api.assets.open(asset_id) as source:
        stream_request_bytes(
            "POST",
            upload_url,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=_multipart_stream(prefix, source, suffix),
            content_length=len(prefix) + metadata.size_bytes + len(suffix),
            failure_message=f"Runway {kind} upload failed.",
            timeout=120,
        )
    return runway_uri


def _image_request(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"prompt", "model", "ratio", "quality"}
    if extra:
        raise ToolInputValidationError("Runway generate_image only supports prompt, model, ratio, and quality.")
    return {
        "model": _string_choice(tool_input, "model", IMAGE_MODELS, DEFAULT_IMAGE_GENERATION_MODEL),
        "promptText": _prompt_text(tool_input, api),
        "ratio": _string_choice(tool_input, "ratio", IMAGE_RATIOS, DEFAULT_IMAGE_RATIO),
        "quality": _string_choice(tool_input, "quality", IMAGE_QUALITIES, DEFAULT_IMAGE_QUALITY),
        "outputCount": 1,
    }


def _speech_request(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"text", "voice", "model", "stability", "style", "speed"}
    if extra:
        raise ToolInputValidationError("Runway generate_speech only supports text, voice, model, stability, style, and speed.")
    text = tool_input.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolInputValidationError("Runway tool_input.text is required.")
    text = text.strip()
    if len(text) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(f"Runway speech text must be at most {MAX_PROMPT_CHARS} characters.")
    voice = _string_choice(tool_input, "voice", SPEECH_VOICES, DEFAULT_SPEECH_VOICE)
    model = _string_choice(tool_input, "model", SPEECH_MODELS, DEFAULT_SPEECH_MODEL)
    body: JSONObject = {
        "model": model,
        "promptText": api.outbound.guard_request_parameter_string(text),
        "voice": {"type": "runway-preset", "presetId": voice},
    }
    for name, lower, upper in (("stability", 0, 1), ("style", 0, 1), ("speed", 0.7, 1.2)):
        if name not in tool_input:
            continue
        if model != "eleven_v3":
            raise ToolInputValidationError(f"Runway speech {name} requires model eleven_v3.")
        value = tool_input[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise ToolInputValidationError(f"Runway speech {name} must be a number from {lower} to {upper}.")
        body[name] = value
    return body


def _task_result(response: JSONObject, output_kind: str = "video") -> JSONObject:
    task_id = response.get("id")
    task_status = response.get("status")
    result: JSONObject = {
                "task_id": task_id if isinstance(task_id, str) else "",
        "task_status": task_status if isinstance(task_status, str) else "unknown",
    }
    if task_status == "SUCCEEDED":
        output = response.get("output")
        if isinstance(output, list):
            result["output_urls"] = [url for url in output if isinstance(url, str) and _is_public_https_url(url)]
        output_url = ""
        if isinstance(output, list) and output and isinstance(output[0], str):
            output_url = output[0]
        if output_url and _is_public_https_url(output_url):
            result[f"{output_kind}_url"] = output_url
            result["message"] = (
                f"Generation succeeded. The {output_kind}_url is a temporary link valid for about "
                "24-48 hours; download or hand off the URL promptly."
            )
        else:
            result["message"] = "Runway reported success but returned no output URL. Submit a new task."
    elif task_status == "FAILED":
        failure_code = response.get("failureCode")
        code = f" (code: {failure_code})" if isinstance(failure_code, str) and failure_code else ""
        result["message"] = f"Runway generation failed{code}. Submit a new task."
    elif task_status == "CANCELLED":
        result["message"] = "Runway task was cancelled. Submit a new task."
    elif task_status == "THROTTLED":
        result["message"] = (
            "Runway queued the task (concurrency limit reached). Poll get_task again in ~15 seconds."
        )
    else:
        progress = response.get("progress")
        percent = ""
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            percent = f" ({round(progress * 100)}%)"
        result["message"] = f"Runway task is {result['task_status']}{percent}. Poll get_task again in ~15 seconds."
    return result


# Structural-only URL check shared with the other media tools; see
# host.tools.shared.web.is_public_https_url for why the host is not pinned.
_is_public_https_url = is_public_https_url


def _failure_from_status(exc: WebRequestError) -> str:
    if exc.status == 401:
        message = "Runway rejected the configured API key."
    elif exc.status == 403:
        message = "Runway denied the request (insufficient permissions for this model or action)."
    elif exc.status == 404:
        message = "Runway task was not found."
    elif exc.status == 429:
        message = "Runway rate limit or daily generation quota was reached."
    elif exc.status in {400, 422}:
        message = "Runway rejected the request. Check that the model, prompt, aspect ratio, and duration are compatible."
    elif exc.status:
        message = f"Runway API returned HTTP {exc.status}."
    else:
        message = known_provider_transport_error(exc)
        if not message:
            raise unmapped_provider_error("Runway", "API", exc) from None
    return message


def _save_media(task_id: str, headers: dict[str, str], *, kind: str) -> ActionResult:
    if not TASK_ID_RE.fullmatch(task_id):
        raise ToolInputValidationError("Runway tool_input.task_id is invalid.")
    response = json_request(
        "GET",
        f"{TASKS_ENDPOINT}/{urllib.parse.quote(task_id, safe='')}",
        headers=headers,
        failure_message="Runway task lookup failed.",
        invalid_response_message="Runway returned an invalid task response.",
    )
    if response.get("status") != "SUCCEEDED":
        return ActionFailed(f"Runway {kind} is not complete. Poll get_task and try again after it succeeds.")
    output = response.get("output")
    output_url = output[0] if isinstance(output, list) and output and isinstance(output[0], str) else ""
    if not output_url or not _is_public_https_url(output_url):
        return ActionFailed(f"Runway reported success but returned no valid {kind} URL.")
    def open_media():
        download = open_downloaded_audio if kind == "audio" else open_downloaded_video
        return download(
            output_url,
            provider="Runway",
            filename_stem=f"runway-{task_id}",
            map_failure=_failure_from_status,
        )

    return StreamingAsset(open_media)


class RunwayTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def _create_task(
        self, endpoint: str, body: JSONObject, headers: dict[str, str], model: str, output_kind: str
    ) -> ActionResult:
        response = json_request(
            "POST",
            endpoint,
            headers=headers,
            body=body,
            failure_message="Runway API request failed.",
            invalid_response_message="Runway API returned an invalid response.",
        )
        task_id = response.get("id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            return ActionFailed("Runway API returned no task id.")
        return ActionExecuted(
            {
                                "message": f"Runway task created. Poll get_task with output_kind={output_kind} until it succeeds.",
                "task_id": task_id,
                "task_status": "PENDING",
                "model": model,
                "output_kind": output_kind,
            }
        )

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            api_key = api.config["RUNWAY_API_SECRET"]
            headers = {
                "authorization": f"Bearer {api_key}",
                "x-runway-version": RUNWAY_API_VERSION,
            }
            if action in {"generate_video", "edit_video"}:
                uploads: dict[str, str] = {}
                if action == "generate_video":
                    endpoint, body = _generation_request(api, tool_input, uploads)
                else:
                    endpoint, body = VIDEO_TO_VIDEO_ENDPOINT, _edit_request(api, tool_input, uploads)
                uploaded = {asset_id: _upload_staged_asset(asset_id, headers, api, kind=kind)
                            for asset_id, kind in uploads.items()}
                body = cast(JSONObject, options.replace_assets(body, uploaded))
                result = self._create_task(endpoint, body, headers, cast(str, body["model"]), "video")
                if isinstance(result, ActionExecuted):
                    for asset_id in uploads:
                        api.assets.delete(asset_id)
                return result
            if action in {"save_video", "save_audio"}:
                if set(tool_input) != {"task_id"} or not isinstance(tool_input.get("task_id"), str):
                    raise ToolInputValidationError(
                        f"Runway {action} requires exactly one string task_id."
                    )
                return _save_media(
                    cast(str, tool_input["task_id"]), headers,
                    kind="audio" if action == "save_audio" else "video",
                )
            if action == "generate_image":
                body = _image_request(api, tool_input)
                return self._create_task(TEXT_TO_IMAGE_ENDPOINT, body, headers, cast(str, body["model"]), "image")
            if action == "generate_speech":
                body = _speech_request(api, tool_input)
                return self._create_task(TEXT_TO_SPEECH_ENDPOINT, body, headers, cast(str, body["model"]), "audio")
            if action == "get_task":
                extra = set(tool_input) - {"task_id", "output_kind"}
                if extra:
                    raise ToolInputValidationError("Runway get_task only supports task_id and output_kind.")
                task_id_value = tool_input.get("task_id")
                if not isinstance(task_id_value, str) or not TASK_ID_RE.fullmatch(task_id_value.strip()):
                    raise ToolInputValidationError("Runway tool_input.task_id must be a valid task id string.")
                response = json_request(
                    "GET",
                    f"{TASKS_ENDPOINT}/{task_id_value.strip()}",
                    headers=headers,
                    failure_message="Runway API request failed.",
                    invalid_response_message="Runway API returned an invalid response.",
                )
                output_kind = _string_choice(tool_input, "output_kind", ("video", "image", "audio"), "video")
                return ActionExecuted(_task_result(response, output_kind))
            return ActionFailed("Unsupported Runway action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            return ActionFailed(_failure_from_status(exc))
        except UnmappedProviderError:
            raise
        except (ValueError, RuntimeError) as exc:
            # The tool's own errors (validation, config-unset) carry curated,
            # secret-free messages; an unexpected exception must not leak its
            # raw text (e.g. internal filesystem paths) to the agent.
            return ActionFailed(str(exc) or "Runway tool request failed.")
        except Exception:
            return ActionFailed("Runway tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Runway has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = RunwayTool()
