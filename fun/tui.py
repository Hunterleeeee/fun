"""Persistent, single-writer terminal UI for interactive Fun sessions."""
from __future__ import annotations

import os
import queue
import select
import sys
import threading
try:
    import termios
    import tty
except ImportError:  # Windows keeps the plain CLI fallback
    termios = None
    tty = None
from dataclasses import dataclass, field
from shutil import get_terminal_size
from typing import Any, Callable

from .terminal_ui import TerminalUiState


@dataclass
class _Approval:
    name: str
    risk: object
    arguments: dict[str, Any]
    answer: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class TerminalUI:
    """A POSIX composer with one render owner and background model workers.

    The callback is invoked on the UI thread for commands and on a worker for
    ordinary goals. Runtime callbacks must only enqueue UI updates.
    """

    def __init__(self, locale: str = "en-US", output=None, commands: list[str] | None = None) -> None:
        self.output = output or sys.stdout
        self.state = TerminalUiState(locale=locale)
        self.commands = commands or []
        self._suggestion_index = 0
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stop = False
        self._worker: threading.Thread | None = None
        self._approval: _Approval | None = None
        self._approval_context: dict[tuple[str, str], dict[str, Any]] = {}
        self._old_termios: list[Any] | None = None
        self._dirty = True
        self._last_size: tuple[int, int] | None = None
        self.modal: dict[str, Any] | None = None
        self.modal_callback: Callable[[dict[str, str] | None], None] | None = None

    def post(self, kind: str, payload: Any = None) -> None:
        self._dirty = True
        self.events.put((kind, payload))

    def open_modal(self, title: str, fields: list[str | tuple[str, bool]], callback: Callable[[dict[str, str] | None], None]) -> None:
        normalized = [(item[0], bool(item[1])) if isinstance(item, tuple) else (item, False) for item in fields]
        self.modal = {"kind": "fields", "title": title, "fields": normalized, "index": "0", "value": "", "values": {}}
        self.modal_callback = callback
        self._dirty = True

    def open_select(self, title: str, options: list[str], callback: Callable[[str | None], None]) -> None:
        self.modal = {"kind": "select", "title": title, "options": options, "index": "0", "current": options[0] if options else "", "loading": False}
        self.modal_callback = callback
        self._dirty = True

    def set_status(self, text: str) -> None:
        self.post("status", text)

    def set_recovery(self, pending: dict[str, Any] | None) -> None:
        self.state.recovery = {key: str((pending or {}).get(key, ""))[:300] for key in ("name", "call_id", "arguments")} if pending else None
        if pending:
            self.state.mode = "recovery"
            self.state.task_state = "recovery"
        self._dirty = True

    def set_background(self, tasks: list[dict[str, Any]]) -> None:
        normalized = [{key: str(item.get(key, ""))[:240] for key in ("id", "status", "goal", "result", "error")} for item in tasks]
        if normalized != self.state.background:
            self.post("background", normalized)

    def append_assistant(self, text: str) -> None:
        self.post("assistant", text)

    def bind_approval(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        self._approval_context[(call_id, name)] = dict(arguments)

    def request_approval(self, name: str, risk: object, arguments: dict[str, Any] | None = None) -> bool:
        resolved = dict(arguments or {})
        for (call_id, tool_name), value in self._approval_context.items():
            if tool_name == name:
                resolved = value
                del self._approval_context[(call_id, tool_name)]
                break
        request = _Approval(name, risk, resolved, None, threading.Event())
        self.post("approval", request)
        while not request.done.wait(0.05):
            if self._stop:
                request.answer = "n"
                request.done.set()
                return False
        return request.answer in {"y", "yes", "a", "always", "本会话"}

    def _consume(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "user":
                self.state.add_user(str(payload))
            elif kind == "assistant":
                self.state.add_assistant(str(payload))
            elif kind == "status":
                self.state.status_text = str(payload)
                value = str(payload) if payload else "ready"
                self.state.task_state = value if value in {"working", "ready", "failed", "stopped", "paused", "recovery"} else ("working" if value in {"thinking", "loading models…"} else self.state.task_state)
                self.state.mode = "working" if value not in {"ready", "failed", "stopped"} else "ready"
            elif kind == "tool":
                event_kind, event_payload = payload
                self.state.tool_status(event_kind, event_payload)
            elif kind == "background":
                self.state.set_background(payload if isinstance(payload, list) else [])
            elif kind == "model_options" and self.modal and self.modal.get("kind") == "select":
                options = [str(item) for item in (payload or []) if str(item)]
                if options:
                    current = self.modal.get("current")
                    if current in options:
                        options = [current] + [item for item in options if item != current]
                    self.modal["options"] = options
                    self.modal["index"] = "0"
                    self.modal["loading"] = False
                    self._dirty = True
            elif kind == "approval":
                self._approval = payload
                self.state.mode = "approval"
                self.state.tool_status("approval.pending", {"call_id": "approval", "name": payload.name, "risk": str(payload.risk), "arguments": payload.arguments})
            elif kind == "recovery_action":
                if hasattr(self, "recovery_handler"):
                    self.recovery_handler(str(payload))
                self.state.recovery = None
                self.state.mode = "working" if payload in {"resume", "discard", "mark_failed"} else "ready"
            elif kind == "approval_answer":
                if self._approval is not None:
                    self._approval.answer = str(payload)
                    self._approval.done.set()
                    self._approval = None
                    self.state.mode = "working"
            elif kind == "quit":
                self._stop = True

    def _frame(self) -> str:
        size = get_terminal_size((88, 24))
        current = (size.columns, size.lines)
        if current != self._last_size:
            self._last_size = current
            self._dirty = True
        frame = self.state.render(current[0], current[1])
        if self.modal:
            if self.modal.get("kind") == "select":
                options = self.modal["options"]
                index = int(self.modal["index"])
                choices = "\n".join(("│ ❯ " if i == index else "│   ") + item for i, item in enumerate(options))
                frame += "\n\n╭─ " + self.modal["title"] + " " + "─" * 24 + "╮\n" + choices + "\n│ ↑↓ choose · Enter accept · Esc cancel\n╰" + "─" * 38 + "╯"
            else:
                field, secret = self.modal["fields"][int(self.modal["index"])]
                shown = "•" * len(self.modal["value"]) if secret else self.modal["value"]
                frame += "\n\n╭─ " + self.modal["title"] + " " + "─" * 24 + "╮\n│ " + field + ": " + shown + "\n│ Enter next · Ctrl-N newline · Esc cancel\n╰" + "─" * 38 + "╯"
        return "\033[2J\033[H" + frame

    def _draw(self) -> None:
        self._consume()
        if not self._dirty:
            return
        self.output.write(self._frame())
        self.output.flush()
        self._dirty = False

    def _read_key(self, fd: int) -> str | None:
        ready, _, _ = select.select([fd], [], [], 0.08)
        if not ready:
            return None
        key = os.read(fd, 1).decode("utf-8", "replace")
        if key == "\x1b":
            ready, _, _ = select.select([fd], [], [], 0.02)
            if ready:
                seq = os.read(fd, 2).decode("utf-8", "replace")
                if seq in {"[5" , "[6"}:
                    os.read(fd, 1)
                    return "pageup" if seq == "[5" else "pagedown"
                return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(seq, "escape")
            return "escape"
        if key in {"\r", "\n"}:
            return "enter"
        if key == "\x0e":
            return "newline"
        if key in {"\x7f", "\b"}:
            return "backspace"
        if key == "\x03":
            return "cancel"
        if key == "\x04":
            return "eof"
        return key

    def run(self, on_submit: Callable[[str], None], on_approval: Callable[[str, object, dict[str, Any]], None] | None = None) -> None:
        if termios is None or tty is None or not sys.stdin.isatty() or not hasattr(termios, "tcgetattr"):
            raise RuntimeError("TUI_REQUIRES_TTY")
        fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self._draw()
            while not self._stop:
                if hasattr(self, "background_provider"):
                    self.set_background(self.background_provider())
                self._draw()
                try:
                    key = self._read_key(fd)
                except KeyboardInterrupt:
                    key = "cancel"
                if key is None:
                    continue
                if self.modal is not None:
                    if self.modal.get("kind") == "select":
                        options = self.modal["options"]
                        if key in {"up", "down"}:
                            step = -1 if key == "up" else 1
                            self.modal["index"] = str((int(self.modal["index"]) + step) % len(options))
                            self._dirty = True
                        elif key == "enter":
                            if self.modal.get("loading"):
                                self.set_status("loading models…")
                                continue
                            selected = options[int(self.modal["index"])]
                            callback, self.modal = self.modal_callback, None
                            self.modal_callback = None
                            if callback: callback(selected)
                        elif key in {"escape", "cancel", "eof"}:
                            callback, self.modal = self.modal_callback, None
                            self.modal_callback = None
                            if callback: callback(None)
                        continue
                    if key in {"escape", "cancel", "eof"}:
                        callback, self.modal = self.modal_callback, None
                        self.modal_callback = None
                        if callback: callback(None)
                    elif key == "enter":
                        fields = self.modal["fields"]
                        index = int(self.modal["index"])
                        self.modal["values"][fields[index][0]] = self.modal["value"]
                        if index + 1 < len(fields):
                            self.modal["index"] = str(index + 1)
                            self.modal["value"] = ""
                        else:
                            values = dict(self.modal["values"])
                            callback, self.modal = self.modal_callback, None
                            self.modal_callback = None
                            if callback: callback(values)
                    elif key == "newline":
                        self.modal["value"] += "\n"
                    elif key == "backspace":
                        self.modal["value"] = self.modal["value"][:-1]
                    elif len(key) == 1 and key.isprintable():
                        self.modal["value"] += key
                    self._dirty = True
                    continue
                if self._approval is not None:
                    if key in {"y", "a", "n"}:
                        self.post("approval_answer", key)
                    elif key in {"cancel", "eof", "escape"}:
                        self.post("approval_answer", "n")
                    continue
                if key == "enter":
                    text = self.state.composer.strip()
                    if text == "/" and self.commands:
                        text = self.commands[self._suggestion_index]
                    self.state.composer = ""
                    if text:
                        self.post("user", text)
                        on_submit(text)
                elif key == "newline":
                    self.state.composer += "\n"
                    self._dirty = True
                elif key == "backspace":
                    self.state.composer = self.state.composer[:-1]
                elif key in {"r", "d", "f", "s"} and self.state.recovery:
                    self.post("recovery_action", {"r": "resume", "d": "discard", "f": "mark_failed", "s": "stop"}[key])
                elif key == "cancel":
                    self.state.composer = ""
                    self.set_status("cancelled")
                elif key == "eof":
                    self._stop = True
                elif key == "pageup":
                    self.state.scroll(-5)
                    self._dirty = True
                elif key == "pagedown":
                    self.state.scroll(5)
                    self._dirty = True
                elif key == "up":
                    if self.state.composer.startswith("/") and self.commands:
                        self._suggestion_index = (self._suggestion_index - 1) % len(self.commands)
                        self.state.composer = self.commands[self._suggestion_index]
                    else:
                        self.state.history(-1)
                    self._dirty = True
                elif key == "down":
                    if self.state.composer.startswith("/") and self.commands:
                        self._suggestion_index = (self._suggestion_index + 1) % len(self.commands)
                        self.state.composer = self.commands[self._suggestion_index]
                    else:
                        self.state.history(1)
                    self._dirty = True
                elif len(key) == 1 and key.isprintable():
                    self.state.composer += key
        finally:
            self._stop = True
            if self._approval is not None:
                self._approval.answer = "n"
                self._approval.done.set()
            if self._old_termios is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._old_termios)
            self.output.write("\033[0m\033[?25h\n")
            self.output.flush()
