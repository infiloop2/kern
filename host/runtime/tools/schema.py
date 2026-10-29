"""The manifest schema subset and its declaration and value validators.

Registration rejects constraints the runtime cannot enforce.
Input and output validation use the same fixed subset.
Tools remain responsible for semantic validation beyond these shapes.
"""

from __future__ import annotations

from typing import Any


# Exactly the keywords validate_against_schema enforces, so a manifest cannot
# declare a constraint the validator would silently skip. Checked at tool
# registration (unsupported_schema_error). CI registers every package before
# merge, and runtime registration keeps the same fail-closed backstop.
_SCHEMA_COMMON_KEYS = frozenset({"type", "enum", "description"})
_SCHEMA_KEYS_BY_TYPE: dict[str, frozenset[str]] = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
    "string": frozenset(),
    "boolean": frozenset(),
    # Results carry counts, prices, and absent values. Without these three a
    # manifest could not describe its own numeric or nullable fields, so the
    # only declarable shape was an open object and outputs went undescribed.
    "integer": frozenset(),
    "number": frozenset(),
    "null": frozenset(),
}


def unsupported_schema_error(
    schema: Any,
    path: str = "schema",
    *,
    allow_empty: bool = False,
) -> str:
    """Reject a declared manifest schema that steps outside the subset
    validate_against_schema enforces. Returns an error string, or "" when the
    whole schema is within the subset. The declaration check is the strict
    mirror of the validator: any keyword, type, or shape the validator would
    ignore is an error here, so declaration and enforcement cannot drift."""
    if not isinstance(schema, dict):
        return f"{path} must be a JSON object."
    if "description" in schema and not isinstance(schema["description"], str):
        return f"{path}.description must be a string."
    if "oneOf" in schema:
        extra = set(schema) - {"oneOf", "description"}
        if extra:
            return f"{path} mixes oneOf with unsupported keys: {', '.join(sorted(extra))}."
        options = schema["oneOf"]
        if not isinstance(options, list) or not options:
            return f"{path}.oneOf must be a non-empty array of schemas."
        for index, option in enumerate(options):
            error = unsupported_schema_error(option, f"{path}.oneOf[{index}]")
            if error:
                return error
        return ""
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum or not all(isinstance(item, (str, int, float, bool)) for item in enum):
            return f"{path}.enum must be a non-empty array of scalars."
    schema_type = schema.get("type")
    if not schema and not allow_empty:
        return f"{path} is an empty schema, which validates nothing."
    if not schema:
        return ""
    if schema_type is None and "enum" in schema:
        return "" if not set(schema) - {"enum", "description"} else (
            f"{path} mixes enum with unsupported keys: {', '.join(sorted(set(schema) - {'enum', 'description'}))}."
        )
    if schema_type not in _SCHEMA_KEYS_BY_TYPE:
        return f"{path}.type {schema_type!r} is outside the supported subset."
    allowed = _SCHEMA_COMMON_KEYS | _SCHEMA_KEYS_BY_TYPE[schema_type]
    extra = set(schema) - allowed
    if extra:
        return f"{path} has unsupported keys: {', '.join(sorted(extra))}."
    if schema_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return f"{path}.properties must be an object."
        if not all(isinstance(name, str) for name in properties):
            return f"{path}.properties names must be strings."
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            return f"{path}.additionalProperties must be a boolean."
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
            return f"{path}.required must be an array of strings."
        if len(required) != len(set(required)):
            return f"{path}.required must not contain duplicate names."
        missing = [name for name in required if name not in properties]
        if missing:
            return f"{path}.required names undeclared properties: {', '.join(sorted(missing))}."
        for name, item in properties.items():
            error = unsupported_schema_error(item, f"{path}.{name}")
            if error:
                return error
    if schema_type == "array":
        if "items" not in schema:
            return f"{path} is an array without items, so elements would go unvalidated."
        for key in ("minItems", "maxItems"):
            if key in schema and (
                isinstance(schema[key], bool)
                or not isinstance(schema[key], int)
                or schema[key] < 0
            ):
                return f"{path}.{key} must be a non-negative integer."
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and isinstance(max_items, int) and min_items > max_items:
            return f"{path}.minItems must not exceed maxItems."
        return unsupported_schema_error(schema["items"], f"{path}.items")
    return ""


def validate_against_schema(value: Any, schema: Any, path: str = "input") -> str:
    """Validate ``value`` against the JSON Schema subset tool manifests use
    (object/string/array/boolean types, properties, required,
    additionalProperties: false, enum, items, minItems/maxItems, oneOf).
    Returns an error string, or "" when the value validates. Tools still own
    semantic validation beyond the schema. The declared schemas are checked
    against exactly this subset at registration (unsupported_schema_error)."""
    if not isinstance(schema, dict):
        return ""
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        for option in one_of:
            if not validate_against_schema(value, option, path):
                return ""
        return f"{path} matches none of the allowed shapes."
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of: {', '.join(str(item) for item in enum)}."
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object."
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{path}.{name} is required."
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                return f"{path} has unsupported fields: {', '.join(sorted(extra))}."
        for name, item in value.items():
            if name in properties:
                error = validate_against_schema(item, properties[name], f"{path}.{name}")
                if error:
                    return error
        return ""
    if schema_type == "array":
        if not isinstance(value, list):
            return f"{path} must be an array."
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"{path} must have at least {min_items} items."
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            return f"{path} must have at most {max_items} items."
        items = schema.get("items")
        for index, item in enumerate(value):
            error = validate_against_schema(item, items, f"{path}[{index}]")
            if error:
                return error
        return ""
    if schema_type == "string" and not isinstance(value, str):
        return f"{path} must be a string."
    # bool is a subclass of int in Python, so both numeric checks exclude it
    # explicitly: True is not a count.
    if schema_type == "boolean" and not isinstance(value, bool):
        return f"{path} must be a boolean."
    if schema_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return f"{path} must be an integer."
    if schema_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return f"{path} must be a number."
    if schema_type == "null" and value is not None:
        return f"{path} must be null."
    return ""
