"""Dependency-free validation for the JSON-Schema subset used by tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ToolValidationError(ValueError):
    tool_name: str
    path: str
    detail: str

    def __str__(self) -> str:
        location = self.path or "$"
        return f"invalid input for {self.tool_name} at {location}: {self.detail}"


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate(tool_name: str, value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, str(item)) for item in expected):
            raise ToolValidationError(tool_name, path, f"expected one of {expected}, got {type(value).__name__}")
    elif expected and not _matches_type(value, str(expected)):
        raise ToolValidationError(tool_name, path, f"expected {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(tool_name, path, f"expected one of {schema['enum']!r}")

    if isinstance(value, dict):
        required = [str(item) for item in schema.get("required", [])]
        for key in required:
            if key not in value:
                raise ToolValidationError(tool_name, path, f"missing required property {key!r}")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ToolValidationError(tool_name, path, f"unexpected properties: {extras}")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                _validate(tool_name, item, child, f"{path}.{key}" if path else key)

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate(tool_name, item, schema["items"], f"{path}[{index}]")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ToolValidationError(tool_name, path, f"shorter than minLength={schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ToolValidationError(tool_name, path, f"longer than maxLength={schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolValidationError(tool_name, path, f"below minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolValidationError(tool_name, path, f"above maximum={schema['maximum']}")


def schema_for_tool(tool_name: str, definitions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = {"grep": "grep_search", "bash": "run_shell"}.get(tool_name, tool_name)
    for definition in definitions:
        if definition.get("name") == normalized:
            schema = definition.get("input_schema")
            return schema if isinstance(schema, dict) else None
    return None


def validate_tool_input(tool_name: str, value: Any, definitions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolValidationError(tool_name, "$", f"expected object, got {type(value).__name__}")
    schema = schema_for_tool(tool_name, definitions)
    if schema is not None:
        _validate(tool_name, value, schema, "$")
    return value
