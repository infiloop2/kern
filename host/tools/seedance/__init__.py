"""Seedance video generation tool package (BytePlus ModelArk).

Seedance is ByteDance's video model family. Runway's aggregator API — the other
media tool bundled here — exposes Seedance 2, but not yet 2.5, so this package
calls ByteDance's own ModelArk API directly. Going direct also keeps the model's
operator to a single party: routing 2.5 through an aggregator would hand the
prompt and any reference image to the aggregator *and* to ByteDance.

ModelArk's video surface is a single Bearer-authenticated JSON API where every
generation is an async task: POST a task, then poll GET tasks/{id} until a
terminal status.
"""

from __future__ import annotations

import re
import urllib.parse

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
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
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.inputs import ToolInputValidationError, provider_fetched_https_url
from host.tools.shared.media import open_downloaded_video
from host.tools.shared.web import (
    WebRequestError,
    is_public_https_url,
    json_request,
    known_provider_transport_error,
    unmapped_provider_error,
)
from typing import cast

# ModelArk is regional, and the region decides where prompts and reference
# images are processed. This host pins the international Asia Pacific (Johor)
# endpoint so the manifest can name one destination instead of "wherever the
# account happens to be"; the mainland-China Volcengine Ark domain is a
# different service with different terms and is deliberately not reachable here.
ARK_API_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
TASKS_ENDPOINT = f"{ARK_API_BASE}/contents/generations/tasks"

# The ModelArk model id for Seedance 2.5, pinned like every other provider
# contract in this package: the agent cannot spend the operator's balance on a
# model the manifest did not disclose, and a ModelArk release that changes the id
# is a code change here rather than a configuration surface to maintain.
# BytePlus international namespaces these models "dreamina-*"; the China-region
# Volcengine Ark service uses "doubao-*" ids that this endpoint does not accept.
MODEL_ID = "dreamina-seedance-2-5-260628"

# Bounded below the outbound guard's 1024-byte parameter limit, which every
# prompt passes through: advertising more than the guard admits would only
# promise a length that always fails locally. (A prompt of mostly multi-byte
# characters can still exceed the byte limit; the guard's own message says so.)
MAX_PROMPT_CHARS = 1_000

# Seedance 2.5 renders up to 30 seconds in one pass; ModelArk bills per token,
# and tokens scale with resolution, duration, and whether audio is generated.
# The defaults here are the cheap end of each axis on purpose — a caller that
# wants 30 seconds or native audio asks for it explicitly.
# 2.5's API currently renders 480p and 720p only. The model's 4K output is a
# consumer-product capability, not an API one, so admitting 1080p/4K here would
# advertise a schema option every request rejects. Widen this when ModelArk
# documents the higher tiers on the video task API.
RESOLUTIONS = ("480p", "720p")
DEFAULT_RESOLUTION = "720p"
# "adaptive" takes the output ratio from the input image, so it is the default
# whenever a first frame is supplied: forcing a fixed ratio onto a reference that
# does not match it crops or distorts the frame the caller asked to animate.
# An explicit ratio is still forwarded rather than rejected here. Provider
# documentation disagrees about whether image-to-video accepts a fixed ratio —
# some describe it as center-cropped, others as unsupported — and ModelArk's own
# reference does not state a restriction. Refusing locally would remove a
# capability on the strength of the stricter reading; forwarding costs a mapped
# "unsupported combination" error in the case where that reading is right.
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")
DEFAULT_RATIO = "16:9"
DEFAULT_IMAGE_RATIO = "adaptive"
MIN_DURATION_SECONDS = 4
MAX_DURATION_SECONDS = 30
DEFAULT_DURATION_SECONDS = 5

# ModelArk task ids look like "cgt-20260807...-abcde"; the pattern also keeps a
# provider-supplied id from walking the tasks path when it is interpolated.
# Must start with an alphanumeric, which is what keeps "." and ".." out. Percent
# encoding leaves dots untouched, so a dot-only id would build "/tasks/.." and
# address the parent resource on any gateway that normalizes path dot segments —
# exactly the path-walking this pattern exists to prevent.
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# Provider failure codes are echoed to the agent, so they are constrained to a
# short opaque token rather than a free-text provider string.
FAILURE_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

TERMINAL_SUCCESS = "succeeded"
# ModelArk's documented task lifecycle. Any other value the provider sends is
# reported as "unknown" rather than passed through to the agent.
TASK_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "expired"})
# Terminal without output: no amount of polling turns one of these into a video.
TERMINAL_FAILURES = frozenset({"failed", "cancelled", "expired"})

SEEDANCE_GENERATE_POLICY = (
    "The prompt and optional reference-image URL supplied by the user or agent are sent to "
    "ByteDance's BytePlus ModelArk API to render a video, billed as tokens against the "
    "deployment's ModelArk account. This action runs directly with no approval; it publishes "
    "nothing anywhere, and the result is a task id returned to active model context to poll "
    "with get_task."
)
SEEDANCE_POLL_POLICY = (
    "Read-only poll. Sends only the task id to BytePlus ModelArk and returns the task status, "
    "the tokens billed, and, once finished, a temporary download URL for the generated video "
    "into active model context. Runs directly with no approval."
)
SEEDANCE_SAVE_VIDEO_POLICY = (
    "Read-only handoff. Sends the task id to BytePlus ModelArk, downloads the completed video "
    "from ModelArk's authoritative temporary output URL, and streams it through the agent-side "
    "bridge into a host-generated path under /tool_assets in the agent workspace."
)

SEEDANCE_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="seedance",
    display_name="Seedance Video Generation",
    description=(
        "Connect ByteDance's BytePlus ModelArk and let your agent generate Seedance 2.5 video "
        "with native audio, straight from the model's own provider."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="generate_video",
            description=(
                "Start an async Seedance 2.5 video generation task from a text prompt and an "
                "optional first-frame reference image from a public URL. Returns a task_id to "
                "poll with get_task; renders typically take one to a few minutes. This runs "
                "immediately, spends ModelArk tokens, and creates no public post."
            ),
            data_policy=SEEDANCE_GENERATE_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "What to render, including any shot direction (up to 1000 chars)."},
                    "image_url": {"type": "string", "description": "Optional public HTTPS image URL used as the first frame (image-to-video)."},
                    "resolution": {
                        "type": "string",
                        "enum": list(RESOLUTIONS),
                        "description": "Output resolution: 720p (default) or the cheaper 480p. Higher resolutions bill more ModelArk tokens.",
                    },
                    "ratio": {
                        "type": "string",
                        "enum": list(RATIOS),
                        "description": (
                            "Output aspect ratio, e.g. 16:9 or 9:16. Defaults to 16:9, or to "
                            "adaptive (match the first frame) when image_url is set. Prefer the "
                            "default with image_url: a fixed ratio the first frame does not "
                            "already have is center-cropped to fit, and some accounts reject the "
                            "combination outright."
                        ),
                    },
                    "duration_seconds": {"type": "string", "description": "Video length in seconds, 4-30 (default 5). Longer videos bill more tokens."},
                    "generate_audio": {"type": "boolean", "description": "Set true for Seedance's native synchronized audio (default false); audio bills more tokens."},
                    "seed": {"type": "string", "description": "Optional integer seed for reproducible output."},
                },
                "additionalProperties": False,
            },
            output_schema=SEEDANCE_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_task",
            description=(
                "Poll a task_id returned by generate_video. Pending tasks have no output, while "
                "success returns a temporary video_url valid about 24 hours, plus the tokens the "
                "generation billed."
            ),
            data_policy=SEEDANCE_POLL_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "ModelArk task id returned when generation starts."},
                },
                "additionalProperties": False,
            },
            output_schema=SEEDANCE_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="save_video",
            description=(
                "Save a completed Seedance video under /tool_assets in the agent workspace "
                "before its ModelArk URL expires. The agent-side bridge creates the filename "
                "and returns the durable path."
            ),
            data_policy=SEEDANCE_SAVE_VIDEO_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "Completed ModelArk video task id."},
                },
                "additionalProperties": False,
            },
            output_schema=SEEDANCE_OUTPUT_SCHEMA,
        ),
    ),
    config=(
        ConfigRequirement(
            key="SEEDANCE_ARK_API_KEY",
            description="BytePlus ModelArk API key from the BytePlus console (console.byteplus.com).",
        ),
    ),
    protections=(
        "Your ModelArk key stays in write-only tool config. Inputs are bounded, and the model, "
        "region endpoint, and resolution/duration ceilings are pinned in the tool rather than "
        "chosen by the agent.",
        "Generation is billed to your BytePlus account. Kern does not publish the media. A "
        "completed video can be saved from ModelArk's authoritative temporary URL into the agent "
        "workspace for durable operator review and later approval-gated publishing.",
        "Requests go only to ByteDance's own API. Kern does not route Seedance through a "
        "reseller or aggregator, so no third party sees the prompt or reference image.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(PARAM_GUARD_TECHNICAL_DETAIL,),
    setup_steps=(
        SetupStep(
            title="Create a BytePlus account and activate Seedance",
            description=(
                "Open the BytePlus console, sign in, and open ModelArk. Confirm the account is on "
                "the international Asia Pacific (Johor) region this tool calls, then activate the "
                "Seedance 2.5 video model and check it appears in your account's model list. "
                "Generation is prepaid or postpaid against this BytePlus account."
            ),
            link_url="https://console.byteplus.com/",
            link_label="Open the BytePlus console",
        ),
        SetupStep(
            title="Create a ModelArk API key",
            description=(
                "In ModelArk's API key area, create a clearly named key and copy it immediately to "
                "a password manager or Kern. Use an API key rather than an Access Key/Secret Key "
                "pair; this tool authenticates with a single bearer key."
            ),
            link_url="https://docs.byteplus.com/en/docs/ModelArk/1361424",
            link_label="View ModelArk's API key guide",
        ),
        SetupStep(
            title="Configure and enable Seedance",
            show_config=True,
            description=(
                "Open Seedance Video Generation under Home > Integrations, save the key as "
                "SEEDANCE_ARK_API_KEY, then enable the tool. Never place the key in a prompt, "
                "reference-image URL, or media filename."
            ),
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(
                        label="Generation requests",
                        text=(
                            "The prompt, generation options (resolution, aspect ratio, duration, "
                            "audio flag, seed), and any public reference-image URL given as input go "
                            "to BytePlus ModelArk. The free-text values (prompt, external image URL) "
                            "first pass the host parameter guard (see Technical notes), which denies "
                            "secret- or credential-shaped values before anything is sent."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Reference images",
                        text=(
                            "Only a URL is sent, never bytes from the agent workspace: ModelArk "
                            "fetches the image itself. Anything encoded in that URL's path or query "
                            "leaves this host with it, which is why the whole URL is scanned."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(
                        label="One provider",
                        text=(
                            "Every request goes to ByteDance's BytePlus ModelArk API and nowhere "
                            "else. Seedance is ByteDance's own model, so no aggregator, reseller, or "
                            "second model provider sits in the path."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Region",
                        text=(
                            "Requests are pinned to ModelArk's international Asia Pacific endpoint. "
                            "BytePlus says the data centers that run these models and process customer "
                            "data are in Johor, Malaysia and/or Jakarta, Indonesia. This tool cannot be "
                            "pointed at the mainland-China Volcengine Ark service, which is a separate "
                            "offering under different terms."
                        ),
                    ),
                    DataSummaryPoint(
                        label="Onward transfer",
                        text=(
                            "BytePlus states that without the customer's prior authorization no customer "
                            "data, input or output, is transferred to any third party."
                        ),
                    ),
                ),
            ),
            DataSummaryCard(
                title="What BytePlus can do with it",
                description=(
                    "BytePlus states that for ModelArk it acts as a data processor, processing customer "
                    "data only on the customer's instructions, and that without the customer's prior "
                    "authorization it will not use customer data for its own model training. There is no "
                    "training opt-out to switch off here, because training use is opt-in by authorization "
                    "rather than a default you must decline. Its video model terms treat generated output "
                    "as the customer's data and disclaim ownership of output. Inputs and outputs still "
                    "pass ModelArk's content pre-filter and moderation, and BytePlus remains a ByteDance "
                    "company subject to its group privacy policy."
                ),
                links=(
                    DataSummaryLink(
                        label="ModelArk data processing",
                        url="https://docs.byteplus.com/api/docs/ModelArk/BytePlus_ModelArk_Data_Processing",
                    ),
                    DataSummaryLink(
                        label="Video generation model terms",
                        url="https://docs.byteplus.com/en/docs/ModelArk/Specific_Terms_for_the_BytePlus_Video_Generation_Model_Services",
                    ),
                    DataSummaryLink(
                        label="BytePlus privacy policy",
                        url="https://docs.byteplus.com/en/docs/legal/docs-privacy-policy",
                    ),
                ),
            ),
            DataSummaryCard(
                title="How long BytePlus retains it",
                description=(
                    "Generated video URLs are temporary and typically expire about 24 hours after the "
                    "task succeeds, which is why save_video exists. BytePlus publishes no single fixed "
                    "retention period for prompts and task records; retention follows the ModelArk "
                    "terms and the account's contract."
                ),
                links=(
                    DataSummaryLink(
                        label="ModelArk data processing",
                        url="https://docs.byteplus.com/api/docs/ModelArk/BytePlus_ModelArk_Data_Processing",
                    ),
                ),
            ),
        ),
    ),
)


def _prompt_text(tool_input: JSONObject, api: HostAPI) -> str:
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolInputValidationError("Seedance tool_input.prompt is required.")
    prompt = prompt.strip()
    # Reject rather than truncate: a silently cut prompt would spend tokens on a
    # render of an input the agent did not ask for.
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(f"Seedance prompt must be at most {MAX_PROMPT_CHARS} characters.")
    return api.outbound.guard_request_parameter_string(prompt)


def _https_url(tool_input: JSONObject, key: str, api: HostAPI) -> str:
    return provider_fetched_https_url(tool_input, key, api, provider="Seedance")


def _string_choice(tool_input: JSONObject, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = tool_input.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(f"Seedance tool_input.{key} must be one of {', '.join(allowed)}.")
    return value


def _duration_seconds(tool_input: JSONObject) -> int:
    value = tool_input.get("duration_seconds")
    if value is None:
        return DEFAULT_DURATION_SECONDS
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 2:
            raise ToolInputValidationError("Seedance tool_input.duration_seconds is outside the supported range.")
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError("Seedance tool_input.duration_seconds must be an integer or digit string.")
    if not MIN_DURATION_SECONDS <= value <= MAX_DURATION_SECONDS:
        raise ToolInputValidationError(
            f"Seedance duration_seconds must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS}."
        )
    return value


def _generate_audio(tool_input: JSONObject) -> bool:
    # A real JSON boolean, matching the schema the host validates against before
    # this runs (and the other bundled tools' boolean fields). Numeric options
    # take digit strings because agents commonly send numbers that way; booleans
    # have no such ambiguity, so accepting "true" here would only widen the
    # contract past what the schema admits.
    value = tool_input.get("generate_audio")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ToolInputValidationError("Seedance tool_input.generate_audio must be true or false.")
    return value


def _optional_seed(tool_input: JSONObject) -> int | None:
    value = tool_input.get("seed")
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        digits = value.strip()
        if len(digits.lstrip("-")) > 10:
            raise ToolInputValidationError("Seedance tool_input.seed must be between 0 and 4294967295.")
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError("Seedance tool_input.seed must be an integer or digit string.")
    if not 0 <= value <= 4_294_967_295:
        raise ToolInputValidationError("Seedance tool_input.seed must be between 0 and 4294967295.")
    return value


def _generation_request(api: HostAPI, tool_input: JSONObject) -> JSONObject:
    """Build the ModelArk create-task body for generate_video."""
    extra = set(tool_input) - {
        "prompt", "image_url", "resolution", "ratio", "duration_seconds", "generate_audio", "seed"
    }
    if extra:
        raise ToolInputValidationError(
            "Seedance generate_video only supports prompt, image_url, resolution, ratio, "
            "duration_seconds, generate_audio, and seed."
        )
    # Typed as the JSON value it becomes, not list[JSONObject]: the latter is not
    # assignable into a JSONObject entry, since the element types are invariant.
    content: list[JSONValue] = [{"type": "text", "text": _prompt_text(tool_input, api)}]
    has_image = tool_input.get("image_url") is not None
    if has_image:
        # "role" is a sibling of "image_url", not a key inside it. Without it
        # ModelArk does not read the image as the frame to animate, which is what
        # this action advertises.
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _https_url(tool_input, "image_url", api)},
                "role": "first_frame",
            }
        )
    body: JSONObject = {
        "model": MODEL_ID,
        "content": content,
        "resolution": _string_choice(tool_input, "resolution", RESOLUTIONS, DEFAULT_RESOLUTION),
        "ratio": _string_choice(
            tool_input, "ratio", RATIOS, DEFAULT_IMAGE_RATIO if has_image else DEFAULT_RATIO
        ),
        "duration": _duration_seconds(tool_input),
        "generate_audio": _generate_audio(tool_input),
    }
    seed = _optional_seed(tool_input)
    if seed is not None:
        body["seed"] = seed
    return body


def _task_id(tool_input: JSONObject) -> str:
    value = tool_input.get("task_id")
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value.strip()):
        raise ToolInputValidationError("Seedance tool_input.task_id must be a valid task id string.")
    return value.strip()


def _output_url(response: JSONObject) -> str:
    """ModelArk returns the finished video under content.video_url.

    The URL is checked structurally, not against a host allowlist, and that is a
    deliberate repeat of the decision recorded for Runway in
    docs/audit-reports/06-security-tools.md (TOOL-003): this value arrives only
    inside ModelArk's own authenticated HTTPS response, an operator who enables
    this tool already trusts ModelArk with the media, and ModelArk does not
    document which object-store/CDN hosts serve output, so pinning a suffix would
    reject legitimate traffic. It is not an SSRF boundary; the tools service's own
    egress policy is.
    """
    content = response.get("content")
    if not isinstance(content, dict):
        return ""
    url = content.get("video_url")
    if not isinstance(url, str) or not is_public_https_url(url):
        return ""
    return url


def _billed_tokens(response: JSONObject) -> int | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    return None


def _task_status(response: JSONObject) -> str:
    """The provider's status, narrowed to the documented set.

    Anything else becomes "unknown" rather than being echoed: this value reaches
    active model context both on its own and inside the message, so a malformed
    or hostile provider body must not be able to put arbitrary text there.
    """
    raw_status = response.get("status")
    if not isinstance(raw_status, str):
        return "unknown"
    status = raw_status.strip().lower()
    return status if status in TASK_STATUSES else "unknown"


def _task_result(response: JSONObject, task_id: str) -> JSONObject:
    # task_id is the validated id this host asked about, not the provider's echo
    # of it, for the same reason the status is narrowed above.
    status = _task_status(response)
    result: JSONObject = {
        "status": "success_executed",
        "task_id": task_id,
        "task_status": status,
    }
    tokens = _billed_tokens(response)
    if tokens is not None:
        result["billed_tokens"] = tokens
    if status == TERMINAL_SUCCESS:
        output_url = _output_url(response)
        if output_url:
            result["video_url"] = output_url
            result["message"] = (
                "Generation succeeded. The video_url is a temporary link valid for about 24 hours; "
                "save_video or hand off the URL promptly."
            )
        else:
            result["message"] = "ModelArk reported success but returned no output URL. Submit a new task."
    elif status == "failed":
        error = response.get("error")
        raw_code = error.get("code") if isinstance(error, dict) else None
        code = (
            f" (code: {raw_code})"
            if isinstance(raw_code, str) and FAILURE_CODE_RE.fullmatch(raw_code)
            else ""
        )
        result["message"] = (
            f"Seedance generation failed{code}. Content moderation and unsupported inputs both land "
            "here; adjust the prompt or reference image and submit a new task."
        )
    elif status == "cancelled":
        result["message"] = "ModelArk task was cancelled. Submit a new task."
    elif status == "expired":
        # Terminal, like failed and cancelled: an expired task never produces a
        # URL, so the agent must not be told to keep polling it.
        result["message"] = (
            "ModelArk task expired before it was collected and has no output. Submit a new task."
        )
    elif status == "unknown":
        result["message"] = (
            "ModelArk returned an unrecognized task status. Poll get_task again in ~15 seconds."
        )
    else:
        result["message"] = f"ModelArk task is {status}. Poll get_task again in ~15 seconds."
    return result


# ModelArk reports both "no such model" and "this account has not activated the
# model" as 404s. Recognized so the setup path gets the actionable message.
_MODEL_ERROR_MARKERS = (b"ModelNotOpen", b"ModelNotFound", b"model not found", b"not open")


def _failure_from_status(exc: WebRequestError, *, creating: bool = False) -> str:
    if exc.status == 401:
        return "ModelArk rejected the configured API key."
    if exc.status == 403:
        return (
            "ModelArk denied the request. The account may not have Seedance 2.5 activated, or the "
            "key may lack access to the video generation service."
        )
    if exc.status == 404:
        # A missing model and a missing task share this status. A create-task
        # request names no task, so its 404 is always about the model or this
        # account's access to it; only a lookup can genuinely miss a task. The
        # body markers stay as a second signal for the lookup paths.
        if creating or any(marker in exc.body for marker in _MODEL_ERROR_MARKERS):
            return (
                "ModelArk did not accept the Seedance model. Activate Seedance 2.5 for this "
                "account in the ModelArk console, and check that the account is on the "
                "international region this tool calls."
            )
        return "ModelArk task was not found."
    if exc.status == 429:
        return "ModelArk rate limit or account quota was reached."
    if exc.status in {400, 422}:
        return (
            "ModelArk rejected the request. Check that the resolution, aspect ratio, and duration "
            "are a supported combination."
        )
    if exc.status:
        return f"ModelArk API returned HTTP {exc.status}."
    # A transport failure with no curated mapping is a Host warning rather than a
    # vague message to the agent: raising here records it with routing metadata
    # only, leaving the provider body confined to the WebRequestError.
    message = known_provider_transport_error(exc)
    if not message:
        raise unmapped_provider_error("ModelArk", "API", exc) from None
    return message


def _download_failure(exc: WebRequestError) -> str:
    """Failures fetching the finished video from ModelArk's output URL.

    Kept apart from the API status mapping: by this point the task lookup has
    already succeeded, so an object-store or CDN error says nothing about the API
    key, the model, or the task. Output URLs expire in about a day, which makes
    the expiry cases the ones worth naming.
    """
    if exc.status in {401, 403}:
        return (
            "ModelArk no longer authorizes this video URL, which usually means it expired. "
            "Poll get_task for a fresh URL, and submit a new task if the output is gone."
        )
    if exc.status == 404:
        return "ModelArk's video output is no longer available; it likely expired. Submit a new task."
    if exc.status:
        return f"Seedance video download failed with HTTP {exc.status}."
    message = known_provider_transport_error(exc)
    if not message:
        raise unmapped_provider_error("ModelArk", "video download", exc) from None
    return message


def _get_task(task_id: str, headers: dict[str, str]) -> JSONObject:
    return json_request(
        "GET",
        f"{TASKS_ENDPOINT}/{urllib.parse.quote(task_id, safe='')}",
        headers=headers,
        failure_message="ModelArk API request failed.",
        invalid_response_message="ModelArk API returned an invalid response.",
    )


def _save_video(task_id: str, headers: dict[str, str]) -> ActionResult:
    response = _get_task(task_id, headers)
    status = _task_status(response)
    if status in TERMINAL_FAILURES:
        # Telling the caller to poll would be telling it to wait for a video that
        # is never coming, the same loop get_task avoids for these states.
        return ActionFailed(
            f"Seedance task is {status} and has no video to save. Submit a new task."
        )
    if status != TERMINAL_SUCCESS:
        return ActionFailed("Seedance video is not complete. Poll get_task and try again after it succeeds.")
    output_url = _output_url(response)
    if not output_url:
        return ActionFailed("ModelArk reported success but returned no valid video URL.")

    def open_video():
        return open_downloaded_video(
            output_url,
            provider="Seedance",
            filename_stem=f"seedance-{task_id}",
            map_failure=_download_failure,
        )

    return StreamingAsset(open_video)


class SeedanceTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            api_key = api.config["SEEDANCE_ARK_API_KEY"]
            headers = {"authorization": f"Bearer {api_key}"}
            if action == "generate_video":
                body = _generation_request(api, tool_input)
                response = json_request(
                    "POST",
                    TASKS_ENDPOINT,
                    headers=headers,
                    body=body,
                    failure_message="ModelArk API request failed.",
                    invalid_response_message="ModelArk API returned an invalid response.",
                )
                task_id = response.get("id")
                if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
                    return ActionFailed("ModelArk API returned no task id.")
                return ActionExecuted(
                    {
                        "status": "success_executed",
                        "message": "Seedance task created. Poll get_task until it succeeds.",
                        "task_id": task_id,
                        "task_status": "queued",
                        "model": MODEL_ID,
                        "output_kind": "video",
                    }
                )
            if action == "get_task":
                extra = set(tool_input) - {"task_id"}
                if extra:
                    raise ToolInputValidationError("Seedance get_task only supports task_id.")
                task_id = _task_id(tool_input)
                return ActionExecuted(_task_result(_get_task(task_id, headers), task_id))
            if action == "save_video":
                if set(tool_input) != {"task_id"}:
                    raise ToolInputValidationError("Seedance save_video requires exactly one string task_id.")
                return _save_video(_task_id(tool_input), headers)
            return ActionFailed("Unsupported Seedance action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            return ActionFailed(_failure_from_status(exc, creating=action == "generate_video"))
        except (ValueError, RuntimeError) as exc:
            # The tool's own errors (validation, config-unset) carry curated,
            # secret-free messages; an unexpected exception must not leak its
            # raw text (e.g. internal filesystem paths) to the agent.
            return ActionFailed(str(exc) or "Seedance tool request failed.")
        except Exception:
            return ActionFailed("Seedance tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Seedance has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = SeedanceTool()
