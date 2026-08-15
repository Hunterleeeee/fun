from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass
class ModelConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 120.0


class ProviderError(RuntimeError):
    """Provider failure with a stable, privacy-safe classification."""

    def __init__(self, error_tag: str, *, cause: Exception | None = None) -> None:
        self.error_tag = error_tag
        self.cause_type = type(cause).__name__ if cause is not None else None
        super().__init__(error_tag)


class OpenAICompatible:
    """Minimal OpenAI-compatible chat-completions streaming adapter."""

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
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                first = True
                buffer = ""
                for raw in response:
                    buffer += raw.decode(errors="replace")
                    lines = buffer.splitlines(keepends=True)
                    buffer = lines.pop() if lines and not lines[-1].endswith(("\\n", "\\r")) else ""
                    for raw_line in lines:
                        line = raw_line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if first:
                            item.setdefault("_meta", {})["ttft_ms"] = int((time.monotonic() - started) * 1000)
                            first = False
                        yield item
                if buffer.strip() and buffer.strip() != "data: [DONE]":
                    line = buffer.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        item = None
                    if item is not None:
                        if first:
                            item.setdefault("_meta", {})["ttft_ms"] = int((time.monotonic() - started) * 1000)
                        yield item
        except TimeoutError as exc:
            raise ProviderError("PROVIDER_TIMEOUT", cause=exc) from exc
        except urllib.error.HTTPError as exc:
            tag = "PROVIDER_AUTH_FAILED" if exc.code in {401, 403} else "PROVIDER_HTTP_FAILED"
            raise ProviderError(tag, cause=exc) from exc
        except urllib.error.URLError as exc:
            raise ProviderError("PROVIDER_NETWORK_FAILED", cause=exc) from exc
        except Exception as exc:
            raise ProviderError("PROVIDER_REQUEST_FAILED", cause=exc) from exc


def tool_schemas() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "explore", "description": "List files in the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}}},
        {"type": "function", "function": {"name": "read", "description": "Read a text file with line numbers.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "edit", "description": "Apply a unified diff after an expected hash check.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "expected_hash": {"type": "string"}, "patch": {"type": "string"}}, "required": ["path", "expected_hash", "patch"]}}},
        {"type": "function", "function": {"name": "exec", "description": "Run a command in the workspace.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]}}},
    ]
