from __future__ import annotations

import hashlib
import platform
import uuid
from typing import Any


ALLOWED_FIELDS = frozenset({
    "event", "install_id", "fun_version", "python_version", "os", "model_family",
    "input_tokens", "output_tokens", "total_tokens", "tool_calls", "duration_ms", "status",
})


def install_id(existing: str | None = None) -> str:
    """Return an opaque local identifier; never derived from a path or account."""
    return existing or uuid.uuid4().hex


def model_family(model: str) -> str:
    """Keep provider metrics coarse and avoid sending a model/account identifier."""
    normalized = model.strip().lower()
    if not normalized:
        return "unknown"
    return normalized.split("/")[-1].split(":")[0][:64]


def event_payload(*, event: str, install: str, model: str = "", status: str | None = None,
                  input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
                  tool_calls: int = 0, duration_ms: int | None = None) -> dict[str, Any]:
    """Build the only shape a future private telemetry transport may send."""
    payload: dict[str, Any] = {
        "event": event,
        "install_id": hashlib.sha256(install.encode("utf-8")).hexdigest()[:32],
        "fun_version": "1.0.0a1",
        "python_version": platform.python_version(),
        "os": platform.system().lower(),
        "model_family": model_family(model),
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "total_tokens": max(0, int(total_tokens)),
        "tool_calls": max(0, int(tool_calls)),
    }
    if status is not None:
        payload["status"] = status
    if duration_ms is not None:
        payload["duration_ms"] = max(0, int(duration_ms))
    return {key: value for key, value in payload.items() if key in ALLOWED_FIELDS}
