"""Provider-neutral tool-input validation primitives for tool packages.

Each bundled tool owns its provider-specific validation grammar; this module
holds the generic building blocks (bounded integers, clipped strings, the
common object-schema shape) so packages do not carry copy-pasted duplicates.
Error messages stay provider-prefixed through the ``provider``/``name``
parameters, so consolidating here changes no user-visible text.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, cast

from host.tools.json_types import JSONObject, JSONValue

if TYPE_CHECKING:  # avoids a runtime import cycle through host_api
    from host.tools.host_api import HostAPI


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def decoded_url_component_values(value: str, *, plus: bool) -> tuple[str, ...]:
    """Return every relevant nested-decoding view of one URL component.

    Query parsing applies form-style plus conversion at its outer layer, while
    application code may decode nested values again. Inspect percent-only and
    form-style interpretations so neither encoded pluses nor spaces hide data.
    """
    if not value:
        return ()
    roots = [urllib.parse.unquote(value, errors="replace")]
    if plus:
        form_root = urllib.parse.unquote_plus(value, errors="replace")
        if form_root not in roots:
            roots.append(form_root)
    values: list[str] = []
    for root in roots:
        decoded = root
        for _ in range(len(value) + 1):
            if decoded not in values:
                values.append(decoded)
            if plus:
                form_value = urllib.parse.unquote_plus(decoded, errors="replace")
                if form_value not in values:
                    values.append(form_value)
            next_value = urllib.parse.unquote(decoded, errors="replace")
            if next_value == decoded:
                break
            decoded = next_value
    return tuple(values)


def guard_url_parameter_string(url: str, api: "HostAPI") -> str:
    """Guard a wire URL and every nested-decoding view of its path and query."""
    guarded_url = api.outbound.guard_request_parameter_string(url)
    parsed = urllib.parse.urlsplit(guarded_url)
    for component, plus in ((parsed.path, False), (parsed.query, True)):
        for decoded in decoded_url_component_values(component, plus=plus):
            api.outbound.guard_request_parameter_string(decoded)
    return guarded_url


def schema(properties: JSONObject, required: list[str] | None = None) -> JSONObject:
    output: JSONObject = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        output["required"] = cast(list[JSONValue], required)
    return output


def int_field(tool_input: JSONObject, key: str, *, provider: str, default: int, low: int, high: int) -> int:
    """Accept a digit string (or raw int) and reject out-of-range values."""
    value = tool_input.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 10:
            raise ToolInputValidationError(
                f"{provider} tool_input.{key} must be between {low} and {high}."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError(f"{provider} tool_input.{key} must be an integer or digit string.")
    if not low <= value <= high:
        raise ToolInputValidationError(
            f"{provider} tool_input.{key} must be between {low} and {high}."
        )
    return value


def bounded_int(value: JSONValue | None, *, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    if isinstance(value, str):
        digits = value.strip()
        if not digits.isascii() or not digits.isdecimal() or len(digits) > 3:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
        number = int(digits)
    else:
        number = value
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return number


def string_value(record: JSONObject, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def clip(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def clip_text(value: str, max_bytes: int) -> str:
    """Clip one field to a UTF-8 byte budget for approval summaries. Clipping
    per field keeps every disclosure present when the whole summary must fit
    the host API's 500-byte limit."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "…"


def provider_fetched_https_url(
    tool_input: JSONObject, key: str, api: "HostAPI", *, provider: str
) -> str:
    """Validate one external media URL a provider will fetch, then guard it.

    Generation tools hand the provider a URL rather than bytes, which makes the
    URL itself the thing that leaves this host: whatever an agent encodes into
    its path or query travels with it. So the whole value is scanned, not just
    the parts a reader would think of as data.

    Raw-IP hosts stay allowed. The fetch is made from the provider's network
    rather than this one, so the SSRF exposure is the provider's — an invariant
    worth stating because anything that makes *this* host fetch the URL breaks
    it and needs its own address checks.

    Shared rather than copied per package: this decides what gets scanned before
    reaching a third party, and two copies of that are two chances to drift.
    """
    message = f"{provider} tool_input.{key} must be an https URL."
    value = tool_input.get(key)
    if not isinstance(value, str):
        raise ToolInputValidationError(message)
    value = value.strip()
    if len(value) > 4_096:
        raise ToolInputValidationError(message)
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ToolInputValidationError(message) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ToolInputValidationError(message)
    return api.outbound.guard_request_parameter_string(value)
