from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse
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
        parsed = urlparse(config.base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("INVALID_PROVIDER_ENDPOINT") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535):
            raise ValueError("INVALID_PROVIDER_ENDPOINT")
        if not isinstance(config.model, str) or not config.model.strip():
            raise ValueError("INVALID_PROVIDER_MODEL")
        if not isinstance(config.api_key, str) or not config.api_key.strip():
            raise ValueError("INVALID_PROVIDER_API_KEY")
        if not isinstance(config.timeout, (int, float)) or isinstance(config.timeout, bool) or not math.isfinite(config.timeout) or config.timeout <= 0:
            raise ValueError("INVALID_PROVIDER_TIMEOUT")
        self.config = config

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw_status = getattr(response, "status", None)
                fallback_status = getattr(response, "code", None)
                if raw_status is not None and fallback_status is not None and raw_status != fallback_status:
                    raise ProviderError("PROVIDER_INVALID_STATUS")
                if raw_status is None:
                    raw_status = fallback_status if fallback_status is not None else 200
                if isinstance(raw_status, bool):
                    raise ProviderError("PROVIDER_INVALID_STATUS")
                if isinstance(raw_status, float) and not raw_status.is_integer():
                    raise ProviderError("PROVIDER_INVALID_STATUS")
                if isinstance(raw_status, str) and raw_status.strip() != raw_status:
                    raise ProviderError("PROVIDER_INVALID_STATUS")
                try:
                    status = int(raw_status)
                except (TypeError, ValueError) as exc:
                    raise ProviderError("PROVIDER_INVALID_STATUS", cause=exc) from exc
                if not 100 <= status <= 599:
                    raise ProviderError("PROVIDER_INVALID_STATUS")
                if status >= 400:
                    tag = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_HTTP_FAILED"
                    raise ProviderError(tag)
                headers = getattr(response, "headers", None)
                raw_content_type = headers.get("Content-Type", "") if headers is not None else ""
                content_type = raw_content_type if isinstance(raw_content_type, str) else ""
                if content_type and "text/event-stream" not in content_type.lower():
                    raise ProviderError("PROVIDER_UNEXPECTED_CONTENT_TYPE")
                first = True
                buffer = ""
                data_parts: list[str] = []
                done = False
                for raw in response:
                    if done:
                        break
                    buffer += raw.decode(errors="replace")
                    lines: list[str] = []
                    while True:
                        newline_positions = [position for position in (buffer.find("\n"), buffer.find("\r")) if position >= 0]
                        if not newline_positions:
                            break
                        position = min(newline_positions)
                        if buffer[position] == "\r" and position + 1 == len(buffer):
                            break
                        end = position + 1
                        if buffer[position] == "\r" and end < len(buffer) and buffer[end] == "\n":
                            end += 1
                        lines.append(buffer[:position])
                        buffer = buffer[end:]
                    for raw_line in lines:
                        line = raw_line.strip()
                        if not line:
                            if data_parts:
                                payload = "\n".join(data_parts)
                                data_parts = []
                                try:
                                    item = json.loads(payload)
                                except json.JSONDecodeError as exc:
                                    raise ProviderError("PROVIDER_MALFORMED_EVENT", cause=exc) from exc
                                if first:
                                    item.setdefault("_meta", {})["ttft_ms"] = int((time.monotonic() - started) * 1000)
                                    first = False
                                yield item
                            continue
                        if line.startswith(":") or not line.startswith("data:"):
                            continue
                        value = line[5:].strip()
                        if value == "[DONE]":
                            done = True
                            if data_parts:
                                try:
                                    item = json.loads("\n".join(data_parts))
                                except json.JSONDecodeError as exc:
                                    raise ProviderError("PROVIDER_MALFORMED_EVENT", cause=exc) from exc
                                data_parts = []
                                if first:
                                    item.setdefault("_meta", {})["ttft_ms"] = int((time.monotonic() - started) * 1000)
                                    first = False
                                yield item
                            continue
                        data_parts.append(value)
                if done:
                    return
                if buffer.strip():
                    line = buffer.strip()
                    if line.startswith("data:"):
                        value = line[5:].strip()
                        if value != "[DONE]":
                            data_parts.append(value)
                if data_parts:
                    try:
                        item = json.loads("\n".join(data_parts))
                    except json.JSONDecodeError as exc:
                        raise ProviderError("PROVIDER_MALFORMED_EVENT", cause=exc) from exc
                    if first:
                        item.setdefault("_meta", {})["ttft_ms"] = int((time.monotonic() - started) * 1000)
                    yield item
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError("PROVIDER_TIMEOUT", cause=exc) from exc
        except urllib.error.HTTPError as exc:
            tag = "PROVIDER_AUTH_FAILED" if exc.code in {401, 403} else "PROVIDER_HTTP_FAILED"
            raise ProviderError(tag, cause=exc) from exc
        except OSError as exc:
            status = getattr(exc, "code", getattr(exc, "status", None))
            if status is not None:
                tag = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_HTTP_FAILED"
                raise ProviderError(tag, cause=exc) from exc
            raise ProviderError("PROVIDER_REQUEST_FAILED", cause=exc) from exc
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
