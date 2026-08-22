from __future__ import annotations

import json
import codecs
import math
import socket
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse
import urllib.error
from typing import Any, Iterator


@dataclass
class ModelConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 120.0
    max_payload_bytes: int = 1024 * 1024


def _classify_empty_stream(body: str) -> str:
    """Name the failure behind a 200 that produced no events."""
    lowered = body.lower()
    if any(marker in lowered for marker in ("invalid api key", "invalid_api_key", "unauthorized", "authentication", "invalid token")):
        return "PROVIDER_AUTH_FAILED"
    return "PROVIDER_EMPTY_STREAM"


class ProviderError(RuntimeError):
    """Provider failure with a stable, privacy-safe classification."""

    def __init__(self, error_tag: str, *, cause: Exception | None = None, endpoint: str = "", key_hint: str = "", status: int = 0) -> None:
        self.error_tag = error_tag
        # The HTTP status, when there was one.  Without it every non-auth HTTP
        # failure — a 404 from a wrong path, a 429 from rate limiting, a 500
        # from the provider — collapsed into one opaque tag and the user had no
        # way to tell which of them had happened.
        self.status = int(status or 0)
        self.cause_type = type(cause).__name__ if cause is not None else None
        # Which address, and which key by its first and last few characters.
        # An auth failure the user cannot attribute to a specific endpoint is a
        # message that says "it did not work" and nothing else.
        self.endpoint = endpoint
        self.key_hint = key_hint
        super().__init__(error_tag)


def mask_key(value: str) -> str:
    """A key rendered as an identity, never as a credential."""
    value = (value or "").strip()
    if len(value) <= 8:
        return "?" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} 位)"


class OpenAICompatible:
    """Minimal OpenAI-compatible chat-completions streaming adapter."""

    MAX_STREAM_BUFFER = 8 * 1024 * 1024

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
        if not isinstance(config.max_payload_bytes, int) or isinstance(config.max_payload_bytes, bool) or not 1 <= config.max_payload_bytes <= 16 * 1024 * 1024:
            raise ValueError("INVALID_PROVIDER_PAYLOAD_LIMIT")
        self.config = config

    def list_models(self) -> list[str]:
        request = urllib.request.Request(self.config.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {self.config.api_key}", "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                payload = json.loads(response.read(self.config.max_payload_bytes).decode("utf-8"))
            data = payload.get("data", []) if isinstance(payload, dict) else []
            models = [item.get("id") for item in data if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()]
            return sorted(set(models))
        except urllib.error.HTTPError as exc:
            raise ProviderError("PROVIDER_AUTH_FAILED" if exc.code in {401, 403} else "PROVIDER_HTTP_FAILED", cause=exc, status=exc.code, **self._identity()) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("PROVIDER_NETWORK_FAILED", cause=exc) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise ProviderError("PROVIDER_REQUEST_FAILED", cause=exc) from exc

    def _identity(self) -> dict[str, str]:
        """The endpoint and a masked key, for error messages only."""
        return {"endpoint": self.config.base_url, "key_hint": mask_key(self.config.api_key)}

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise ProviderError("PROVIDER_INVALID_MESSAGES")
        if tools is not None and (not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools)):
            raise ProviderError("PROVIDER_INVALID_TOOLS")
        try:
            serialized_messages = json.dumps(messages, separators=(",", ":"))
            serialized_tools = json.dumps(tools, separators=(",", ":")) if tools is not None else ""
        except (TypeError, ValueError) as exc:
            raise ProviderError("PROVIDER_INVALID_PAYLOAD", cause=exc) from exc
        if len(serialized_messages.encode()) + len(serialized_tools.encode()) > self.config.max_payload_bytes:
            raise ProviderError("PROVIDER_PAYLOAD_TOO_LARGE")
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
                    raise ProviderError(tag, status=status, **self._identity())
                headers = getattr(response, "headers", None)
                raw_content_type = headers.get("Content-Type", "") if headers is not None else ""
                content_type = raw_content_type if isinstance(raw_content_type, str) else ""
                if content_type and "text/event-stream" not in content_type.lower():
                    raise ProviderError("PROVIDER_UNEXPECTED_CONTENT_TYPE")
                if content_type and "json" in content_type.lower():
                    raise ProviderError("PROVIDER_UNEXPECTED_CONTENT_TYPE")
                first = True
                buffer = ""
                data_parts: list[str] = []
                done = False
                # An incremental decoder, not a per-chunk decode: a 3-byte CJK
                # character split across two reads decoded to replacement
                # characters, silently corrupting the text instead of failing.
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                # A silence deadline, not a total budget.  Bounding the whole
                # stream would kill a healthy long completion mid-flight and
                # throw away everything it had produced; what actually needs
                # bounding is a provider that has stopped saying anything, which
                # urlopen's per-socket-operation timeout does not catch once
                # bytes trickle in.
                last_progress = time.monotonic()
                for raw in response:
                    if done:
                        break
                    if time.monotonic() - last_progress > self.config.timeout:
                        raise ProviderError("PROVIDER_TIMEOUT", **self._identity())
                    last_progress = time.monotonic()
                    buffer += decoder.decode(raw)
                    if len(buffer) > self.MAX_STREAM_BUFFER:
                        raise ProviderError("PROVIDER_MALFORMED_EVENT", **self._identity())
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
                        if value.startswith("{") and '"error"' in value:
                            # Some gateways frame the failure as a normal SSE
                            # event.  Yielding it fed a provider error into the
                            # tool-call parser as if the model had said it.
                            raise ProviderError(_classify_empty_stream(value), **self._identity())
                        if value == "[DONE]":
                            done = True
                            # `continue` only ends this *line*; the rest of the
                            # chunk kept being parsed and yielded, so anything a
                            # proxy appended after [DONE] reached the tool-call
                            # parser as if the model had produced it.
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
                            break
                        data_parts.append(value)
                if done:
                    if first:
                        # A stream whose only content was [DONE] said nothing.
                        raise ProviderError("PROVIDER_EMPTY_STREAM", **self._identity())
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
                        first = False
                    yield item
                if first:
                    # Nothing was ever yielded.  Gateways commonly answer 200
                    # with a JSON error body and no event-stream content type;
                    # returning an empty stream turned "invalid api key" into a
                    # blank reply with no diagnostic anywhere.
                    tail = (buffer + decoder.decode(b"", True)).strip()
                    raise ProviderError(_classify_empty_stream(tail), **self._identity())
        except ProviderError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError("PROVIDER_TIMEOUT", cause=exc) from exc
        except urllib.error.HTTPError as exc:
            tag = "PROVIDER_AUTH_FAILED" if exc.code in {401, 403} else "PROVIDER_HTTP_FAILED"
            raise ProviderError(tag, cause=exc, status=exc.code, **self._identity()) from exc
        except urllib.error.URLError as exc:
            raise ProviderError("PROVIDER_NETWORK_FAILED", cause=exc) from exc
        except OSError as exc:
            status = getattr(exc, "code", getattr(exc, "status", None))
            if status is not None:
                tag = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_HTTP_FAILED"
                raise ProviderError(tag, cause=exc, status=int(status), **self._identity()) from exc
            raise ProviderError("PROVIDER_REQUEST_FAILED", cause=exc) from exc
        except Exception as exc:
            raise ProviderError("PROVIDER_REQUEST_FAILED", cause=exc) from exc


def tool_schemas() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "explore", "description": "List files in the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}}},
        {"type": "function", "function": {"name": "read", "description": "Read a text file with line numbers.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "edit", "description": "Apply a unified diff after an expected hash check.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "expected_hash": {"type": "string"}, "patch": {"type": "string"}}, "required": ["path", "expected_hash", "patch"]}}},
        {"type": "function", "function": {"name": "exec", "description": "Run a command in the workspace.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]}}},
    ]
