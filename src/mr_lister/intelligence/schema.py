"""Translate application contracts into Bedrock's supported JSON Schema subset."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel

_SUPPORTED_SCHEMA_KEYS = {
    "$defs",
    "$ref",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "definitions",
    "description",
    "enum",
    "format",
    "items",
    "properties",
    "required",
    "title",
    "type",
}


def bedrock_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return a provider schema while preserving stricter application validation.

    Bedrock structured output accepts a subset of JSON Schema Draft 2020-12. In particular,
    it rejects string and numeric bounds and supports only array ``minItems`` values 0 or 1.
    Mr Lister therefore uses Bedrock for structural JSON guarantees and always validates the
    result again against the untouched Pydantic contract.
    """

    return _sanitize_schema(deepcopy(model.model_json_schema()))


def _sanitize_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized: dict[str, Any] = {}
    for key, child in value.items():
        if key not in _SUPPORTED_SCHEMA_KEYS:
            continue
        if key in {"properties", "$defs", "definitions"}:
            sanitized[key] = {
                name: _sanitize_schema(definition) for name, definition in child.items()
            }
        else:
            sanitized[key] = _sanitize_schema(child)

    if sanitized.get("type") == "object":
        sanitized["additionalProperties"] = False
    return sanitized
