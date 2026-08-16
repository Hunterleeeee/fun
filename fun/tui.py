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

    def post(self, kind: str, payload: Any = None) -> None:
        self._dirty = True
        self.events.put((kind, payload))

    def set_status(self, text: str) -> None:
        self.post("status", text)

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
                self.state.task_state = "working" if payload and payload != "ready" else "ready"
                self.state.mode = "working" if payload else "ready"
            elif kind == "tool":
                event_kind, event_payload = payload
                self.state.tool_status(event_kind, event_payload)
            elif kind == "approval":
                self._approval = payload
                self.state.mode = "approval"
                self.state.tool_status("approval.pending", {"call_id": "approval", "name": payload.name, "risk": str(payload.risk), "arguments": payload.arguments})
            elif kind == "approval_answer":
                if self._approval is not None:
                    self._approval.answer = str(payload)
                    self._approval.done.set()
                    self._approval = None
                    self.state.mode = "working"
            elif kind == "quit":
                self._stop = True

    def _frame(self) -> str:
        width = get_terminal_size((88, 24)).columns
        return "\033[2J\033[H" + self.state.render(width)

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
                self._draw()
                key = self._read_key(fd)
                if key is None:
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
                elif key == "backspace":
                    self.state.composer = self.state.composer[:-1]
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
