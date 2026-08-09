"""Provider-neutral tool-input validation primitives for tool packages.

Each bundled tool owns its provider-specific validation grammar; this module
holds the generic building blocks (bounded integers, clipped strings, the
common object-schema shape) so packages do not carry copy-pasted duplicates.
Error messages stay provider-prefixed through the ``provider``/``name``
parameters, so consolidating here changes no user-visible text.
"""

from __future__ import annotations

from typing import cast

from host.tools.json_types import JSONObject, JSONValue


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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
