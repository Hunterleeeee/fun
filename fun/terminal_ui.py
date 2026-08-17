"""Small, testable terminal UI state model for Fun's persistent composer.

This module deliberately owns presentation state, not Runtime state.  It keeps a
transcript and live tool cards so the CLI can redraw one coherent view instead
of printing unrelated status lines from callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import textwrap
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
    recovery: dict[str, str] | None = None

    def history(self, direction: int) -> str:
        if not self.composer_history:
            return self.composer
        if self.history_index is None:
            self.history_index = len(self.composer_history)
        self.history_index = max(0, min(len(self.composer_history), self.history_index + direction))
        self.composer = self.composer_history[self.history_index] if self.history_index < len(self.composer_history) else ""
        return self.composer

    def set_recovery(self, pending: dict[str, Any] | None) -> None:
        self.recovery = {key: str((pending or {}).get(key, ""))[:300] for key in ("name", "call_id", "arguments")} if pending else None
        if pending:
            self.mode = "recovery"
            self.task_state = "recovery"

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
        status = f"{self.task_state}"
        if self.model_name:
            status += f"  ·  {self.model_name}"
        status += f"  ·  approval={self.approval_mode}"
        header_text = f"Fun  ·  {status}"
        lines.append(header_text[:width])
        lines.append("─" * min(width, 72))
        visible = self.transcript[self.scroll_offset:] if self.scroll_offset else self.transcript
        if self.scroll_offset:
            lines.append(f"  ↑ older messages · offset {self.scroll_offset} · PgUp/PgDn to navigate")
        if not visible:
            lines.append("")
            lines.append("  Start a conversation by describing what you want to build.")
        previous_role = ""
        for item in visible:
            if item.role == "user":
                if previous_role != "user":
                    lines.append("")
                    lines.append("You")
                previous_role = "user"
                lines.extend(textwrap.wrap(f"› {item.text}", width=max(20, width - 2), replace_whitespace=False) or ["› "])
            elif item.role in {"assistant", "system"}:
                if previous_role != item.role:
                    lines.append("Assistant" if item.role == "assistant" else "System")
                previous_role = item.role
                for paragraph in (item.text.splitlines() or [""]):
                    lines.extend(textwrap.wrap(paragraph, width=width, replace_whitespace=False) or [""])
            elif item.tool is not None:
                previous_role = "tool"
                card = item.tool
                args = " ".join(f"{k}={v!r}" for k, v in card.arguments.items())
                detail = f" · {args}" if args else ""
                timing = f" · {card.elapsed_ms}ms" if card.elapsed_ms is not None else ""
                lines.append("")
                header = f"  {('✓' if card.status == 'completed' else '×' if card.status == 'failed' else '•')} {card.name} · {card.status}{timing}{detail}"
                lines.extend(textwrap.wrap(header, width=width, replace_whitespace=False) or [header[:width]])
                if card.status == "approval":
                    risk = f" · risk={card.risk}" if card.risk else ""
                    lines.append(f"  Approval required{risk} · [y] once · [a] this session · [n] deny")
                if card.output:
                    for output_line in card.output[:500].splitlines() or [""]:
                        lines.extend("  " + line for line in (textwrap.wrap(output_line, width=max(10, width - 2), replace_whitespace=False) or [""]))
                if card.error:
                    lines.append(f"  × {card.error}")
                lines.append("")
        if self.recovery:
            lines.append("")
            lines.append("⚠ Recovery required")
            lines.append(f"  {self.recovery.get('name', 'tool')} · {self.recovery.get('call_id', '?')}")
            lines.append(f"  args: {self.recovery.get('arguments', '')}")
            lines.append("  [r] resume  [d] discard  [f] mark failed  [s] stop")
        extras = []
        raw_status = self.status_text.replace("·", " ")
        for token in raw_status.split():
            if token.startswith(("model=", "approval=", "task=")) or token == self.task_state:
                continue
            extras.append(token)
        if extras:
            compact = " ".join(dict.fromkeys(extras))
            if len(compact) > 42:
                compact = compact[:39] + "…"
            status += " · " + compact
        lines.append(f"{status}")
        for task in self.background:
            detail = task.get("result") or task.get("error") or ""
            suffix = f" · {detail}" if detail else ""
            marker = "✓" if task.get("status") == "completed" else "×" if task.get("status") == "failed" else "•"
            lines.append(f"  {marker} bg {task.get('id', '?')} · {task.get('status', '?')} · {task.get('goal', '')}{suffix}")
        lines.append("")
        lines.append("─" * min(width, 72))
        lines.append("Composer")
        prompt = "> " if self.mode == "ready" else "… "
        draft_lines = self.composer.splitlines() or [""]
        lines.append(prompt + draft_lines[0])
        lines.extend("│ " + line for line in draft_lines[1:])
        lines.extend(textwrap.wrap("│ Ctrl-N newline · Enter submit/send · Ctrl-C clear · PgUp/PgDn scroll", width=width, replace_whitespace=False) or [""])
        lines.append("─" * min(width, 72))
        if height is not None and height > 4 and len(lines) > height:
            fixed = len(draft_lines) + 4
            lines = lines[:max(1, height - fixed)] + lines[-fixed:]
        return "\n".join(lines)
