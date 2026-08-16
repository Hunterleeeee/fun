"""Small, testable terminal UI state model for Fun's persistent composer.

This module deliberately owns presentation state, not Runtime state.  It keeps a
transcript and live tool cards so the CLI can redraw one coherent view instead
of printing unrelated status lines from callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCard:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    elapsed_ms: int | None = None
    output: str = ""
    error: str = ""
    risk: str = ""
    exit_code: int | None = None

    def update(self, status: str, payload: dict[str, Any]) -> None:
        self.status = status
        if isinstance(payload.get("elapsed_ms"), int):
            self.elapsed_ms = payload["elapsed_ms"]
        if isinstance(payload.get("text"), str):
            self.output = payload["text"][:500]
        if isinstance(payload.get("error"), str):
            self.error = payload["error"][:240]
        if isinstance(payload.get("risk"), str):
            self.risk = payload["risk"]
        if isinstance(payload.get("exit_code"), int):
            self.exit_code = payload["exit_code"]


@dataclass
class TranscriptItem:
    role: str
    text: str = ""
    tool: ToolCard | None = None


@dataclass
class TerminalUiState:
    """Owned UI state for a persistent transcript and bottom composer."""

    locale: str = "en-US"
    composer: str = ""
    mode: str = "ready"
    status_text: str = ""
    model_name: str = ""
    task_state: str = "idle"
    approval_mode: str = "smart"
    transcript: list[TranscriptItem] = field(default_factory=list)
    tools: dict[str, ToolCard] = field(default_factory=dict)
    composer_history: list[str] = field(default_factory=list)
    history_index: int | None = None
    scroll_offset: int = 0
    background: list[dict[str, str]] = field(default_factory=list)

    def history(self, direction: int) -> str:
        if not self.composer_history:
            return self.composer
        if self.history_index is None:
            self.history_index = len(self.composer_history)
        self.history_index = max(0, min(len(self.composer_history), self.history_index + direction))
        self.composer = self.composer_history[self.history_index] if self.history_index < len(self.composer_history) else ""
        return self.composer

    def set_background(self, tasks: list[dict[str, str]]) -> None:
        self.background = [
            {key: str(item.get(key, ""))[:240] for key in ("id", "status", "goal", "result", "error")}
            for item in tasks
        ]

    def scroll(self, delta: int) -> int:
        self.scroll_offset = max(0, min(max(0, len(self.transcript) - 1), self.scroll_offset + delta))
        return self.scroll_offset

    def add_user(self, text: str) -> None:
        text = text.strip()
        if text:
            self.transcript.append(TranscriptItem("user", text))
            self.composer_history.append(text)
            self.history_index = None

    def restore_messages(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                self.add_user(content)
            elif role == "assistant" and isinstance(content, str) and content:
                self.add_assistant(content)
            elif role == "tool" and isinstance(content, str):
                call_id = str(message.get("tool_call_id", "restored-tool"))
                self.tool_status("tool.completed", {"call_id": call_id, "name": "tool", "text": content})

    def add_assistant(self, text: str) -> None:
        if not text:
            return
        if self.transcript and self.transcript[-1].role == "assistant":
            self.transcript[-1].text += text
        else:
            self.transcript.append(TranscriptItem("assistant", text))

    def tool_status(self, kind: str, payload: dict[str, Any]) -> ToolCard | None:
        call_id = str(payload.get("call_id", ""))
        if not call_id:
            return None
        card = self.tools.get(call_id)
        if card is None:
            card = ToolCard(call_id, str(payload.get("name", "tool")), dict(payload.get("arguments") or {}))
            self.tools[call_id] = card
            self.transcript.append(TranscriptItem("tool", tool=card))
        mapping = {
            "tool.started": "queued",
            "approval.pending": "approval",
            "tool.executing": "running",
            "tool.progress": "running",
            "tool.completed": "completed",
            "tool.failed": "failed",
        }
        card.update(mapping.get(kind, kind), payload)
        return card

    def render(self, width: int = 88, height: int | None = None) -> str:
        """Render a stable transcript plus a fixed bottom composer area."""
        width = max(40, width)
        lines: list[str] = []
        visible = self.transcript[self.scroll_offset:] if self.scroll_offset else self.transcript
        for item in visible:
            if item.role == "user":
                lines.append(f"› {item.text}")
            elif item.role in {"assistant", "system"}:
                lines.extend(item.text.splitlines() or [""])
            elif item.tool is not None:
                card = item.tool
                args = " ".join(f"{k}={v!r}" for k, v in card.arguments.items())
                detail = f" · {args}" if args else ""
                timing = f" · {card.elapsed_ms}ms" if card.elapsed_ms is not None else ""
                lines.append(f"┌ {card.name} · {card.status}{timing}{detail}")
                if card.status == "approval":
                    risk = f" · risk={card.risk}" if card.risk else ""
                    lines.append(f"│ Approval required{risk} · [y] once · [a] this session · [n] deny")
                if card.output:
                    lines.append(f"│ {card.output.replace(chr(10), chr(10) + '│ ')[:500]}")
                if card.error:
                    lines.append(f"│ × {card.error}")
                lines.append("└")
        status = f"{self.task_state}"
        if self.model_name:
            status += f" · model={self.model_name}"
        status += f" · approval={self.approval_mode}"
        if self.status_text:
            status += f" · {self.status_text}"
        lines.append(f"· {status}")
        for task in self.background:
            detail = task.get("result") or task.get("error") or ""
            suffix = f" · {detail}" if detail else ""
            lines.append(f"  bg {task.get('id', '?')} · {task.get('status', '?')} · {task.get('goal', '')}{suffix}")
        lines.append("─" * min(width, 88))
        lines.append("  Ctrl-N newline · Enter submit · Ctrl-C clear")
        prompt = "> " if self.mode == "ready" else "… "
        draft_lines = self.composer.splitlines() or [""]
        lines.append(prompt + draft_lines[0])
        lines.extend("  " + line for line in draft_lines[1:])
        if height is not None and height > 4 and len(lines) > height:
            fixed = len(draft_lines) + 2
            lines = lines[:max(1, height - fixed)] + lines[-fixed:]
        return "\n".join(lines)
