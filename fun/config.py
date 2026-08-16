from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass


def _keychain_get() -> str:
    if shutil.which("security") is None:
        return ""
    try:
        result = subprocess.run(["security", "find-generic-password", "-a", "fun", "-s", "fun-api-key", "-w"], capture_output=True, text=True, check=False, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _keychain_delete() -> bool:
    if shutil.which("security") is None:
        return False
    try:
        result = subprocess.run(["security", "delete-generic-password", "-a", "fun", "-s", "fun-api-key"], capture_output=True, text=True, check=False, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _keychain_set(value: str) -> bool:
    if shutil.which("security") is None:
        return False
    try:
        result = subprocess.run(["security", "add-generic-password", "-a", "fun", "-s", "fun-api-key", "-w", value, "-U"], capture_output=True, text=True, check=False, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
from pathlib import Path


@dataclass
class FunConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    approval: str = "smart"
    locale: str = "en-US"
    telemetry: bool = False
    telemetry_endpoint: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "FunConfig":
        target = Path(path).expanduser()
        if not target.exists():
            return cls()
        data = json.loads(target.read_text(encoding="utf-8"))
        allowed = {"base_url", "api_key", "model", "approval", "locale", "telemetry", "telemetry_endpoint"}
        loaded = cls(**{key: value for key, value in data.items() if key in allowed})
        loaded.api_key = os.getenv("FUN_API_KEY") or _keychain_get() or loaded.api_key
        if not loaded.api_key and data.get("api_key_store") == "macos-keychain":
            loaded.base_url = ""
            loaded.model = ""
        return loaded

    def save(self, path: str | Path) -> None:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        if data.get("api_key"):
            key = data.pop("api_key")
            if _keychain_set(key) and _keychain_get() == key:
                data["api_key_store"] = "macos-keychain"
            else:
                # Do not claim durable storage when Keychain cannot read it back.
                # The caller keeps FUN_API_KEY in the current process; next launch
                # must ask for it again rather than silently sending no credentials.
                data["api_key_env"] = "FUN_API_KEY"
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass

    def clear_credentials(self, path: str | Path) -> None:
        _keychain_delete()
        self.api_key = ""
        self.base_url = ""
        self.model = ""
        self.save(path)

    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)
