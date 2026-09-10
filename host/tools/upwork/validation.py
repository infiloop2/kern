"""Bounded parameter guards and validation for Kern's fixed Upwork actions."""

import json
import math
import re
import urllib.parse

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject
from host.tools.shared.inputs import decoded_url_component_values

# These provider identifiers/cursors have a bounded ASCII grammar. Keep all
# explicit secret checks, while allowing the numeric/opaque values Upwork issues.
OPAQUE_FIELDS = frozenset(("org_uid", "profile_key", "cursor", "job_id", "job_reference",
    "job_posting_id", "proposal_id", "room_id", "preview_id", "team_org_id",
    "attachments", "certificate_ids", "portfolio_project_ids", "trace_id"))
OPAQUE = re.compile(r"[A-Za-z0-9_./:+=~-]{1,1024}\Z")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_KEYWORDS = frozenset(("type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    "title", "description", "default", "examples", "$schema", "deprecated"))


class UnsupportedSchema(ValueError):
    """The local action schema uses unsupported constraints."""


def arguments(raw: object, api: HostAPI, *, guarded: bool = True) -> JSONObject:
    limit = 16384 if guarded else 65536
    if not isinstance(raw, str) or len(raw.encode()) > limit:
        raise ValueError(f"Upwork parameters must be a JSON object of at most {limit // 1024} KiB.")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate Upwork argument keys are not allowed.")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        raise ValueError("Upwork parameters must be valid JSON without duplicate keys.") from None
    if not isinstance(value, dict):
        raise ValueError("Upwork parameters must be an object.")
    count = 0

    def guard(item, depth=0, field=""):
        nonlocal count
        count += 1
        if depth > 8 or count > 512:
            raise ValueError("Upwork arguments exceed the nesting or item limit.")
        if isinstance(item, dict):
            for key, child in item.items():
                if not NAME.fullmatch(key):
                    raise ValueError("Upwork argument keys must be simple field names.")
                if guarded:
                    api.outbound.guard_request_parameter_string(key)
                guard(child, depth + 1, key)
        elif isinstance(item, list):
            for child in item:
                guard(child, depth + 1, field)
        elif isinstance(item, str):
            if "\x00" in item:
                raise ValueError("Upwork parameters cannot contain NUL characters.")
            opaque = field in OPAQUE_FIELDS
            if opaque and not OPAQUE.fullmatch(item):
                raise ValueError("Upwork identifiers and cursors must use their bounded ASCII grammar.")
            if not guarded:
                return
            api.outbound.guard_request_parameter_string(item, allow_identifiers=opaque, allow_machine_tokens=opaque)
            # Inspect all nested percent/form interpretations, including
            # encoded schemes, fragments and strings that are not URLs.
            for decoded in decoded_url_component_values(item, plus=True):
                api.outbound.guard_request_parameter_string(decoded, allow_identifiers=opaque, allow_machine_tokens=opaque)
                if "://" in decoded:
                    parsed = urllib.parse.urlsplit(decoded)
                    for component in (parsed.netloc, parsed.path, parsed.query, parsed.fragment):
                        api.outbound.guard_request_parameter_string(component, allow_identifiers=opaque, allow_machine_tokens=opaque)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            if not math.isfinite(item) or abs(item) > 9_007_199_254_740_991:
                raise ValueError("Upwork numeric arguments must be finite safe JSON numbers.")
            if guarded:
                api.outbound.guard_request_parameter_string(str(item))
        elif item is not None and not isinstance(item, bool):
            raise ValueError("Unsupported Upwork argument value.")

    guard(value)
    return value


def _json_equal(left, right) -> bool:
    # JSON numbers compare numerically, but booleans are a distinct type.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) or isinstance(right, dict):
        return isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) or isinstance(right, list):
        return isinstance(left, list) and isinstance(right, list) and len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _validate(value, schema, depth) -> None:
    if depth > 8 or not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise UnsupportedSchema("Unsupported Kern Upwork input schema.")
    expected = schema.get("type")
    if value is None:
        actual = "null"
    elif isinstance(value, bool):
        actual = "boolean"
    elif isinstance(value, (int, float)):
        actual = "integer" if isinstance(value, int) or value.is_integer() else "number"
    elif isinstance(value, str):
        actual = "string"
    elif isinstance(value, list):
        actual = "array"
    else:
        actual = "object"
    if "type" in schema:
        types = expected if isinstance(expected, list) else [expected]
        if not types or any(not isinstance(kind, str) or kind not in ("null", "boolean", "integer", "number", "string", "array", "object") for kind in types):
            raise UnsupportedSchema("Upwork returned an invalid type schema.")
        if actual not in types and not (actual == "integer" and "number" in types):
            raise ValueError("Upwork argument has the wrong type for its schema.")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not any(_json_equal(value, choice) for choice in schema["enum"])):
        raise ValueError("Upwork argument is outside the tool's allowed values.")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise ValueError("Upwork argument is outside the tool's allowed values.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list) or not all(isinstance(key, str) for key in required):
            raise UnsupportedSchema("Upwork returned an invalid object schema.")
        unknown = set(value) - set(properties)
        if set(required) - set(value) or unknown:
            raise ValueError("Upwork arguments have missing required or unknown fields.")
        for key, child in value.items():
            _validate(child, properties[key], depth + 1)
    if isinstance(value, list) and "items" in schema:
        for child in value:
            _validate(child, schema["items"], depth + 1)
    for low, high, measurement in (
        ("minLength", "maxLength", len(value) if isinstance(value, str) else None),
        ("minItems", "maxItems", len(value) if isinstance(value, list) else None),
        ("minimum", "maximum", value if isinstance(value, (int, float)) and not isinstance(value, bool) else None),
    ):
        if measurement is not None and (low in schema and measurement < schema[low] or high in schema and measurement > schema[high]):
            raise ValueError("Upwork argument is outside the tool's size or numeric limit.")


def validate(value, schema) -> None:
    _validate(value, schema, 0)
