"""Small, testable terminal UI state model for Fun's persistent composer.

This module deliberately owns presentation state, not Runtime state.  It keeps a
transcript and live tool cards so the CLI can redraw one coherent view instead
of printing unrelated status lines from callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import textwrap
from typing import Any


class _Ansi:
    def __init__(self) -> None:
        enabled = os.getenv("NO_COLOR") is None and os.getenv("TERM", "dumb") != "dumb"
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.cyan = "\033[36m" if enabled else ""
        self.green = "\033[32m" if enabled else ""
        self.yellow = "\033[33m" if enabled else ""
        self.red = "\033[31m" if enabled else ""
        self.reset = "\033[0m" if enabled else ""


ANSI = _Ansi()


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
    command: bool = False


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
    collapsed_tools: set[str] = field(default_factory=set)
    show_all_commands: bool = False
    toast: str = ""
    toast_ticks: int = 0

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

    def add_command(self, text: str) -> None:
        text = text.strip()
        if text:
            self.transcript.append(TranscriptItem("user", text, command=True))

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

    def tick(self) -> None:
        if self.toast:
            self.toast_ticks += 1
            if self.toast_ticks > 4:
                self.toast = ""
                self.toast_ticks = 0

    def render(self, width: int = 88, height: int | None = None) -> str:
        """Render a stable transcript plus a fixed bottom composer area."""
        width = max(40, width)
        lines: list[str] = []
        status = f"{self.task_state}"
        if self.model_name:
            status += f"  ·  {self.model_name}"
        status += f"  ·  approval={self.approval_mode}"
        header_text = f"Fun  ·  {status}"
        lines.append(f"{ANSI.bold}{ANSI.cyan}{header_text[:width]}{ANSI.reset}")
        lines.append("─" * min(width, 72))
        visible = self.transcript[self.scroll_offset:] if self.scroll_offset else self.transcript
        if self.scroll_offset:
            lines.append(f"  ↑ older messages · offset {self.scroll_offset} · PgUp/PgDn to navigate")
        if not visible:
            lines.append("")
            lines.append("  Start a conversation by describing what you want to build.")
        command_count = sum(1 for item in visible if item.command)
        hidden_commands = 0 if self.show_all_commands else max(0, command_count - 6)
        if hidden_commands:
            lines.append(f"{ANSI.dim}… {hidden_commands} earlier commands hidden · press C to expand{ANSI.reset}")
        elif command_count > 6:
            lines.append(f"{ANSI.dim}… showing all {command_count} commands · press C to collapse{ANSI.reset}")
        shown_commands = 0
        previous_role = ""
        for item in visible:
            if item.role == "user":
                if item.command:
                    shown_commands += 1
                    if shown_commands <= hidden_commands:
                        continue
                    lines.append(f"{ANSI.dim}⌘ {item.text}{ANSI.reset}")
                    previous_role = "command"
                    continue
                if previous_role != "user":
                    lines.append("")
                    lines.append(f"{ANSI.bold}{ANSI.cyan}You{ANSI.reset}")
                previous_role = "user"
                lines.extend(textwrap.wrap(f"› {item.text}", width=max(20, width - 2), replace_whitespace=False) or ["› "])
            elif item.role in {"assistant", "system"}:
                if previous_role != item.role:
                    role_label = "Assistant" if item.role == "assistant" else "System"
                    lines.append(f"{ANSI.bold}{role_label}{ANSI.reset}")
                previous_role = item.role
                for paragraph in (item.text.splitlines() or [""]):
                    lines.extend(textwrap.wrap(paragraph, width=width, replace_whitespace=False) or [""])
            elif item.tool is not None:
                previous_role = "tool"
                card = item.tool
                args = " ".join(f"{k}={v!r}" for k, v in card.arguments.items())
                if len(args) > 56:
                    args = args[:53] + "…"
                detail = f" · {args}" if args else ""
                timing = f" · {card.elapsed_ms}ms" if card.elapsed_ms is not None else ""
                lines.append("")
                symbol = '✓' if card.status == 'completed' else '×' if card.status == 'failed' else '•'
                color = ANSI.green if card.status == 'completed' else ANSI.red if card.status == 'failed' else ANSI.yellow
                header = f"  {symbol} {card.name} · {card.status}{timing}{detail}"
                wrapped = textwrap.wrap(header, width=width, replace_whitespace=False) or [header[:width]]
                lines.extend((f"{color}{line}{ANSI.reset}" if i == 0 else line) for i, line in enumerate(wrapped))
                if card.status == "approval":
                    risk = f" · risk={card.risk}" if card.risk else ""
                    lines.append(f"{ANSI.yellow}  Approval required{risk} · [y] once · [a] this session · [n] deny{ANSI.reset}")
                if card.call_id not in self.collapsed_tools and card.output:
                    for output_line in card.output[:500].splitlines() or [""]:
                        lines.extend("  " + line for line in (textwrap.wrap(output_line, width=max(10, width - 2), replace_whitespace=False) or [""]))
                elif card.output:
                    lines.append(f"  ↳ output hidden ({len(card.output)} chars)")
                if card.error:
                    lines.append(f"  × {card.error}")
                lines.append("")
        if self.recovery:
            lines.append("")
            lines.append(f"{ANSI.bold}{ANSI.yellow}⚠ Recovery required{ANSI.reset}")
            lines.append(f"  {self.recovery.get('name', 'tool')} · {self.recovery.get('call_id', '?')}")
            lines.append(f"  args: {self.recovery.get('arguments', '')}")
            lines.append("  [r] resume  [d] discard  [f] mark failed  [s] stop")
        extras = []
        raw_status = self.status_text.replace("·", " ")
        for token in raw_status.split():
            if token.startswith(("model=", "approval=", "task=")) or token == self.task_state:
                continue
            extras.append(token)
        if self.toast:
            lines.append(f"{ANSI.green}✓ {self.toast}{ANSI.reset}")
        if extras:
            compact = " ".join(dict.fromkeys(extras))
            if len(compact) > 42:
                compact = compact[:39] + "…"
            status += " · " + compact
        lines[0] = f"{ANSI.bold}{ANSI.cyan}{('Fun  ·  ' + status)[:width]}{ANSI.reset}"
        for task in self.background:
            detail = task.get("result") or task.get("error") or ""
            suffix = f" · {detail}" if detail else ""
            marker = "✓" if task.get("status") == "completed" else "×" if task.get("status") == "failed" else "•"
            lines.append(f"  {marker} bg {task.get('id', '?')} · {task.get('status', '?')} · {task.get('goal', '')}{suffix}")
        if self.mode == "working":
            lines.append("  · working…")
        elif self.mode == "approval":
            lines.append("  · waiting for approval · y/a/n")
        elif self.mode == "recovery":
            lines.append("  · recovery action required · r/d/f/s")
        lines.append("")
        lines.append("─" * min(width, 72))
        lines.append(f"{ANSI.bold}Composer{ANSI.reset}")
        prompt = "> " if self.mode == "ready" else "… "
        draft_lines = self.composer.splitlines() or [""]
        lines.append(prompt + draft_lines[0])
        lines.extend("│ " + line for line in draft_lines[1:])
        hints = "│ Ctrl-N newline · Enter submit/send · Ctrl-C clear · PgUp/PgDn scroll"
        if self.tools:
            hints += " · C collapse/expand"
        if self.show_all_commands:
            hints += " · C collapse commands"
        lines.extend(textwrap.wrap(hints, width=width, replace_whitespace=False) or [""])
        lines.append("─" * min(width, 72))
        if height is not None and height > 4 and len(lines) > height:
            fixed = len(draft_lines) + 4
            lines = lines[:max(1, height - fixed)] + lines[-fixed:]
        return "\n".join(lines)
