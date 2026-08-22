"""Building blocks for the ``output_schema`` a tool manifest declares.

Every direct action states the exact JSON its result carries: each field named,
typed, and described, with every object closed. These helpers are the output
counterpart of ``inputs.schema`` — they keep that declaration short enough that
describing a result stays easier than leaving it vague.

A result carries only what the action found. Whether the call succeeded is the
host's envelope status, not a field inside the result.
"""

from __future__ import annotations

from typing import cast

from host.tools.json_types import JSONObject, JSONValue


def obj(properties: JSONObject, required: list[str] | None = None, description: str = "") -> JSONObject:
    """One closed object: the named properties and nothing else."""
    schema: JSONObject = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = cast(list[JSONValue], required)
    if description:
        schema["description"] = description
    return schema


def array_of(items: JSONObject, description: str) -> JSONObject:
    return {"type": "array", "items": items, "description": description}


def nullable(schema: JSONObject, description: str) -> JSONObject:
    """A value the provider may not have, reported as null rather than omitted."""
    return {"oneOf": [schema, {"type": "null"}], "description": description}


def text(description: str) -> JSONObject:
    return {"type": "string", "description": description}


def integer(description: str) -> JSONObject:
    return {"type": "integer", "description": description}


def number(description: str) -> JSONObject:
    return {"type": "number", "description": description}


def boolean(description: str) -> JSONObject:
    return {"type": "boolean", "description": description}
