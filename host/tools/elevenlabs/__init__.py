"""Direct ElevenLabs audio generation and original voice design.

Provider contract: https://elevenlabs.io/docs/api-reference/introduction
No SDK, public URLs, provider credential sharing, or automatic retries.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
import json
import math
import re
import secrets
from typing import Iterator, cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.cost_reporting import report_priced_units
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, ApprovalResult, OpenedStreamingAsset, StreamingAsset
from host.tools.shared.inputs import clip
from host.tools.shared.web import (
    WebRequestError, encode_query, json_request, json_request_with_headers, open_response_stream,
    transport_or_unmapped_provider_error,
)
from .manifest import MANIFEST

API_ROOT = "https://api.elevenlabs.io"
TIMEOUT = 280
MAX_AUDIO_BYTES = 20_000_000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _keys(value: JSONObject, allowed: set[str]) -> None:
    if set(value) - allowed:
        raise ValueError("ElevenLabs input contains unsupported fields.")


def _id(value: JSONValue) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("ElevenLabs requires a valid voice or preview ID.")
    return value


def _text(value: JSONValue, api: HostAPI, *, maximum: int = 1000, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise ValueError(f"ElevenLabs text must be {'0' if empty else '1'} to {maximum} characters.")
    return api.outbound.guard_request_parameter_string(value) if value else value


def _number(value: JSONValue, low: float, high: float, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value) or (integer and not isinstance(value, int)):
        raise ValueError(f"ElevenLabs value must be a finite {'integer' if integer else 'number'} from {low} to {high}.")
    return value


def _boolean(value: JSONValue) -> bool:
    if not isinstance(value, bool):
        raise ValueError("ElevenLabs flag must be a boolean.")
    return value


def _choice(value: JSONValue, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError("ElevenLabs model must be one of the documented choices.")
    return value


def _styles(value: JSONValue, api: HostAPI) -> list[JSONValue]:
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("ElevenLabs styles must be an array of at most 12 directions.")
    return [_text(item, api, maximum=200) for item in value]


def _music_body(values: JSONObject, api: HostAPI) -> JSONObject:
    body: JSONObject = {
        "model_id": "music_v2_5",
        "store_for_inpainting": False,
    }
    if ("prompt" in values) == ("sections" in values):
        raise ValueError("ElevenLabs music needs exactly one of prompt or sections.")
    if "prompt" in values:
        body.update(prompt=_text(values["prompt"], api), music_length_ms=_number(values.get("duration_ms"), 3000, 300000, integer=True), force_instrumental=_boolean(values.get("force_instrumental", True)))
        return body
    if "duration_ms" in values or "force_instrumental" in values:
        raise ValueError("ElevenLabs duration_ms and force_instrumental apply only to prompt mode.")
    sections = values["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= 20:
        raise ValueError("ElevenLabs sections must contain 1 to 20 objects.")
    chunks: list[JSONValue] = []
    total = 0
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("ElevenLabs section must be an object.")
        _keys(section, {"text", "duration_ms", "positive_styles", "negative_styles"})
        duration = int(_number(section.get("duration_ms"), 3000, 120000, integer=True))
        script = _text(section.get("text"), api, empty=True)
        if len(script.splitlines()) > 30 or any(len(line) > 200 for line in script.splitlines()):
            raise ValueError("ElevenLabs music text allows 30 lines of at most 200 characters each.")
        chunk: JSONObject = {"text": script, "duration_ms": duration, "positive_styles": _styles(section.get("positive_styles"), api)}
        if "negative_styles" in section:
            chunk["negative_styles"] = _styles(section["negative_styles"], api)
        total += duration
        chunks.append(chunk)
    if not 3000 <= total <= 300000:
        raise ValueError("ElevenLabs music sections must total 3 seconds to 5 minutes.")
    body["composition_plan"] = {"chunks": chunks}
    return body


def _generation(action: str, values: JSONObject, api: HostAPI) -> tuple[str, JSONObject]:
    if action == "generate_music":
        return "/v1/music", _music_body(values, api)
    body: JSONObject = {"text": _text(values.get("text"), api)}
    if action == "generate_speech":
        voice_id = _id(values.get("voice_id"))
        model = _choice(values.get("model", "eleven_v3"), ("eleven_v3", "eleven_multilingual_v2"))
        body["model_id"] = model
        settings: JSONObject = {}
        for key in ("stability", "style", "similarity_boost", "speed"):
            if key in values:
                settings[key] = _number(values[key], 0.7 if key == "speed" else 0, 1.2 if key == "speed" else 1)
        if model == "eleven_v3" and "stability" in settings and settings["stability"] not in (0, 0.5, 1):
            raise ValueError("Eleven v3 stability must be 0 (creative), 0.5 (natural), or 1 (robust).")
        if settings:
            body["voice_settings"] = settings
        return f"/v1/text-to-speech/{voice_id}", body
    body.update(model_id="eleven_text_to_sound_v2", duration_seconds=_number(values.get("duration_seconds"), 0.5, 30), loop=_boolean(values.get("loop", False)))
    if "prompt_influence" in values:
        body["prompt_influence"] = _number(values["prompt_influence"], 0, 1)
    return "/v1/sound-generation", body


def _is_mp3(raw: bytes) -> bool:
    return raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 255 and raw[1] & 0xE0 == 0xE0)


def _save_generation(path: str, body: JSONObject | None, headers: dict[str, str], api: HostAPI) -> StreamingAsset:
    # ElevenLabs can return chunked audio without Content-Length. Read it once
    # under a fixed bound, then hand the existing bridge an exact byte count.
    output_format = "auto" if path == "/v1/music" else "mp3_44100_128"
    with open_response_stream(
        "GET" if body is None else "POST", API_ROOT + path + ("" if body is None else "?output_format=" + output_format),
        headers={**headers, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        data=None if body is None else json.dumps(body, allow_nan=False).encode(), timeout=TIMEOUT,
        failure_message="ElevenLabs audio generation failed.",
    ) as (source, response_headers):
        media_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"audio/mpeg", "application/octet-stream"}:
            raise ValueError("ElevenLabs returned an unsupported audio media type.")
        if body is not None:
            report_priced_units(api, response_headers.get("character-cost"), "0.0002")
        raw = source.read(MAX_AUDIO_BYTES + 1)
    if not 12 <= len(raw) <= MAX_AUDIO_BYTES or not _is_mp3(raw):
        raise ValueError("ElevenLabs returned invalid or oversized MP3 audio.")
    summary = "ElevenLabs audio saved."
    filename = f"elevenlabs-{secrets.token_hex(8)}.mp3"

    @contextmanager
    def open_audio() -> Iterator[OpenedStreamingAsset]:
        with io.BytesIO(raw) as source:
            yield OpenedStreamingAsset(filename=filename, media_type="audio/mpeg", size_bytes=len(raw), source=source, summary=summary)

    return StreamingAsset(open_audio)


def _design_voice(values: JSONObject, api: HostAPI, headers: dict[str, str]) -> ActionExecuted:
    description = _text(values.get("voice_description"), api)
    if len(description) < 20:
        raise ValueError("ElevenLabs voice descriptions must contain 20 to 1000 characters.")
    body: JSONObject = {
        "voice_description": description,
        "model_id": _choice(values.get("model", "eleven_ttv_v3"), ("eleven_ttv_v3", "eleven_multilingual_ttv_v2")),
        "stream_previews": True,
        "auto_generate_text": "text" not in values,
    }
    if "text" in values:
        script = _text(values["text"], api)
        if len(script) < 100:
            raise ValueError("ElevenLabs voice preview text must contain 100 to 1000 characters.")
        body["text"] = script
    if "should_enhance" in values:
        body["should_enhance"] = _boolean(values["should_enhance"])
    response, response_headers = json_request_with_headers("POST", API_ROOT + "/v1/text-to-voice/design?output_format=mp3_44100_128", headers=headers, body=body, timeout=TIMEOUT, max_bytes=65536, failure_message="ElevenLabs voice design failed.", invalid_response_message="ElevenLabs returned an invalid voice design response.")
    report_priced_units(api, response_headers.get("character-cost"), "0.0002")
    previews = response.get("previews")
    if not isinstance(previews, list) or not 1 <= len(previews) <= 10:
        raise ValueError("ElevenLabs returned an invalid voice preview list.")
    ids: list[JSONValue] = []
    for preview in previews:
        ids.append(_id(preview.get("generated_voice_id") if isinstance(preview, dict) else None))
    return ActionExecuted({"generated_voice_ids": ids, "text": clip(response.get("text"), 1000)})


def _save_voice(values: JSONObject, api: HostAPI, headers: dict[str, str]) -> ActionExecuted:
    description = _text(values.get("voice_description"), api)
    if len(description) < 20:
        raise ValueError("ElevenLabs voice descriptions must contain 20 to 1000 characters.")
    body: JSONObject = {
        "generated_voice_id": _id(values.get("generated_voice_id")),
        "voice_name": _text(values.get("name"), api, maximum=100),
        "voice_description": description,
    }
    response = json_request("POST", API_ROOT + "/v1/text-to-voice", headers=headers, body=body, timeout=TIMEOUT, max_bytes=65536, failure_message="ElevenLabs could not save the designed voice.", invalid_response_message="ElevenLabs returned an invalid saved voice response.")
    return ActionExecuted({"voice_id": _id(response.get("voice_id"))})


def _voices(values: JSONObject, api: HostAPI, headers: dict[str, str]) -> ActionExecuted:
    query = {"page_size": str(_number(values.get("page_size", 20), 1, 100, integer=True))}
    if "search" in values:
        query["search"] = _text(values["search"], api, maximum=200)
    if "next_page_token" in values:
        token = values["next_page_token"]
        if not isinstance(token, str) or not 1 <= len(token) <= 512:
            raise ValueError("ElevenLabs next_page_token must be 1 to 512 characters.")
        query["next_page_token"] = api.outbound.guard_request_parameter_string(token, allow_identifiers=True, allow_machine_tokens=True)
    response = json_request("GET", API_ROOT + "/v2/voices?" + encode_query(query), headers=headers, failure_message="ElevenLabs voice lookup failed.", invalid_response_message="ElevenLabs returned an invalid voice response.", max_bytes=2_000_000)
    rows = response.get("voices")
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("ElevenLabs returned an invalid voice list.")
    voices: list[JSONValue] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("ElevenLabs returned an invalid voice record.")
        voices.append({"voice_id": _id(row.get("voice_id")), "name": clip(row.get("name"), 200), "description": clip(row.get("description"), 1000), "category": clip(row.get("category"), 100)})
    token = response.get("next_page_token")
    if token is not None and (not isinstance(token, str) or len(token) > 512):
        raise ValueError("ElevenLabs returned an invalid pagination token.")
    return ActionExecuted({"voices": voices, "has_more": _boolean(response.get("has_more", False)), "next_page_token": token or ""})


def _failure(exc: WebRequestError) -> str:
    if exc.status == 401:
        return "ElevenLabs rejected the API key. Update it in Integrations."
    if exc.status in {402, 403}:
        return "ElevenLabs denied access. Check the API key permissions, plan access and available credits."
    if exc.status == 404:
        return "ElevenLabs could not find the requested voice or preview."
    if exc.status == 429:
        return "ElevenLabs rate or credit limit reached. No automatic retry was made."
    if exc.status in {400, 422}:
        return "ElevenLabs rejected the input or model settings. Check the action parameters."
    if exc.status >= 500:
        return "ElevenLabs is temporarily unavailable. The request may have consumed credits; no automatic retry was made."
    raise transport_or_unmapped_provider_error("ElevenLabs", "audio request", exc)


class ElevenLabsTool:
    manifest = MANIFEST
    credentials = None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        spec = MANIFEST.action(action)
        if spec is None:
            return ActionFailed("Unknown ElevenLabs action.")
        try:
            _keys(tool_input, set(cast(JSONObject, spec.input_schema["properties"])))
            headers = {"xi-api-key": api.config["ELEVENLABS_API_KEY"]}
            if action == "list_voices":
                return _voices(tool_input, api, headers)
            if action == "design_voice":
                return _design_voice(tool_input, api, headers)
            if action == "preview_voice":
                preview_id = _id(tool_input.get("generated_voice_id"))
                return _save_generation(f"/v1/text-to-voice/{preview_id}/stream", None, headers, api)
            if action == "save_voice":
                return _save_voice(tool_input, api, headers)
            path, body = _generation(action, tool_input, api)
            return _save_generation(path, body, headers, api)
        except KeyError:
            return ActionFailed("Set the ElevenLabs API key in Integrations.")
        except WebRequestError as exc:
            return ActionFailed(_failure(exc))
        except ValueError as exc:
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        return ActionFailed("ElevenLabs has no approval-gated actions.")


BUNDLED_TOOL = ElevenLabsTool()
