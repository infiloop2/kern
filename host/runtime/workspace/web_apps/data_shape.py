"""Bounded structural descriptions for Web App JSON documents."""

from __future__ import annotations

import json
from typing import Any


MAX_PATH_KEY_BYTES = 128
MAX_SHAPE_DEPTH = 6
MAX_SHAPE_OBJECT_KEYS = 64
MAX_SHAPE_ARRAY_SAMPLE = 200
MAX_SHAPE_NODES = 1000
MAX_SHAPE_ENUM_VALUES = 8
MIN_SHAPE_ENUM_OBSERVATIONS = 4
MIN_SHAPE_ENUM_VALUE_OBSERVATIONS = 2
MAX_SHAPE_ENUM_VALUE_BYTES = 40


class _ShapeBudget:
    """Bounds one shape response to a fixed number of described nodes."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def data_shape(value: Any) -> dict[str, Any]:
    return _data_shape([value], 0, _ShapeBudget(MAX_SHAPE_NODES))


def _data_shape(values: list[Any], depth: int, budget: _ShapeBudget) -> dict[str, Any]:
    """Describe one position from every value observed there."""
    node = _shape_node(values, depth, budget)
    if len(values) == 1 and node["type"] in {"object", "array", "string"}:
        node["bytes"] = _encoded_size(values[0])
    return node


def _shape_node(values: list[Any], depth: int, budget: _ShapeBudget) -> dict[str, Any]:
    kinds = sorted({_shape_kind(value) for value in values})
    if len(kinds) > 1:
        return {"type": "mixed", "types": kinds}
    if kinds[0] == "object":
        return _object_shape(values, depth, budget)
    if kinds[0] == "array":
        return _array_shape(values, depth, budget)
    return _scalar_shape(kinds[0], values)


def _shape_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def _object_shape(values: list[Any], depth: int, budget: _ShapeBudget) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "object"}
    if depth >= MAX_SHAPE_DEPTH:
        node["truncated"] = True
        return node
    observed: dict[str, list[Any]] = {}
    for value in values:
        for key, child in value.items():
            observed.setdefault(key, []).append(child)
    keys: dict[str, Any] = {}
    for key in sorted(observed):
        if len(keys) >= MAX_SHAPE_OBJECT_KEYS or not budget.take():
            node["truncated"] = True
            break
        child_shape = _data_shape(observed[key], depth + 1, budget)
        if len(observed[key]) < len(values):
            child_shape["optional"] = True
        if not _addressable_key(key):
            child_shape["addressable"] = False
        keys[key] = child_shape
    node["keys"] = keys
    return node


def _array_shape(values: list[Any], depth: int, budget: _ShapeBudget) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "array"}
    if len(values) == 1:
        node["length"] = len(values[0])
    elements = [element for value in values for element in value]
    if not elements:
        return node
    if depth >= MAX_SHAPE_DEPTH:
        node["truncated"] = True
        return node
    if len(elements) > MAX_SHAPE_ARRAY_SAMPLE:
        node["sampled"] = MAX_SHAPE_ARRAY_SAMPLE
        elements = elements[:MAX_SHAPE_ARRAY_SAMPLE]
    if not budget.take():
        node["truncated"] = True
        return node
    node["items"] = _data_shape(elements, depth + 1, budget)
    return node


def _scalar_shape(kind: str, values: list[Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": kind}
    if kind != "string":
        return node
    distinct = sorted(set(values))
    if (
        len(values) >= MIN_SHAPE_ENUM_OBSERVATIONS
        and len(distinct) * MIN_SHAPE_ENUM_VALUE_OBSERVATIONS <= len(values)
        and len(distinct) <= MAX_SHAPE_ENUM_VALUES
        and all(_enum_value_fits(value) for value in distinct)
    ):
        node["enum"] = distinct
    return node


def utf8_length(text: str) -> int | None:
    try:
        return len(text.encode())
    except UnicodeEncodeError:
        return None


def _addressable_key(key: str) -> bool:
    size = utf8_length(key)
    return bool(key) and size is not None and size <= MAX_PATH_KEY_BYTES


def _enum_value_fits(value: str) -> bool:
    size = utf8_length(value)
    return size is not None and size <= MAX_SHAPE_ENUM_VALUE_BYTES


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())
