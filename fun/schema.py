from __future__ import annotations

from typing import Any


class SchemaError(ValueError):
    pass


TOOL_FIELDS: dict[str, dict[str, set[str]]] = {
    "explore": {"required": set(), "optional": {"path", "limit"}},
    "read": {"required": {"path"}, "optional": {"path", "start", "end"}},
    "edit": {"required": {"path", "expected_hash", "patch"}, "optional": {"path", "expected_hash", "patch"}},
    "exec": {"required": {"command"}, "optional": {"command", "timeout"}},
}


def validate_tool_arguments(name: str, arguments: Any) -> dict[str, Any]:
    if name not in TOOL_FIELDS:
        raise SchemaError("UNSUPPORTED_TOOL")
    if not isinstance(arguments, dict):
        raise SchemaError("INVALID_ARGUMENTS")
    fields = TOOL_FIELDS[name]
    unknown = set(arguments) - fields["optional"]
    missing = fields["required"] - set(arguments)
    if unknown or missing:
        raise SchemaError("INVALID_ARGUMENTS")
    for key, value in arguments.items():
        if key in {"path", "expected_hash", "patch", "command"} and not isinstance(value, str):
            raise SchemaError("INVALID_ARGUMENTS")
        if key in {"limit", "start", "end"} and (not isinstance(value, int) or isinstance(value, bool)):
            raise SchemaError("INVALID_ARGUMENTS")
        if key == "timeout" and (not isinstance(value, (int, float)) or value <= 0):
            raise SchemaError("INVALID_ARGUMENTS")
    return arguments
