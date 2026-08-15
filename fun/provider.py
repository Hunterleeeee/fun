from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass
class ModelConfig:
    base_url: str
    api_key: str
    model: str


class ProviderError(RuntimeError):
    pass


class OpenAICompatible:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                for raw in response:
                    line = raw.decode(errors="replace").strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            raise ProviderError(str(exc)) from exc
