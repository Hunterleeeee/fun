from __future__ import annotations

import math
from typing import Any

# The exec timeout is the only bound on the highest-risk tool, so a model
# argument must not be able to remove it.  ``json.loads`` accepts the
# non-standard literals ``Infinity`` and ``NaN``, and ``bool`` is an ``int``.
MAX_EXEC_TIMEOUT = 900.0


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
        if key == "timeout":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError("INVALID_ARGUMENTS")
            if not math.isfinite(value) or not 0 < value <= MAX_EXEC_TIMEOUT:
                raise SchemaError("INVALID_ARGUMENTS")
    return arguments
