"""Default frontend: streaming output with a repainted bottom dock.

Settled transcript items are printed into the terminal's normal scrollback, so
the conversation can be scrolled with the terminal's own scrollbar, selected and
copied, and piped to a file.  Only the live tail — the assistant message still
streaming, the tool still running — plus the status bar and composer live in the
repainted dock at the bottom.
"""
from __future__ import annotations

from typing import TextIO

from .screen import DockWriter
from .state import UiState


class StreamSurface:
    """Paints a Fun session without ever clearing the screen."""

    name = "stream"
    supports_scrollback = True

    def __init__(self, output: TextIO | None = None) -> None:
        self.writer = DockWriter(output)
        self._started = False
        self._intro_written = False

    def start(self) -> None:
        self._started = True

    def paint(self, state: UiState, width: int, height: int, overlay: list[str] | None = None) -> None:
        if not self._intro_written:
            # Printed once into scrollback, so it scrolls away like any other
            # output instead of being repainted on every keystroke.
            self._intro_written = True
            self.writer.write_above("\n".join(state.intro_lines(width)))
        settled = state.flush(width)
        if settled:
            self.writer.write_above("\n".join(settled))
        live = state.live_lines(width)
        dock = live + state.dock_lines(width)
        if overlay:
            dock = dock + [""] + overlay
        # Never let the dock exceed the viewport, or it would scroll itself away.
        trimmed = 0
        if height and len(dock) > max(4, height - 1):
            trimmed = len(dock) - max(4, height - 1)
            dock = dock[trimmed:]
        self.writer.draw(dock)
        caret = state.dock_caret
        if caret is not None:
            # dock_caret is measured against dock_lines alone; the drawn dock is
            # preceded by the live tail and may then have been trimmed from the
            # top, so both shifts apply.  Missing the trim put the cursor on the
            # hint bar instead of the composer.
            row = caret[0] + len(live) - trimmed
            # The column is bounded too.  It is measured against the composer's
            # own budget, and a caret past the terminal's last column emits a
            # cursor-forward the terminal clamps wherever it likes — which is
            # how the caret ended up sitting on the ellipsis rather than after
            # the text.
            column = max(0, min(caret[1], max(0, width - 1)))
            if 0 <= row < len(dock):
                self.writer.place_cursor(row, column)

    def write_direct(self, text: str) -> None:
        """Print text straight to scrollback, used for one-shot command output."""
        self.writer.write_above(text)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.writer.close()
