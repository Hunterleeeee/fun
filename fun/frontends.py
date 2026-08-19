"""Concrete frontends: the interactive app and the plain, pipe-safe fallback.

Both implement the :class:`~fun.commands.Frontend` protocol, so every slash
command works identically whether the session is a full terminal app or a
program reading from a pipe.  The goal runner is shared for the same reason:
one place decides how a model turn reports progress.
"""
from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Sequence

from .commands import Session
from .i18n import t
from .ui.app import App
from .ui.text import truncate
from .ui.theme import Theme


def friendly_error(exc: Exception, locale: str) -> str:
    """Map a provider error tag onto a localised, non-leaking message."""
    tag = getattr(exc, "error_tag", "")
    keys = {
        "PROVIDER_AUTH_FAILED": "provider_auth",
        "PROVIDER_NETWORK_FAILED": "provider_network",
        "PROVIDER_TIMEOUT": "provider_timeout",
        "PROVIDER_MALFORMED_EVENT": "provider_bad_response",
        "PROVIDER_UNEXPECTED_CONTENT_TYPE": "provider_bad_response",
        "PROVIDER_EMPTY_STREAM": "provider_empty_stream",
    }
    if tag not in keys:
        return str(exc)
    message = t(locale, keys[tag])
    endpoint = getattr(exc, "endpoint", "")
    key_hint = getattr(exc, "key_hint", "")
    if tag == "PROVIDER_AUTH_FAILED" and (endpoint or key_hint):
        # Name the address and identify the key without printing it, so a
        # rejected key is a fact the user can check rather than a mystery.
        message += "\n" + " · ".join(part for part in (endpoint, key_hint) if part)
    return message


class AppFrontend:
    """Frontend backed by the interactive terminal app."""

    def __init__(self, app: App, locale: str = "en-US") -> None:
        self.app = app
        self.locale = locale

    def say(self, text: str) -> None:
        if text:
            self.app.post("system", text)

    def notify(self, text: str) -> None:
        self.app.toast(text)

    def status(self, text: str) -> None:
        self.app.set_status(text)

    def clear(self) -> None:
        self.app.post("clear")

    def form(self, title: str, fields: Sequence[Any], callback: Callable[[dict[str, str] | None], None]) -> None:
        self.app.open_form(title, list(fields), callback)

    def select(self, title: str, options: Sequence[str], callback: Callable[[str | None], None], loader: Callable[[], list[str]] | None = None) -> None:
        token = self.app.open_select(title, list(options), callback)
        if loader is None:
            return
        modal = self.app.modal
        if modal is not None:
            modal.loading = True

        def load() -> None:
            try:
                self.app.post("model_options", (token, loader()))
            except Exception as exc:
                self.app.post("system", "× " + friendly_error(exc, self.locale))
                self.app.post("model_options", (token, []))

        threading.Thread(target=load, daemon=True).start()

    def edit(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        self.app.open_prompt(title, initial, callback)

    def apply_theme(self, name: str) -> None:
        """Restyle the running session without restarting it."""
        current = self.app.state.theme
        self.app.state.theme = Theme(current.mode, current.unicode, name, current.locale)

    def quit(self) -> None:
        self.app.post("quit")


class PlainFrontend:
    """Frontend for non-tty sessions: plain prints and line input."""

    def __init__(self, locale: str = "en-US", theme: Theme | None = None, stream=None) -> None:
        self.locale = locale
        self.theme = theme or Theme(mode="none", locale=locale)
        self.stream = stream or sys.stdout
        self.stopped = False

    def say(self, text: str) -> None:
        if text:
            print(text, file=self.stream, flush=True)

    def notify(self, text: str) -> None:
        self.say(self.theme.style(f"✓ {text}", "success"))

    def status(self, text: str) -> None:
        self.say(self.theme.style(f"● {text}", "muted"))

    def clear(self) -> None:
        # A scrollback-preserving frontend has nothing to drop; erasing the
        # user's terminal history is not this command's business.
        return

    def _ask(self, prompt: str, secret: bool = False) -> str | None:
        try:
            if secret:
                import getpass

                return getpass.getpass(prompt).strip()
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(file=self.stream)
            return None

    def form(self, title: str, fields: Sequence[Any], callback: Callable[[dict[str, str] | None], None]) -> None:
        self.say(title)
        values: dict[str, str] = {}
        for item in fields:
            name, secret = (item[0], bool(item[1])) if isinstance(item, tuple) else (item, False)
            answer = self._ask(f"  {name}: ", secret)
            if answer is None:
                callback(None)
                return
            values[name] = answer
        callback(values)

    def select(self, title: str, options: Sequence[str], callback: Callable[[str | None], None], loader: Callable[[], list[str]] | None = None) -> None:
        choices = list(options)
        if loader is not None:
            try:
                loaded = loader()
                if loaded:
                    choices = loaded
            except Exception as exc:
                self.say("× " + friendly_error(exc, self.locale))
        if not choices:
            callback(self._ask("  value: "))
            return
        self.say(title)
        for index, option in enumerate(choices, 1):
            self.say(f"  [{index}] {option}")
        answer = self._ask(f"  choose [1-{len(choices)}] ")
        if answer is None:
            callback(None)
            return
        callback(choices[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(choices) else None)

    def edit(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        self.say(f"{title} (current: {truncate(initial, 60) or 'empty'})")
        callback(self._ask("  value: "))

    def quit(self) -> None:
        self.stopped = True


def run_goal(session: Session, frontend: Any, text: str, on_text: Callable[[str], None] | None = None, on_status: Callable[[str, dict[str, Any]], None] | None = None) -> None:
    """Create a task for ``text`` and drive one model turn to completion."""
    runtime = session.runtime
    if runtime.provider is None:
        frontend.say(t(frontend.locale, "offline"))
        return
    try:
        runtime.create_task(text)
    except RuntimeError as exc:
        # Name the way out.  These are ordinary situations — a paused task, an
        # unresolved call from an interrupted session — and printing the raw
        # tag told the user something was wrong without saying what to do.
        guidance = {
            "TASK_PAUSED": "task_paused",
            "RECOVERY_REQUIRED": "task_recovery",
            "TASK_ALREADY_RUNNING": "task_running",
        }.get(str(exc))
        frontend.say("× " + (t(frontend.locale, guidance) if guidance else str(exc)))
        return
    frontend.status("working")
    try:
        output = runtime.run_model_turn(on_text=on_text, on_status=on_status)
        runtime.complete(output)
        frontend.status("ready")
    except Exception as exc:
        interrupted = isinstance(exc, RuntimeError) and str(exc) in {"TASK_NOT_RUNNING", "EVENT_STORE_CLOSED", "NO_ACTIVE_TASK"}
        try:
            if not interrupted:
                runtime.fail(str(exc))
        except RuntimeError:
            pass
        if interrupted:
            # The user stopped it.  Reporting their own Ctrl-C as an internal
            # error, and then marking the turn failed, was the last step of a
            # clean cancellation being dressed up as a crash.
            frontend.status("stopped")
            return
        frontend.say("× " + friendly_error(exc, frontend.locale))
        frontend.status("failed")
