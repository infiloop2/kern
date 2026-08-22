"""OpenAI image generation tool package (OpenAI Images API, GPT Image models).

One action, `generate_image`, over OpenAI's synchronous Images API. Unlike
Runway's task queue, OpenAI has no task ids and no hosted output URLs: GPT
Image models always answer with the image itself, base64-encoded inside the
JSON response. So the whole result of a call is one file, and this package
returns it as a `StreamingAsset` — the host relays it to the agent-side shim,
which writes it under `/tool_assets` in the agent workspace and hands the agent
back only a path. The base64 payload never enters model context.

Reference images (image-to-image editing) travel the same private path every
other agent-supplied media uses: the agent calls `stage_image` with a workspace
path, the shim streams the bytes into the tools service, and this package
receives only an opaque, tool-scoped asset id it can stream to the
multipart edits endpoint. No agent-controlled pathname crosses the boundary.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import io
import json
import re
import secrets
from typing import Iterator, cast

from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL
from host.tools.json_types import JSONObject
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
    ActionFailed,
    ActionResult,
    ApprovalResult,
    OpenedStreamingAsset,
    StreamingAsset,
)
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.web import (
    WebRequestError,
    json_request,
    known_provider_transport_error,
    stream_request_bytes,
    unmapped_provider_error,
)

OPENAI_API_BASE = "https://api.openai.com"
GENERATIONS_ENDPOINT = f"{OPENAI_API_BASE}/v1/images/generations"
# Reference images route to the edits endpoint, which is multipart rather than
# JSON. It is the same generation capability with input images attached.
EDITS_ENDPOINT = f"{OPENAI_API_BASE}/v1/images/edits"

# The host parameter guard bounds free text at 1,024 bytes; reject a longer
# prompt here with a specific message rather than letting the guard's generic
# size denial explain it. (OpenAI itself accepts far longer prompts.)
MAX_PROMPT_CHARS = 1_000

# One stable contract across every exposed model. GPT Image 2 also accepts
# arbitrary width x height, but admitting that would put a per-model size
# matrix in the tool schema that can drift from provider support independently
# of this file; these three sizes are accepted by all three models.
SUPPORTED_MODELS = ("gpt-image-2", "gpt-image-1.5", "gpt-image-1-mini")
DEFAULT_MODEL = "gpt-image-2"
SIZES = ("1024x1024", "1536x1024", "1024x1536")
DEFAULT_SIZE = "1024x1024"
QUALITIES = ("low", "medium", "high")
# Low by default: quality is the dominant cost *and* latency lever, and a tool
# call still has a bounded lifetime (see REQUEST_TIMEOUT_SECONDS).
DEFAULT_QUALITY = "low"
OUTPUT_FORMATS = ("png", "jpeg", "webp")
DEFAULT_OUTPUT_FORMAT = "png"
FORMAT_MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
FORMAT_SUFFIXES = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
# Magic bytes, checked against the requested format: the media type is emitted
# as a response header, so it must describe the bytes actually returned.
FORMAT_SIGNATURES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}

# The formats the staging store accepts and OpenAI takes as edit inputs, with
# the generated part filename extension each one gets on the wire.
REFERENCE_MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_REFERENCE_IMAGES = 4
# OpenAI accepts input images up to 50 MB; this tighter bound keeps one
# multipart request (and the tools service's memory) predictable.
MAX_REFERENCE_BYTES = 25_000_000
MAX_TOTAL_REFERENCE_BYTES = 60_000_000

# Image generation is the slowest synchronous provider call this host makes: a
# high-quality render can take minutes, which is why the MCP shim's per-call
# budget is 300 seconds. Stop short of it so a render that will not finish
# returns this package's own failure instead of dying in the shim's socket.
REQUEST_TIMEOUT_SECONDS = 280
# Base64 inflates bytes by 4/3 and the image rides inside the JSON body.
MAX_RESPONSE_BODY_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 20_000_000
MIN_IMAGE_BYTES = 256

# Only a machine-shaped provider error code is ever surfaced; the provider's
# free-form message (which can echo attacker-influenced prompt text) is not.
ERROR_CODE_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")

OPENAI_IMAGE_POLICY = (
    "The prompt and rendering options (model, size, quality, output format) supplied by "
    "the user or agent are sent to OpenAI's Images API and billed to the deployment's "
    "OpenAI project. When the agent supplies reference images, those image bytes are "
    "uploaded too. The prompt first passes the host parameter guard. This action runs "
    "directly with no approval and publishes nothing: the generated image is streamed "
    "into the agent workspace under /tool_assets, and only its path is returned to "
    "active model context."
)


MANIFEST = ToolManifest(
    tool_id="openai_images",
    display_name="OpenAI Image Generation",
    description=(
        "Connect an OpenAI API key and let your agent generate images with the GPT Image "
        "models, saved straight into the agent workspace."
    ),
    connection="enable_only",
    actions=(
        ActionSpec(
            id="generate_image",
            description=(
                "Generate one image with OpenAI's GPT Image models and save it under "
                "/tool_assets in the agent workspace; the result is the durable file path, "
                "not image data. Optionally pass staged workspace images as references to "
                "edit or combine them. The call is synchronous and returns only when the "
                "render is done: low quality takes seconds, high quality at the larger "
                "sizes can take minutes. It spends OpenAI credits and publishes nothing."
            ),
            data_policy=OPENAI_IMAGE_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to render, or how to edit the reference images (up to 1000 chars).",
                    },
                    "model": {
                        "type": "string",
                        "enum": list(SUPPORTED_MODELS),
                        "description": "GPT Image model (default gpt-image-2, the strongest; gpt-image-1-mini is the cheapest and fastest).",
                    },
                    "size": {
                        "type": "string",
                        "enum": list(SIZES),
                        "description": "Output resolution: square 1024x1024 (default), landscape 1536x1024, or portrait 1024x1536.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": list(QUALITIES),
                        "description": "Rendering quality (default low); higher quality costs more and takes considerably longer.",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": list(OUTPUT_FORMATS),
                        "description": "File format of the saved image: png (default), jpeg, or webp.",
                    },
                    "image_asset_ids": {
                        "type": "array",
                        "maxItems": MAX_REFERENCE_IMAGES,
                        "items": {"type": "string"},
                        "description": "Built-in references for up to 4 JPEG, PNG, or WebP images staged from the agent workspace with stage_image (for_tool=openai_images). Supplying any switches the call to image editing; each staged copy is consumed by the call.",
                    },
                },
                "additionalProperties": False,
            },
            # The whole result is one binary file, so there is no JSON result to
            # describe: the host returns the streamed asset instead.
            returns_asset=True,
        ),
    ),
    config=(
        ConfigRequirement(
            key="OPENAI_API_KEY",
            description="OpenAI API key (project-scoped) from platform.openai.com, for an organization verified for the GPT Image models.",
        ),
    ),
    protections=(
        "Your OpenAI key stays in write-only tool config. The agent never sees it, and this tool reaches only OpenAI's image endpoints — no chat, assistants, or file APIs.",
        "Generated images are written to /tool_assets in the agent workspace for review; Kern publishes them nowhere. Reference images are uploaded only when the agent passes a staged image into a call, and the staged copy is deleted once OpenAI accepts it.",
        PARAM_GUARD_PROTECTION,
    ),
    technical_details=(
        "GPT Image models return the image as base64 inside the API response rather than a hosted URL. The tools service decodes it, checks the bytes against the requested format, and streams the file to the agent-side bridge, which writes it under /tool_assets with a host-generated name. The image data never enters the agent's model context or the tool audit log.",
        PARAM_GUARD_TECHNICAL_DETAIL,
    ),
    setup_steps=(
        SetupStep(
            title="Create an OpenAI API organization and verify it",
            description="Sign in at platform.openai.com and choose the organization that should own all agent-generated images. Under Settings > Organization > General, complete API organization verification: OpenAI gates the GPT Image models behind it, and an unverified organization gets HTTP 403 on every image call. Verification can take up to 30 minutes to take effect. This is a platform API organization, separate from any ChatGPT subscription and from the Codex network integration on this host.",
            link_url="https://platform.openai.com/settings/organization/general",
            link_label="Open OpenAI organization settings",
        ),
        SetupStep(
            title="Fund the project and create an API key",
            description="Add credits to the organization under Billing, then create a project-scoped secret key under API keys and copy it immediately into a password manager or Kern. Image generation is billed per image by quality: roughly a few cents at low quality and several times that at high. Keep the key scoped to this project so its spend and revocation are independent of anything else you run.",
            link_url="https://platform.openai.com/api-keys",
            link_label="Open the OpenAI API keys page",
        ),
        SetupStep(
            title="Check whether the organization shares data with OpenAI",
            description="Open Settings > Data controls > Sharing and confirm 'Share inputs and outputs with OpenAI' is Disabled for the project holding this key. It is off by default, but OpenAI offers complimentary daily tokens for turning it on, so an organization that took that offer is sharing every prompt and generated image for model improvement. The setting is per project, so you can leave sharing on elsewhere and disable it for the project your agent uses. Changing it needs organization owner permissions.",
            link_url="https://platform.openai.com/settings/organization/data-controls/sharing",
            link_label="Open OpenAI data controls",
        ),
        SetupStep(
            title="Configure and enable OpenAI Image Generation",
            show_config=True,
            description="Open OpenAI Image Generation under Home > Integrations, save the key as OPENAI_API_KEY, then enable the tool. There is no OAuth step. Never place the key in a prompt, a filename, or a URL.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(
                        label="Generation requests",
                        text="The prompt and the rendering options (model, size, quality, output format) go to OpenAI. The prompt is agent- or user-supplied free text and first passes the host parameter guard (see Technical notes), which denies secret- or credential-shaped values before anything is sent.",
                    ),
                    DataSummaryPoint(
                        label="Reference images",
                        text="When the agent passes staged workspace images into a call, their bytes are uploaded to OpenAI as the images to edit. Their local workspace paths and filenames are not sent; each upload is labeled with a generic name.",
                    ),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(
                        label="OpenAI only",
                        text="Requests go to api.openai.com and nowhere else. This tool calls only the image generation and image edit endpoints; it cannot reach chat, assistants, files, or any other OpenAI API.",
                    ),
                    DataSummaryPoint(
                        label="OpenAI's processors",
                        text="OpenAI runs the models on its own infrastructure and may involve its subprocessors and a limited set of reviewers for abuse monitoring; it does not forward the prompt to another model vendor.",
                    ),
                ),
            ),
            DataSummaryCard(
                title="What OpenAI can do with it",
                description=(
                    "API inputs and outputs are not used to train OpenAI's models by default, but "
                    "that default is a per-organization setting an owner can change: under Settings > "
                    "Data controls > Sharing, 'Share inputs and outputs with OpenAI' can be enabled "
                    "for all or selected projects, and OpenAI offers complimentary daily tokens in "
                    "exchange. While it is on for the project holding this key, every prompt and "
                    "generated image here is shared for model improvement, so check it before "
                    "enabling this tool. OpenAI screens prompts and generated images against its "
                    "usage policies and can refuse a request. Retained data is accessible to "
                    "authorized OpenAI staff and vetted reviewers for abuse investigation, support, "
                    "and legal compliance."
                ),
                links=(
                    DataSummaryLink(label="Check your organization's data sharing setting", url="https://platform.openai.com/settings/organization/data-controls/sharing"),
                    DataSummaryLink(label="What sharing inputs and outputs means", url="https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai"),
                    DataSummaryLink(label="OpenAI enterprise privacy and API data commitments", url="https://openai.com/enterprise-privacy/"),
                    DataSummaryLink(label="OpenAI usage policies", url="https://openai.com/policies/usage-policies/"),
                    DataSummaryLink(label="OpenAI privacy policy", url="https://openai.com/policies/privacy-policy/"),
                ),
            ),
            DataSummaryCard(
                title="How long OpenAI retains it",
                description=(
                    "By default OpenAI retains API inputs and outputs for up to 30 days for abuse "
                    "monitoring and then deletes them, unless it is legally required to keep them "
                    "longer. Organizations approved for Zero Data Retention on eligible endpoints "
                    "are not logged at all. Generated images are returned inline in the response, "
                    "so OpenAI hosts no link to them; the copy that persists is the one Kern writes "
                    "into the agent workspace."
                ),
                links=(
                    DataSummaryLink(label="OpenAI data controls documentation", url="https://platform.openai.com/docs/guides/your-data"),
                ),
            ),
        ),
    ),
    agent_notes=(
        "One image per call, so ask again for variants or alternates. To edit or combine "
        "existing images, first call stage_image with for_tool=openai_images for each one "
        "and pass the returned ids as image_asset_ids; a staged id is consumed by the call "
        "that uses it, so stage again for the next edit. The result is a workspace file "
        "path under /tool_assets, not image data: read, show, or stage that path for "
        "another tool rather than expecting bytes back, and there is no separate save "
        "step. If a call fails with no provider status, the render likely outran the "
        "call budget; retry at a lower quality or a smaller size."
    ),
)


def _string_choice(tool_input: JSONObject, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = tool_input.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(
            f"OpenAI Images tool_input.{key} must be one of {', '.join(allowed)}."
        )
    return value


def _prompt_text(tool_input: JSONObject, api: HostAPI) -> str:
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolInputValidationError("OpenAI Images tool_input.prompt is required.")
    prompt = prompt.strip()
    # Reject rather than truncate: a silently cut prompt would spend credits
    # rendering an input the agent did not ask for.
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(
            f"OpenAI Images prompt must be at most {MAX_PROMPT_CHARS} characters."
        )
    return api.outbound.guard_request_parameter_string(prompt)


def _reference_asset_ids(tool_input: JSONObject) -> list[str]:
    value = tool_input.get("image_asset_ids")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolInputValidationError(
            "OpenAI Images tool_input.image_asset_ids must be an array of staged image ids."
        )
    if len(value) > MAX_REFERENCE_IMAGES:
        raise ToolInputValidationError(
            f"OpenAI Images accepts at most {MAX_REFERENCE_IMAGES} reference images."
        )
    asset_ids: list[str] = []
    for asset_id in value:
        if not isinstance(asset_id, str) or not asset_id:
            raise ToolInputValidationError(
                "OpenAI Images tool_input.image_asset_ids entries must be non-empty strings."
            )
        if asset_id in asset_ids:
            raise ToolInputValidationError(
                "OpenAI Images tool_input.image_asset_ids must not repeat an image."
            )
        asset_ids.append(asset_id)
    return asset_ids


def _request_fields(api: HostAPI, tool_input: JSONObject) -> dict[str, str]:
    """The parameters both endpoints share, as strings (the edits endpoint is
    multipart, where every field is text; the JSON endpoint takes the same
    values verbatim)."""
    extra = set(tool_input) - {
        "prompt", "model", "size", "quality", "output_format", "image_asset_ids"
    }
    if extra:
        raise ToolInputValidationError(
            "OpenAI Images generate_image only supports prompt, model, size, quality, "
            "output_format, and image_asset_ids."
        )
    return {
        "model": _string_choice(tool_input, "model", SUPPORTED_MODELS, DEFAULT_MODEL),
        "prompt": _prompt_text(tool_input, api),
        "size": _string_choice(tool_input, "size", SIZES, DEFAULT_SIZE),
        "quality": _string_choice(tool_input, "quality", QUALITIES, DEFAULT_QUALITY),
        "output_format": _string_choice(
            tool_input, "output_format", OUTPUT_FORMATS, DEFAULT_OUTPUT_FORMAT
        ),
        # One image per call: the result of a call is one file.
        "n": "1",
    }


def _reference_parts(api: HostAPI, asset_ids: list[str]) -> list[tuple[str, str, int]]:
    """Validate every staged reference up front, before any bytes are sent."""
    parts: list[tuple[str, str, int]] = []
    total = 0
    for asset_id in asset_ids:
        metadata = api.assets.describe(asset_id)
        if metadata.media_type not in REFERENCE_MEDIA_TYPES:
            raise ToolInputValidationError(
                "OpenAI Images image_asset_ids must refer to staged JPEG, PNG, or WebP images."
            )
        if metadata.size_bytes > MAX_REFERENCE_BYTES:
            raise ToolInputValidationError(
                f"OpenAI Images reference images must be at most {MAX_REFERENCE_BYTES} bytes."
            )
        total += metadata.size_bytes
        if total > MAX_TOTAL_REFERENCE_BYTES:
            raise ToolInputValidationError(
                "OpenAI Images reference images exceed the combined upload limit."
            )
        parts.append((asset_id, metadata.media_type, metadata.size_bytes))
    return parts


def _multipart_segments(
    fields: dict[str, str], references: list[tuple[str, str, int]], boundary: str
) -> tuple[list[bytes | str], int]:
    """Build the multipart body as literal byte segments plus asset ids to
    stream in place, with the exact Content-Length.

    Nothing here is provider- or filename-controlled: field names are fixed
    literals, the only free text is the (guarded, bounded) prompt sitting in a
    part *body* where newlines are content, and each reference is labeled with
    a generated name rather than the agent's workspace filename. So no input
    can forge a part header or escape the random boundary.
    """
    segments: list[bytes | str] = []
    length = 0

    def add(chunk: bytes) -> None:
        nonlocal length
        segments.append(chunk)
        length += len(chunk)

    for name, value in fields.items():
        add(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for index, (asset_id, media_type, size_bytes) in enumerate(references, start=1):
        suffix = REFERENCE_MEDIA_TYPES[media_type]
        add(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="image[]"; filename="reference-{index}{suffix}"\r\n'
                f"Content-Type: {media_type}\r\n\r\n"
            ).encode("utf-8")
        )
        segments.append(asset_id)
        length += size_bytes
        add(b"\r\n")
    add(f"--{boundary}--\r\n".encode("ascii"))
    return segments, length


def _multipart_stream(api: HostAPI, segments: list[bytes | str]) -> Iterator[bytes]:
    for segment in segments:
        if isinstance(segment, bytes):
            yield segment
            continue
        with api.assets.open(segment) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk


def _generate(
    api: HostAPI, headers: dict[str, str], tool_input: JSONObject
) -> tuple[JSONObject, list[str], str]:
    """Call the endpoint the input selects.

    Returns the provider response, the staged asset ids the call consumed, and
    the output format the response must be decoded as.
    """
    fields = _request_fields(api, tool_input)
    output_format = fields["output_format"]
    asset_ids = _reference_asset_ids(tool_input)
    if not asset_ids:
        body: JSONObject = {}
        body.update(fields)
        # The multipart form carries every value as text; JSON keeps n numeric.
        body["n"] = 1
        return (
            json_request(
                "POST",
                GENERATIONS_ENDPOINT,
                headers=headers,
                body=body,
                failure_message="OpenAI image generation failed.",
                invalid_response_message="OpenAI returned an invalid image response.",
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_bytes=MAX_RESPONSE_BODY_BYTES,
            ),
            [],
            output_format,
        )
    references = _reference_parts(api, asset_ids)
    boundary = f"kern-{secrets.token_hex(16)}"
    segments, content_length = _multipart_segments(fields, references, boundary)
    raw = stream_request_bytes(
        "POST",
        EDITS_ENDPOINT,
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=_multipart_stream(api, segments),
        content_length=content_length,
        failure_message="OpenAI image edit failed.",
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_bytes=MAX_RESPONSE_BODY_BYTES,
    )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("OpenAI returned an invalid image response.") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("OpenAI returned an invalid image response.")
    return cast(JSONObject, decoded), asset_ids, output_format


def _image_bytes(response: JSONObject, output_format: str) -> bytes:
    """Decode the single returned image and prove it is the format requested.

    GPT Image models answer only in base64 (`url` is a DALL-E-era field), so
    this is the one place the bytes exist before they are streamed out.
    """
    data = response.get("data")
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    encoded = first.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("OpenAI returned no image data. Submit the request again.")
    if len(encoded) > MAX_RESPONSE_BODY_BYTES:
        raise RuntimeError("OpenAI returned an image larger than the supported size.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("OpenAI returned undecodable image data.") from exc
    if not MIN_IMAGE_BYTES <= len(raw) <= MAX_IMAGE_BYTES:
        raise RuntimeError("OpenAI returned an image outside the supported size range.")
    if not any(raw.startswith(signature) for signature in FORMAT_SIGNATURES[output_format]):
        raise RuntimeError(f"OpenAI returned data that is not a {output_format} image.")
    return raw


def _saved_image(raw: bytes, output_format: str) -> StreamingAsset:
    """Hand the decoded image to the host as this call's exclusive result.

    The bytes are already in hand, so the lazy open only wraps them: the
    provider call and its error mapping stay in ``execute``, where a failure
    becomes a normal ActionFailed the agent can read.
    """
    filename = f"openai-image-{secrets.token_hex(8)}{FORMAT_SUFFIXES[output_format]}"
    media_type = FORMAT_MEDIA_TYPES[output_format]

    @contextmanager
    def open_image() -> Iterator[OpenedStreamingAsset]:
        yield OpenedStreamingAsset(
            filename=filename,
            media_type=media_type,
            size_bytes=len(raw),
            source=io.BytesIO(raw),
        )

    return StreamingAsset(open_image)


def _provider_error_code(body: bytes) -> str:
    """The provider's machine-readable error code, or "".

    Only a strict code token is surfaced. OpenAI's ``error.message`` can echo
    prompt text back, so it never reaches the agent or the audit log.
    """
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ""
    error = decoded.get("error") if isinstance(decoded, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and ERROR_CODE_RE.fullmatch(code):
        return code
    return ""


def _failure_from_status(exc: WebRequestError) -> str:
    code = _provider_error_code(exc.body)
    suffix = f" (code: {code})" if code else ""
    if exc.status == 401:
        return "OpenAI rejected the configured API key."
    if exc.status == 403:
        return (
            "OpenAI denied the request. The GPT Image models require a verified API "
            f"organization; ask the operator to verify it in the OpenAI dashboard.{suffix}"
        )
    if exc.status == 404:
        return f"OpenAI does not expose the requested image model to this key.{suffix}"
    if exc.status == 429:
        return "OpenAI rate limit or billing quota was reached. Retry later or add credits."
    if exc.status in {400, 422}:
        return (
            "OpenAI rejected the request. The prompt may have been refused by its content "
            f"filters, or the model, size, and quality combination is unsupported.{suffix}"
        )
    if exc.status >= 500:
        return f"OpenAI reported a server error (HTTP {exc.status}). Submit the request again."
    if exc.status:
        return f"OpenAI API returned HTTP {exc.status}.{suffix}"
    # No status: the request never produced an HTTP response. A render that
    # outran REQUEST_TIMEOUT_SECONDS looks the same here as a connection
    # failure, so the two share one message rather than guessing which it was.
    # An otherwise unmapped transport failure is a Host warning instead of a
    # vague agent message, as in every other provider package.
    message = known_provider_transport_error(exc)
    if not message:
        raise unmapped_provider_error("OpenAI", "images", exc) from None
    return message


class OpenAIImagesTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action != "generate_image":
                return ActionFailed("Unsupported OpenAI Images action.")
            api_key = api.config["OPENAI_API_KEY"]
            headers = {"authorization": f"Bearer {api_key}"}
            response, consumed, output_format = _generate(api, headers, tool_input)
            raw = _image_bytes(response, output_format)
            # Consume the staged inputs only once OpenAI has returned a usable
            # image: a failed or malformed response leaves them available for a
            # clean retry.
            for asset_id in consumed:
                api.assets.delete(asset_id)
            return _saved_image(raw, output_format)
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            return ActionFailed(_failure_from_status(exc))
        except (ValueError, RuntimeError) as exc:
            # The tool's own errors (validation, config-unset, staged-asset
            # lookup) carry curated, secret-free messages; an unexpected
            # exception must not leak its raw text to the agent.
            return ActionFailed(str(exc) or "OpenAI image request failed.")
        except Exception:
            return ActionFailed("OpenAI image request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("OpenAI Images has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = OpenAIImagesTool()
