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
from pathlib import Path

from .ui.app import App
from .ui.completion import mentions
from .ui.text import truncate
from .ui.theme import Theme


def friendly_error(exc: Exception, locale: str) -> str:
    """Map a provider failure onto a localised message the user can act on.

    Every ``ProviderError`` tag is covered.  Previously only six were, and an
    uncovered one — ``PROVIDER_HTTP_FAILED`` above all, which is every non-auth
    HTTP status there is — fell through to ``str(exc)`` and printed the raw tag
    on screen.  "× PROVIDER_HTTP_FAILED" tells the user that something failed
    and nothing else: not what failed, not whose fault it is, not what to do.
    """
    tag = getattr(exc, "error_tag", "")
    if not tag:
        return str(exc)
    status = int(getattr(exc, "status", 0) or 0)
    if tag == "PROVIDER_HTTP_FAILED":
        # The status is the whole diagnosis here: 404 is a wrong address, 429 is
        # rate limiting, 5xx is not the user's fault at all.
        if status == 404:
            key = "provider_http_404"
        elif status == 429:
            key = "provider_http_429"
        elif 500 <= status <= 599:
            key = "provider_http_5xx"
        elif 400 <= status <= 499:
            key = "provider_http_400"
        else:
            key = "provider_http"
        message = t(locale, key).format(status=status or "?")
    else:
        keys = {
            "PROVIDER_AUTH_FAILED": "provider_auth",
            "PROVIDER_NETWORK_FAILED": "provider_network",
            "PROVIDER_REQUEST_FAILED": "provider_network",
            "PROVIDER_TIMEOUT": "provider_timeout",
            "PROVIDER_MALFORMED_EVENT": "provider_bad_response",
            "PROVIDER_UNEXPECTED_CONTENT_TYPE": "provider_bad_response",
            "PROVIDER_INVALID_STATUS": "provider_bad_response",
            "PROVIDER_EMPTY_STREAM": "provider_empty_stream",
            "PROVIDER_PAYLOAD_TOO_LARGE": "provider_payload_too_large",
            "PROVIDER_INVALID_PAYLOAD": "provider_invalid_request",
            "PROVIDER_INVALID_MESSAGES": "provider_invalid_request",
            "PROVIDER_INVALID_TOOLS": "provider_invalid_request",
        }
        # An unknown tag still gets a sentence, with the tag as a parenthetical
        # detail rather than as the entire message.
        message = t(locale, keys[tag]) if tag in keys else f"{t(locale, 'provider_unavailable')} ({tag})"
    endpoint = getattr(exc, "endpoint", "")
    key_hint = getattr(exc, "key_hint", "")
    if tag == "PROVIDER_AUTH_FAILED" and (endpoint or key_hint):
        # Name the address and identify the key without printing it, so a
        # rejected key is a fact the user can check rather than a mystery.
        message += "\n" + " · ".join(part for part in (endpoint, key_hint) if part)
    elif tag == "PROVIDER_HTTP_FAILED" and endpoint:
        # The address, but not the key: a 404 or a 429 is not the key's fault,
        # and pointing at the key sends the user off to reconfigure a
        # credential that was working fine.
        message += "\n" + endpoint
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

    def select(self, title: str, options: Sequence[str], callback: Callable[[Any], None], loader: Callable[[], list[str]] | None = None, multi: bool = False, chosen: Sequence[str] = ()) -> None:
        token = self.app.open_select(title, list(options), callback, multi=multi, chosen=chosen)
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

    def select(self, title: str, options: Sequence[str], callback: Callable[[Any], None], loader: Callable[[], list[str]] | None = None, multi: bool = False, chosen: Sequence[str] = ()) -> None:
        choices = list(options)
        if loader is not None:
            try:
                loaded = loader()
                if loaded:
                    choices = loaded
            except Exception as exc:
                self.say("× " + friendly_error(exc, self.locale))
        if not choices:
            typed = self._ask("  value: ")
            callback([typed] if multi and typed else typed)
            return
        self.say(title)
        for index, option in enumerate(choices, 1):
            self.say(f"  [{index}] {option}")
        answer = self._ask(f"  choose [1-{len(choices)}]{' (comma-separated for several)' if multi else ''} ")
        if answer is None:
            callback(None)
            return
        picked = []
        for part in answer.replace(",", " ").split():
            if part.isdigit() and 1 <= int(part) <= len(choices):
                option = choices[int(part) - 1]
                if option not in picked:
                    picked.append(option)
        if multi:
            callback(picked)
            return
        callback(picked[0] if picked else None)

    def edit(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        self.say(f"{title} (current: {truncate(initial, 60) or 'empty'})")
        callback(self._ask("  value: "))

    def quit(self) -> None:
        self.stopped = True


def attach_mentions(text: str, root: str) -> tuple[str, list[str]]:
    """Turn ``@path`` references into an instruction the model cannot miss.

    The composer offers "@ files" and completes real workspace paths, but
    nothing downstream ever read them back: the reference travelled as plain
    prose and whether the file was opened came down to the model's mood.  Now
    the referenced paths are listed explicitly, and ones that do not exist are
    reported to the user instead of silently meaning nothing.
    """
    found = mentions(text)
    if not found:
        return text, []
    base = Path(root)
    resolved: list[str] = []
    missing: list[str] = []
    for path in found:
        try:
            target = (base / path).resolve()
            inside = target.is_relative_to(base.resolve())
        except (OSError, ValueError):
            target, inside = None, False
        if target is not None and inside and target.exists():
            resolved.append(path)
        else:
            missing.append(path)
    if not resolved:
        return text, missing
    listing = "\n".join(f"- {path}" for path in resolved)
    return text + "\n\nFiles the user referenced with @ (open each with the read tool before answering):\n" + listing, missing


def run_goal(session: Session, frontend: Any, text: str, on_text: Callable[[str], None] | None = None, on_status: Callable[[str, dict[str, Any]], None] | None = None) -> None:
    """Create a task for ``text`` and drive one model turn to completion."""
    runtime = session.runtime
    if runtime.provider is None:
        frontend.say(t(frontend.locale, "offline"))
        return
    text, missing = attach_mentions(text, runtime.tools.guard.root)
    for name in missing:
        # Say it now.  A mention that resolves to nothing used to be sent as
        # ordinary prose, and the model quietly answered without ever opening
        # the file the user thought they had handed it.
        frontend.say("× " + t(frontend.locale, "mention_missing").format(path=name))
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
            # Leave a mark in the transcript, not only in the status bar: the
            # half-finished reply above otherwise reads, on the way back up the
            # scrollback, exactly like a reply that finished.
            frontend.say(t(frontend.locale, "turn_interrupted"))
            frontend.status("stopped")
            return
        frontend.say("× " + friendly_error(exc, frontend.locale))
        frontend.status("failed")
