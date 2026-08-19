"""The interactive application loop.

One thread owns the terminal.  Runtime callbacks fire on model worker threads
and background sub-agent threads, and they are forbidden from touching the
screen — they call :meth:`App.post`, which drops an item on a queue that the UI
thread drains before each paint.  That single-writer rule is what keeps streamed
tokens, tool status and approval prompts from interleaving mid-escape-sequence.

Painting itself is delegated to a *surface* (streaming or fullscreen), so the
loop below knows about keys and state, never about escape codes.
"""
from __future__ import annotations

import queue
import signal
import sys
import threading
from dataclasses import dataclass, field
from shutil import get_terminal_size
from typing import Any, Callable, Protocol

from . import input as keys
from .completion import Completer, CompletionState
from .modal import Modal, field_modal, palette_modal, prompt_modal, select_modal
from .state import UiState, normalize_background
from .theme import Theme


EDIT_KEYS: dict[str, Any] = {
    "left": lambda editor: editor.move_left(),
    "right": lambda editor: editor.move_right(),
    "home": lambda editor: editor.move_home(),
    "end": lambda editor: editor.move_end(),
    "word_left": lambda editor: editor.move_word_left(),
    "word_right": lambda editor: editor.move_word_right(),
    "backspace": lambda editor: editor.backspace(),
    "delete": lambda editor: editor.delete(),
    "kill_to_end": lambda editor: editor.kill_to_end(),
    "kill_to_start": lambda editor: editor.kill_to_start(),
    "kill_word_left": lambda editor: editor.kill_word_left(),
    "kill_word_right": lambda editor: editor.kill_word_right(),
    "yank": lambda editor: editor.yank(),
}


class Surface(Protocol):
    name: str
    supports_scrollback: bool

    def start(self) -> None: ...
    def paint(self, state: UiState, width: int, height: int, overlay: list[str] | None = None) -> None: ...
    def stop(self) -> None: ...


@dataclass
class ApprovalRequest:
    """A tool approval waiting on the user, resolved from the UI thread."""

    name: str
    risk: object
    arguments: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def allowed(self) -> bool:
        return self.answer in {"y", "yes", "a", "always"}

    @property
    def remembered(self) -> bool:
        """Whether the user chose to allow this tool for the rest of the session."""
        return self.answer in {"a", "always"}


class App:
    """Owns UI state, the event queue and the key dispatch loop."""

    def __init__(self, surface: Surface, theme: Theme | None = None, locale: str = "", commands: list[str] | None = None, output=None) -> None:
        self.surface = surface
        self.output = output or sys.stdout
        # Theme carries the locale — it is what every component reads — so a
        # theme built without one has to be told, or App(locale="zh-CN")
        # silently renders an English UI while claiming Chinese.
        resolved = theme or Theme.detect()
        if locale and locale != resolved.locale:
            resolved = Theme(resolved.mode, resolved.unicode, resolved.name, locale)
        self.state = UiState(locale=resolved.locale, theme=resolved)
        self.commands = sorted(commands or [])
        self.completer = Completer(commands={name: "" for name in self.commands})
        self.completion = CompletionState()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.modal: Modal | None = None
        self._submit: Callable[[str], None] | None = None
        self.recovery_handler: Callable[[str], None] | None = None
        self.interrupt_handler: Callable[[], bool] | None = None
        self.mode_handler: Callable[[str], None] | None = None
        self.background_provider: Callable[[], list[dict[str, str]]] | None = None
        self._approval: ApprovalRequest | None = None
        self._painted_size: tuple[int, int] | None = None
        self._reported_background: set[str] = set()
        self._interrupt_armed = False
        self._stop = False
        self._dirty = True

    # ------------------------------------------------------------ thread-safe

    def post(self, kind: str, payload: Any = None) -> None:
        """Enqueue a UI update from any thread."""
        self._dirty = True
        self.events.put((kind, payload))

    def set_status(self, text: str) -> None:
        self.post("status", text)

    def append_assistant(self, text: str) -> None:
        self.post("assistant", text)

    def toast(self, text: str) -> None:
        self.post("toast", text)

    def request_approval(self, name: str, risk: object, arguments: dict[str, Any] | None = None) -> str:
        """Block the calling worker thread until the user answers.

        Returns ``"always"``, ``"yes"`` or ``"no"`` rather than a bool, because
        the caller needs to distinguish "allowed once" from "allowed for the
        session" — collapsing them to True is why the ``a`` key did nothing.
        """
        request = ApprovalRequest(name, risk, dict(arguments or {}))
        self.post("approval", request)
        while not request.done.wait(0.05):
            if self._stop:
                request.answer = "n"
                request.done.set()
                return "no"
        if request.remembered:
            return "always"
        return "yes" if request.allowed else "no"

    # ----------------------------------------------------------------- modals

    def open_prompt(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        self.modal = prompt_modal(title, initial, callback)
        self._dirty = True

    def open_form(self, title: str, fields: list[Any], callback: Callable[[dict[str, str] | None], None]) -> None:
        self.modal = field_modal(title, fields, callback)
        self._dirty = True

    def open_select(self, title: str, options: list[str], callback: Callable[[str | None], None]) -> str:
        """Open a picker and return its token, for work loaded on its behalf."""
        self.modal = select_modal(title, options, callback)
        self._dirty = True
        return self.modal.token

    # ------------------------------------------------------------ event drain

    def _consume(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            self._apply(kind, payload)
            self._dirty = True

    def _apply(self, kind: str, payload: Any) -> None:
        state = self.state
        if kind == "user":
            text = str(payload)
            state.add_command(text) if text.startswith("/") else state.add_user(text)
        elif kind == "assistant":
            state.add_assistant(str(payload))
        elif kind == "system":
            state.add_system(str(payload))
        elif kind == "clear":
            state.transcript.clear()
            state.tools.clear()
            state.flushed = 0
            state.goal = ""
            state.set_plan([], [])
        elif kind == "turn":
            state.set_turn_footer(str(payload))
        elif kind == "toast":
            state.toast = str(payload)
            state.toast_ticks = 0
        elif kind == "status":
            self._apply_status(str(payload) if payload else "ready")
        elif kind == "plan":
            steps, statuses = payload
            state.set_plan(steps, statuses)
        elif kind == "usage":
            state.usage_text = str(payload)
        elif kind == "tool":
            event_kind, event_payload = payload
            state.tool_status(event_kind, event_payload)
        elif kind == "background":
            state.set_background(payload if isinstance(payload, list) else [])
        elif kind == "model_options":
            token, values = payload if isinstance(payload, tuple) else ("", payload)
            if self.modal is None or self.modal.kind != "select" or self.modal.token != token:
                # The dialog this list was loaded for is gone.  Applying it to
                # whatever is open now replaced the approval-mode choices with
                # model names, and the user's pick was then silently rejected.
                return
            options = [str(item) for item in (values or []) if str(item)]
            if options:
                current = self.modal.options[self.modal.index] if self.modal.options else ""
                if current in options:
                    options = [current] + [item for item in options if item != current]
                self.modal.options = options
                self.modal.index = 0
                self.modal.loading = False
        elif kind == "approval":
            # No card is created here.  run_tool already emitted
            # on_status("approval.pending", ...) for the *real* call id, so this
            # made a second, permanently unsettled card carrying the Runtime's
            # internal approval subject ("exec:ls") and str(Risk.MEDIUM) — and
            # because flushable() stops at the first unsettled item, that
            # phantom froze scrollback flushing for the rest of the session.
            self._approval = payload
            state.mode = "approval"
        elif kind == "approval_answer":
            if self._approval is not None:
                self._approval.answer = str(payload)
                self._approval.done.set()
                self._approval = None
                state.mode = "working"
        elif kind == "recovery":
            state.set_recovery(payload)
        elif kind == "recovery_action":
            action = payload.get("action") if isinstance(payload, dict) else payload
            action = str(action)
            if self.recovery_handler is not None:
                self.recovery_handler(action)
            state.recovery = None
            state.mode = "working" if action in {"resume", "discard", "mark_failed"} else "ready"
        elif kind == "quit":
            self._stop = True

    def _apply_status(self, value: str) -> None:
        state = self.state
        state.status_text = value
        settled = {"ready", "failed", "stopped", "paused", "recovery", "completed"}
        if value in settled:
            state.task_state = value
            state.mode = "recovery" if value == "recovery" else "ready"
        elif value == "working" or value.startswith(("thinking", "loading")):
            state.task_state = "working"
            state.mode = "working"

    # ------------------------------------------------------------------- keys

    def _refresh_completion(self) -> None:
        self.completion.refresh(self.completer, self.state.editor.text, self.state.editor.cursor)
        self.state.completion = self.completion

    def _complete(self, key: str) -> None:
        """Handle a key while the completion popup owns it."""
        if key == "escape":
            self.completion.clear()
        elif key == "up":
            self.completion.move(-1)
        elif key == "down":
            self.completion.move(1)
        else:  # tab or enter accept the highlighted candidate
            choice = self.completion.selected()
            context = self.completion.context
            if choice and context:
                text, cursor = self.completer.apply(self.state.editor.text, context, choice)
                self.state.editor.set(text)
                self.state.editor.cursor = cursor
            self.completion.clear()
        self._dirty = True

    def _open_palette(self) -> None:
        """Ctrl-P: every command in one grouped, searchable surface.

        The rows are built from the same registry the ``/`` completion reads,
        so a newly registered command appears here without any extra wiring;
        only the presentation differs.  Commands that need an argument are not
        run on the spot — they are typed into the composer with the cursor
        after them, because the palette has nowhere to ask for the argument.
        """
        from ..commands import grouped_commands

        theme = self.state.theme
        groups = [
            (theme.text(f"ui_group_{group}"), [(command.name, f"{command.name}  {command.describe(theme.locale)}", command.key) for command in commands])
            for group, commands in grouped_commands()
        ]
        self.completion.clear()
        self.modal = palette_modal(theme.text("ui_palette_title"), groups, self._run_palette_choice)
        self.set_status("command palette")
        self._dirty = True

    def _run_palette_choice(self, name: str | None) -> None:
        """Act on the palette's result once the modal has closed."""
        from ..commands import REGISTRY

        if not name:
            # set_status("") maps to "ready", a *settled* state: cancelling the
            # palette mid-turn stopped the spinner and claimed the turn was done
            # while tokens were still arriving.
            self.set_status(self.state.status_text or ("working" if self.state.mode == "working" else "ready"))
            return
        command = REGISTRY.get(name)
        editor = self.state.editor
        if command is not None and command.takes_argument:
            editor.set(f"{name} ")
            editor.cursor = len(editor.text)
            self.set_status(command.summary)
            return
        editor.clear()
        self.post("user", name)
        if self._submit is not None:
            self._submit(name)

    def _cycle_mode(self) -> None:
        """Tab moves through the agent modes and tells the Runtime about it."""
        from ..policy import AGENT_MODES

        current = self.state.agent_mode
        index = AGENT_MODES.index(current) if current in AGENT_MODES else 0
        selected = AGENT_MODES[(index + 1) % len(AGENT_MODES)]
        self.state.agent_mode = selected
        if self.mode_handler is not None:
            self.mode_handler(selected)
        self.set_status(f"mode={selected}")

    def _cancel(self) -> None:
        """Resolve Ctrl-C against the most specific thing it could cancel.

        The order matters: cancelling a draft should never also kill the
        session, and killing the session should never be a single keystroke
        while work is in flight.  So Ctrl-C walks from most local to most
        drastic, and only exits on a deliberate second press.
        """
        state = self.state
        if state.editor.text:
            state.editor.clear()
            self._interrupt_armed = False
            self.set_status("draft cleared")
        elif self.interrupt_handler is not None and self.interrupt_handler():
            self._interrupt_armed = False
            self.set_status("interrupted")
        elif self._interrupt_armed:
            self._stop = True
        else:
            self._interrupt_armed = True
            state.toast = ""
            self.set_status("press Ctrl-C again to exit")
        self._dirty = True

    def _handle_key(self, key: str, on_submit: Callable[[str], None]) -> None:
        state = self.state
        if key != "cancel":
            self._interrupt_armed = False
        pasted = keys.paste_text(key)
        if pasted is not None:
            # A paste is content, never control: it must not be able to press
            # Enter, cancel a dialog or trigger a binding, whatever it contains.
            if self.modal is not None:
                self.modal.handle(key)
            elif self._approval is None and not state.recovery:
                state.editor.insert(pasted)
                self._refresh_completion()
            self._dirty = True
            return
        if self.modal is not None:
            current = self.modal
            if current.handle(key):
                # The callback runs inside handle(), and a command such as
                # /config opens its own modal from there.  Clearing the slot
                # unconditionally threw that new modal away, which is why
                # /config was unreachable from the palette.
                if self.modal is current:
                    self.modal = None
            self._dirty = True
            return
        if self._approval is not None:
            if key in {"y", "a", "n"}:
                self.post("approval_answer", key)
            elif key in {"cancel", "eof", "escape"}:
                self.post("approval_answer", "n")
            return
        if state.recovery:
            # A pending recovery blocks the session: a new goal cannot start
            # until it is resolved, so the composer is not usable and should not
            # pretend to be.  Previously it accepted every key *except* r/d/f/s,
            # which meant typing "restart from scratch" silently resumed the
            # task on its first character and then dropped the rest.
            if key in {"r", "d", "f", "s"}:
                self.post("recovery_action", {"r": "resume", "d": "discard", "f": "mark_failed", "s": "stop"}[key])
            elif key in {"cancel", "eof"}:
                self._cancel()
            self._dirty = True
            return
        editor = state.editor
        if self.completion.active and key in {"tab", "up", "down", "enter", "escape"}:
            self._complete(key)
            return
        if key == "enter":
            text = editor.text.strip()
            editor.clear()
            self._dirty = True
            if text:
                self.post("user", text)
                on_submit(text)
        elif key == "newline":
            editor.newline()
        elif key in EDIT_KEYS:
            EDIT_KEYS[key](editor)
        elif key == "cancel":
            self._cancel()
            # Refresh before returning: the early return left the popup active
            # with a context describing text that no longer exists, so Enter
            # then "completed" the cleared draft back into the buffer.
            self._refresh_completion()
            self._dirty = True
            return
        elif key == "eof":
            # Ctrl-D leaves only on an empty buffer, like a shell; otherwise it
            # deletes forward, which is what readline users expect.
            if editor.text:
                editor.delete()
            else:
                self._stop = True
        elif key == "redraw":
            self._dirty = True
        elif key == "pageup":
            state.scroll(-5)
        elif key == "pagedown":
            state.scroll(5)
        elif key == "toggle_output":
            state.toggle_output()
        elif key == "tab":
            self._cycle_mode()
        elif key == "palette":
            self._open_palette()
        elif key == "sidebar":
            shown = state.toggle_sidebar()
            self.set_status("sidebar on" if shown else "sidebar off")
        elif key == "up":
            # Inside a multi-line draft the arrows move the cursor; only at the
            # top edge do they fall through to history, the way a shell behaves.
            if not editor.move_up():
                editor.history_previous()
        elif key == "down":
            if not editor.move_down():
                editor.history_next()
        elif keys.is_text(key):
            editor.insert(key)
        self._refresh_completion()
        self._dirty = True

    # ------------------------------------------------------------------- loop

    def size(self) -> tuple[int, int]:
        size = get_terminal_size((88, 24))
        return max(32, size.columns), max(8, size.lines)

    def paint(self, force: bool = False) -> None:
        """Repaint, unless nothing changed and nothing is animating.

        ``_dirty`` was assigned in thirteen places and read in none — the whole
        gate was decoration and every loop iteration repainted.  The queue is
        still drained on every pass; only the drawing is skipped.
        """
        self._consume()
        size = self.size()
        if size != self._painted_size:
            self._dirty = True
            self._painted_size = size
        if self.state.animating():
            self.state.tick()
            self._dirty = True
        elif not self._dirty and not force:
            return
        width, height = size
        overlay = self.modal.lines(self.state.theme, width) if self.modal else None
        self.surface.paint(self.state, width, height, overlay)
        self._dirty = False

    def run(self, on_submit: Callable[[str], None]) -> None:
        if not keys.supports_raw_mode():
            raise RuntimeError("TUI_REQUIRES_TTY")
        self._submit = on_submit
        fd = sys.stdin.fileno()
        self.surface.start()
        # SIGTERM/SIGHUP (closing the terminal window, `kill <pid>`) end the
        # interpreter without unwinding, so `finally` never runs and the user's
        # shell comes back in cbreak mode, inside the alternate screen, with
        # bracketed paste still on.  Turning them into a normal stop is the
        # difference between a clean exit and an unusable terminal.
        restored = self._install_signal_handlers()
        try:
            with keys.RawMode(fd):
                self.paint(force=True)
                while not self._stop:
                    if self.background_provider is not None:
                        # Compare like against like: the state stores these
                        # trimmed, so comparing raw dicts against trimmed ones
                        # never converged for a long goal and repainted forever.
                        tasks = [normalize_background(item) for item in self.background_provider()]
                        if tasks != self.state.background:
                            self.post("background", tasks)
                        self._report_finished_background(tasks)
                    self.paint()
                    try:
                        key = keys.read_key(fd)
                    except KeyboardInterrupt:
                        key = "cancel"
                    if key is not None:
                        self._handle_key(key, on_submit)
        finally:
            self._stop = True
            if self._approval is not None:
                self._approval.answer = "n"
                self._approval.done.set()
            self.surface.stop()
            restored()

    def _report_finished_background(self, tasks: list[dict[str, str]]) -> None:
        """Put a finished sub-agent's answer in the transcript, once.

        The rail shows that a sub-agent finished; the answer is the reason it
        was asked, and it belongs where the conversation is — reported once,
        because the poll sees the finished task on every pass.
        """
        for task in tasks:
            identifier = task.get("id", "")
            if not identifier or identifier in self._reported_background:
                continue
            status = task.get("status", "")
            if status not in {"completed", "failed", "cancelled"}:
                continue
            self._reported_background.add(identifier)
            detail = (task.get("result") or task.get("error") or "").strip()
            label = self.state.theme.text({"completed": "ui_subagent", "failed": "ui_subagent_failed", "cancelled": "ui_subagent_cancelled"}[status])
            self.post("system", f"{label} {identifier}\n{detail}".strip())

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Ask the loop to stop on SIGTERM/SIGHUP.  Returns a restore callable."""
        previous: list[tuple[int, Any]] = []

        def request_stop(_signum: int, _frame: Any) -> None:
            self._stop = True

        for name in ("SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                previous.append((number, signal.signal(number, request_stop)))
            except (ValueError, OSError):
                # Not the main thread, or the platform disallows it.
                continue

        def restore() -> None:
            for number, handler in previous:
                try:
                    signal.signal(number, handler)
                except (ValueError, OSError):
                    pass

        return restore
