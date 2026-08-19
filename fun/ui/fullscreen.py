"""Alternate-screen frontend for a fixed, dense layout.

Selected with ``--fullscreen``.  The session takes over the screen, draws a
stable layout, and restores the terminal exactly as it was on exit.  Repaints
are diffed line by line, so an idle spinner rewrites one row rather than the
whole screen.

Trade-off worth knowing: the alternate screen has no scrollback, so history is
navigated with PgUp/PgDn inside the app and cannot be selected with the mouse
the way the streaming frontend allows.
"""
from __future__ import annotations

from typing import Any, TextIO

from .layout import frame_canvas
from .screen import ScreenWriter
from .text import display_width
from .state import UiState


class FullscreenSurface:
    """Paints a Fun session into the alternate screen buffer."""

    name = "fullscreen"
    supports_scrollback = False

    def __init__(self, output: TextIO | None = None, theme: Any = None) -> None:
        self.writer = ScreenWriter(output, background=theme.canvas() if theme is not None else "")
        self.theme = theme

    def start(self) -> None:
        self.writer.enter()

    def paint(self, state: UiState, width: int, height: int, overlay: list[str] | None = None) -> None:
        # Everything is repainted from state, so nothing is ever "flushed away".
        state.flushed = len(state.transcript)
        if overlay:
            # The dialog is centred and given its own rows just above the input
            # panel, so it belongs to the thing that opened it and never erases
            # the transcript behind it.
            overlay_width = max((display_width(line) for line in overlay), default=0)
            pad = " " * max(0, (width - overlay_width) // 2)
            reserved = [""] + [pad + line for line in overlay] + [""]
            frame = state.compose(width - 4, height - 2, reserved)
        else:
            frame = state.compose(width - 4, height - 2)
        frame = frame_canvas(
            state.theme, frame, width, height,
            session=state.session_label, workspace=state.workspace,
            mode=state.agent_mode, approval=state.approval_mode, version=state.version,
        )
        self.writer.draw(frame, width, height)
        hint = state.cursor_hint
        if hint is not None and 0 <= hint[0] + 1 < height:
            # The border adds one row above and two columns to the left, and the
            # column is clamped to the screen for the same reason as in stream.
            self.writer.place_cursor(hint[0] + 2, max(0, min(hint[1] + 3, max(0, width - 1))))
        else:
            self.writer.hide_cursor()

    def stop(self) -> None:
        self.writer.close()
