"""Small, fully enforced JSON-result contract used by OpenAI features."""

from __future__ import annotations

import json
import math
from typing import Any


TYPES = frozenset({"object", "array", "string", "boolean", "integer", "number", "null"})


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    return False


def matches(value: Any, schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        return False
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(matches(value, {**schema, "type": item}) for item in expected)
    if expected == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties")
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        if any(key not in value for key in required):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(key in properties and matches(item, properties[key]) for key, item in value.items())
    if expected == "array":
        if not isinstance(value, list):
            return False
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            return False
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return False
        return all(matches(item, schema.get("items")) for item in value)
    if expected == "string":
        if not isinstance(value, str):
            return False
        return not (
            isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]
            or isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        )
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "null":
        return value is None
    return False


def _validate(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"text inference schema {path} must be an object")
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if (
        not types
        or any(not isinstance(item, str) or item not in TYPES for item in types)
        or len(set(types)) != len(types)
    ):
        raise ValueError(f"text inference schema {path} has an invalid type")
    allowed = {"type", "description", "enum"}
    if "object" in types:
        allowed.update({"properties", "required", "additionalProperties"})
    if "array" in types:
        allowed.update({"items", "minItems", "maxItems"})
    if "string" in types:
        allowed.update({"minLength", "maxLength"})
    unsupported = set(schema) - allowed
    if unsupported:
        raise ValueError(
            f"text inference schema {path} uses unsupported keyword {sorted(unsupported)[0]}"
        )
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"text inference schema {path} enum must be a non-empty array")
    if "object" in types:
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
            raise ValueError(f"text inference schema {path} object properties are invalid")
        if (
            not isinstance(required, list)
            or any(not isinstance(key, str) for key in required)
            or len(set(required)) != len(required)
            or set(required) != set(properties)
        ):
            raise ValueError(f"text inference schema {path} must require every property")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"text inference schema {path} must reject additional properties")
        for key, child in properties.items():
            _validate(child, f"{path}.{key}")
    if "array" in types:
        if "items" not in schema:
            raise ValueError(f"text inference schema {path} array items are required")
        _validate(schema["items"], f"{path}[]")
        _validate_bounds(schema, path, "minItems", "maxItems")
    if "string" in types:
        _validate_bounds(schema, path, "minLength", "maxLength")
    if enum is not None:
        without_enum = {key: value for key, value in schema.items() if key != "enum"}
        if any(not matches(item, without_enum) for item in enum):
            raise ValueError(f"text inference schema {path} enum values do not match its type")


def _validate_bounds(schema: dict[str, Any], path: str, minimum: str, maximum: str) -> None:
    for keyword in (minimum, maximum):
        value = schema.get(keyword)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"text inference schema {path} {keyword} is invalid")
    if schema.get(minimum, 0) > schema.get(maximum, math.inf):
        raise ValueError(f"text inference schema {path} bounds are invalid")


def validate_object_schema(schema: Any, *, max_bytes: int) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("text inference schema must describe an object")
    _validate(schema)
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > max_bytes:
        raise ValueError("text inference schema is too large")
    return schema
