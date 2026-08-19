"""Backwards-compatible alias for the interactive application loop.

``TerminalUI`` was the single class that owned key decoding, modals, the event
queue and the escape codes.  Those concerns now live in :mod:`fun.ui.app`,
:mod:`fun.ui.modal`, :mod:`fun.ui.input` and the surface modules.  This shim
preserves the old constructor signature.
"""
from __future__ import annotations

from typing import Any, Callable

from .ui.app import App, ApprovalRequest
from .ui.stream import StreamSurface
from .ui.theme import Theme


class TerminalUI(App):
    """An :class:`~fun.ui.app.App` defaulting to the streaming surface."""

    def __init__(self, locale: str = "", output: Any = None, commands: list[str] | None = None, theme: Theme | None = None, surface: Any = None) -> None:
        super().__init__(surface or StreamSurface(output), theme=theme or Theme.detect(), locale=locale, commands=commands, output=output)

    # Names kept from the previous public surface.
    def open_prompt_modal(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        self.open_prompt(title, initial, callback)

    def open_modal(self, title: str, fields: list[Any], callback: Callable[[dict[str, str] | None], None]) -> None:
        self.open_form(title, fields, callback)

    def set_recovery(self, pending: dict[str, Any] | None) -> None:
        self.post("recovery", pending)

    def set_background(self, tasks: list[dict[str, Any]]) -> None:
        self.post("background", tasks)


__all__ = ["TerminalUI", "App", "ApprovalRequest"]
