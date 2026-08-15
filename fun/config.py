from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class FunConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    approval: str = "smart"
    locale: str = "en-US"

    @classmethod
    def load(cls, path: str | Path) -> "FunConfig":
        target = Path(path).expanduser()
        if not target.exists():
            return cls()
        data = json.loads(target.read_text(encoding="utf-8"))
        allowed = {"base_url", "api_key", "model", "approval", "locale"}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def save(self, path: str | Path) -> None:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass

    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)
