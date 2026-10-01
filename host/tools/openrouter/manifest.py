"""HeyGen Video through OpenRouter, with private staged reference media."""
from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink,
    DataSummaryPoint, SetupStep, ToolManifest, guarded_input, protect_inputs, validated_input,
)
from host.tools.shared import outputs
from host.tools.shared.inputs import schema

TASK_INPUT: JSONObject = {"task_id": {"type": "string", "description": "Job ID returned by create_heygen_video."}}
STATUSES: list[JSONValue] = ["queued", "running", "succeeded", "failed", "cancelled", "expired", "unknown"]
TASK_OUTPUT: JSONObject = outputs.obj({
    "task_id": outputs.text("OpenRouter job ID; pass unchanged to get_task and save_video."),
    "task_status": {"type": "string", "enum": STATUSES},
    "message": outputs.text("How to continue or what happened to the job."),
}, ["task_id", "task_status", "message"])


def references(kind: str, maximum: int) -> JSONObject:
    return {"type": "array", "items": {"type": "string"}, "maxItems": maximum,
            "description": f"Staged {kind} asset IDs, ordered for prompt references. Stage each with for_tool=openrouter. No source URLs or paths."}


MANIFEST = ToolManifest(
    tool_id="openrouter", display_name="OpenRouter", connection="enable_only", reports_cost=True,
    description="Connect to models from different providers through OpenRouter.",
    agent_notes=(
        "create_heygen_video always selects heygen/heygen-video-1. Stage source media with "
        "stage_image, stage_video or stage_audio and for_tool=openrouter. Use image_asset_id for an opening frame, "
        "or reference_*_asset_ids for visual/audio guidance; never mix those modes. Reference lists are addressed "
        "independently as <Picture 1>, <Video 1>, <Audio 1>, and so on. Audio alone cannot select reference mode. "
        "HeyGen includes native audio. First-frame mode follows the image's shape; crop it before staging to change framing. "
        "Clips run 5-15 seconds at 480p or 768p. "
        "Images are at most 5 MB each; video/audio at most 16 MB each, all references at most 60 MB. "
        "Generation starts one paid job and returns immediately. Poll get_task about every 30 seconds, then save_video. "
        "A conservative cost estimate is recorded internally in Kern's MTD tool usage at accepted submission, even if never polled. Do not repeat creation after an ambiguous submission: check OpenRouter activity first."
    ),
    actions=protect_inputs((
        ActionSpec(
            id="create_heygen_video",
            description="Start one paid HeyGen Video 1 job from a prompt, opening frame, or multimodal references. Returns a task_id for get_task and save_video.",
            data_policy="Sends the guarded prompt, generation settings and selected staged media bytes to OpenRouter and HeyGen. Runs directly, bills the configured OpenRouter account, and publishes nothing. Staged bytes use inline data URLs; no public source link is created.",
            cost_description="Records a conservative USD estimate in Kern's MTD usage once OpenRouter accepts the job. Uses the higher of live catalog and documented list rates, without promotional discounts: 480p/768p text or first-frame $0.02/$0.03 per output second; reference mode $0.04/$0.06 per output plus video-reference second. Video durations round up to whole seconds; unreadable video metadata counts as 60 seconds per reference. Image/audio references add no extra seconds. This is an estimate, not the provider invoice; later refunds and actual charges are not reconciled.",
            input_schema=schema({
                "prompt": {"type": "string", "description": "Scene, camera, motion, dialogue and sound direction, up to 5120 UTF-8 bytes. Reference labels: <Picture 1>, <Video 1>, <Audio 1>."},
                "mode": {"type": "string", "enum": ["auto", "text", "image", "reference"], "description": "Default auto chooses from supplied media. An explicit mode must match the inputs."},
                "image_asset_id": {"type": "string", "description": "Staged JPEG/PNG/WebP opening frame. Output follows this image's aspect ratio; omit aspect_ratio. Cannot mix with references."},
                "reference_image_asset_ids": {**references("image", 9), "description": "Staged images guiding subjects, products or places in a new scene. They do not fix the opening frame. Order them for <Picture 1>, <Picture 2>, etc. Stage with for_tool=openrouter; no source URLs or paths."},
                "reference_video_asset_ids": references("video", 3),
                "reference_audio_asset_ids": references("audio", 3),
                "duration_seconds": {"type": "string", "description": "Any integer 5-15, default 5. Longer output costs more."},
                "resolution": {"type": "string", "enum": ["480p", "768p"], "description": "Default 768p. 480p costs less."},
                "aspect_ratio": {"type": "string", "enum": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"], "description": "Text defaults to 16:9. Optional reference-mode shape; omit to leave provider framing defaults. First-frame mode uses the image's shape."},
                "seed": {"type": "string", "description": "Optional unsigned 32-bit integer (0-4294967295). Omit for random variation."},
            }, ["prompt"]), output_schema=TASK_OUTPUT,
        ),
        ActionSpec(
            id="get_task", description="Poll a video job's status. Save successful jobs promptly because remote output storage is temporary.",
            data_policy="Sends only the validated job ID and API key to OpenRouter. Creates no generation and needs no approval.",
            cost_description="No generation charge or usage update. The estimate is recorded at submission; polling does not add or replace it.",
            input_schema=schema(TASK_INPUT, ["task_id"]), output_schema=TASK_OUTPUT,
        ),
        ActionSpec(
            id="save_video", returns_asset=True,
            description="Download output index 0 of a completed job into durable tool_assets. Returns its private workspace file path.",
            data_policy="Sends the job ID and API key only to fixed OpenRouter status/content endpoints, then streams the video into the agent workspace. No approval or publication occurs.",
            cost_description="No generation charge or usage update. Saving does not add or replace the submission estimate.",
            input_schema=schema(TASK_INPUT, ["task_id"]),
        ),
    ), {
        "create_heygen_video": {
            "prompt": guarded_input(allow_longer_text=True),
            **{key: validated_input("Typed setting, compatible input mode and live HeyGen capability checks before paid submission.") for key in ("mode", "duration_seconds", "resolution", "aspect_ratio", "seed")},
            **{key: validated_input("Opaque staged IDs; tool ownership, expiry, media type, byte size and reference count checked. No caller URLs or paths.") for key in ("image_asset_id", "reference_image_asset_ids", "reference_video_asset_ids", "reference_audio_asset_ids")},
        },
        "get_task": {"task_id": validated_input("Documented gen-vid ID or legacy 20-character alphanumeric ID; cannot address another API route.")},
        "save_video": {"task_id": validated_input("Documented gen-vid ID or legacy 20-character alphanumeric ID; cannot address another API route.")},
    }),
    config=(ConfigRequirement(key="OPENROUTER_API_KEY", description="OpenRouter inference key. Set a credit limit suitable for paid video generation."),),
    setup_steps=(
        SetupStep(title="Fund OpenRouter and create a key", description="Add credit and create an inference key with a spending limit. A separate HeyGen key is not used by this integration.", link_url="https://openrouter.ai/settings/keys", link_label="Open OpenRouter API keys"),
        SetupStep(title="Choose your data policy", description="Review training and logging settings before sending confidential prompts or media. You can exclude providers that train. Enforced zero data retention blocks video generation; Kern does not bypass it.", link_url="https://openrouter.ai/settings/privacy", link_label="Open OpenRouter privacy settings"),
        SetupStep(title="Configure and enable", description="Save OPENROUTER_API_KEY here and enable OpenRouter. Ask the agent to create a HeyGen clip, optionally using files from your workspace.", show_config=True),
    ),
    protections=(
        "The API key stays in write-only configuration. Creation pins HeyGen Video 1, and checks current OpenRouter capabilities before submitting.",
        "Source files use private, tool-scoped staging and inline bytes, without public links. Selected staged copies are consumed after an accepted submission.",
        "Creation runs once. Polling and saving never create another job; downloads stay on the authenticated OpenRouter endpoint and refuse redirects.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        PARAM_GUARD_TECHNICAL_DETAIL,
        "Media IDs must belong to this tool and remain unexpired. JPEG/PNG/WebP inputs are at most 5 MB; MP4/MOV/MP3/WAV inputs at most 16 MB. References permit 9 images, 3 videos, 3 audio files and 12 total, at most 60 MB combined. Media duration/content constraints remain provider-validated. Bytes become data URLs internally, never model context or caller URLs.",
        "Mode conflicts fail before submission. Arbitrary provider options, callbacks, model overrides and endpoints are not accepted. First-frame aspect overrides fail rather than being ignored.",
        "Accepted submission records one estimate with a stable job charge ID, without needing a poll. MP4/MOV movie-header durations round up; unreadable metadata uses a 60-second allowance per reference, not a guaranteed upper bound. Catalog pricing falls back to verified list rates and ignores discounts. Polls/saves ignore usage.cost, so estimates remain unchanged after completion or refunds. Output index 0 uses the shared video type and 200 MB bounds.",
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard(title="What leaves this host", description="The scene prompt, generation settings and selected source image/video/audio bytes go to OpenRouter with your API key. OpenRouter forwards them to HeyGen. Polls and downloads send the job ID. Original workspace files stay until you delete them; the selected staged copies are consumed after acceptance."),
        DataSummaryCard(title="Where it can go", description="Creation selects heygen/heygen-video-1 through OpenRouter. HeyGen and its subprocessors receive the data needed to render the clip. This integration uses OpenRouter's global endpoint and does not guarantee processing in a particular country.", links=(DataSummaryLink(label="OpenRouter provider policies", url="https://openrouter.ai/docs/guides/privacy/provider-logging"), DataSummaryLink(label="HeyGen subprocessors", url="https://www.heygen.com/trust-and-safety"))),
        DataSummaryCard(title="What the provider can do with it", points=(
            DataSummaryPoint(label="OpenRouter", text="Says it does not train models on inputs or outputs. Optional content logging and product-improvement use are separate, off-by-default settings. You can exclude providers that train in account settings."),
            DataSummaryPoint(label="HeyGen", text="Its public policy permits training and model improvement on non-enterprise customer data with an opt-out, while its trust page excludes enterprise data. It also screens content and may review flagged material. These public policies do not establish a stronger OpenRouter-specific agreement; Kern cannot assume a separate HeyGen enterprise contract applies."),
        ), links=(DataSummaryLink(label="OpenRouter privacy", url="https://openrouter.ai/privacy"), DataSummaryLink(label="HeyGen privacy and training opt-out", url="https://www.heygen.com/privacy"), DataSummaryLink(label="HeyGen enterprise and moderation policy", url="https://www.heygen.com/trust-and-safety"))),
        DataSummaryCard(title="How long it is retained", points=(
            DataSummaryPoint(label="Video jobs", text="Require temporary remote output storage and are not eligible for zero data retention. Account-wide ZDR blocks routing. Save completed clips promptly; no fixed job expiry is promised."),
            DataSummaryPoint(label="OpenRouter", text="Keeps request metadata. Content logging follows account settings; media may also be retained for abuse prevention, security, billing or legal compliance."),
            DataSummaryPoint(label="HeyGen", text="Its general policy keeps data as needed for service and other stated purposes. Deleted data can remain in disaster-recovery backups for 60 days; deletion requests are targeted within 72 hours, subject to exceptions. These are public-policy terms, not a verified OpenRouter job deletion schedule."),
            DataSummaryPoint(label="Your saved files", text="Remain in the Kern workspace until you delete them."),
        ), links=(DataSummaryLink(label="Video storage and ZDR", url="https://openrouter.ai/docs/guides/overview/multimodal/video-generation"), DataSummaryLink(label="HeyGen retention policy", url="https://www.heygen.com/privacy"), DataSummaryLink(label="OpenRouter privacy", url="https://openrouter.ai/privacy"))),
    )),
)
