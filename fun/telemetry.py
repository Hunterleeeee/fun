from __future__ import annotations

import hashlib
import platform
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any


ALLOWED_FIELDS = frozenset({
    "event", "install_id", "fun_version", "python_version", "os", "model_family",
    "input_tokens", "output_tokens", "total_tokens", "tool_calls", "duration_ms", "status",
})


def install_id(existing: str | None = None) -> str:
    """Return an opaque local identifier; never derived from a path or account."""
    return existing or uuid.uuid4().hex


def load_or_create_install_id(state_dir: str) -> str:
    path = os.path.join(state_dir, "telemetry_id")
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = install_id()
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.chmod(path, 0o600)
    except FileExistsError:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip() or value
        except OSError:
            pass
    except OSError:
        pass
    return value


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


class TelemetryClient:
    """Best-effort opt-in sender for an operator-controlled private endpoint."""

    def __init__(self, enabled: bool = False, endpoint: str = "", install: str | None = None) -> None:
        self.enabled = bool(enabled and endpoint.strip())
        self.endpoint = endpoint.strip()
        self.install = install_id(install)

    def send(self, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        safe = {key: value for key, value in payload.items() if key in ALLOWED_FIELDS}
        try:
            request = urllib.request.Request(self.endpoint, data=json.dumps(safe).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError, ValueError):
            return False
