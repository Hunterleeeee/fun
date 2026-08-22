"""Incremental frame writers.

The old UI repainted by sending ``\\033[2J`` (erase everything) on every frame.
That flickers, destroys the terminal's scrollback so nothing can be copied, and
sends a full screen of bytes per keystroke over SSH.  Both writers here avoid
that by only emitting what actually changed:

``DockWriter``    keeps history in the normal scrollback and repaints just the
                  bottom dock (status bar, composer, hints) in place.
``ScreenWriter``  runs in the alternate screen and repaints only the lines whose
                  content differs from the previous frame.
"""
from __future__ import annotations

import sys
from typing import TextIO

from .text import display_width, truncate

ENTER_ALT_SCREEN = "\033[?1049h"
LEAVE_ALT_SCREEN = "\033[?1049l"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ERASE_LINE = "\033[2K"
ERASE_BELOW = "\033[0J"
RESET = "\033[0m"


def _cursor_to(row: int, column: int = 1) -> str:
    return f"\033[{row};{column}H"


class DockWriter:
    """Bottom-anchored repainting that preserves scrollback.

    Transcript output goes through :meth:`write_above`, which lifts the dock out
    of the way, prints the new content so the terminal scrolls it into history
    like any ordinary program output, then paints the dock back underneath.
    """

    def __init__(self, output: TextIO | None = None) -> None:
        self.output = output or sys.stdout
        self._dock_lines: list[str] = []
        self._active = False
        # Which dock row the cursor is on.  ``_erase_dock`` used to assume the
        # last one, but ``place_cursor`` deliberately parks it on the composer,
        # so every repaint walked up too far and erased that many rows of the
        # user's scrollback.
        self._cursor_row = 0

    @property
    def height(self) -> int:
        return len(self._dock_lines)

    def _erase_dock(self) -> str:
        if not self._dock_lines:
            return ""
        # Move up from wherever the cursor actually is to the dock's first row,
        # then erase from there down.
        up = max(0, min(self._cursor_row, len(self._dock_lines) - 1))
        return ("\033[F" * up if up > 0 else "") + "\r" + ERASE_BELOW

    def write_above(self, text: str) -> None:
        """Print ``text`` into scrollback without disturbing the dock."""
        if not text:
            return
        payload = self._erase_dock() + text
        if not payload.endswith("\n"):
            payload += "\n"
        dock = self._dock_lines
        self._dock_lines = []
        self._cursor_row = 0
        self.output.write(payload)
        self.output.flush()
        if dock:
            self.draw(dock)

    def draw(self, lines: list[str]) -> None:
        """Repaint the dock, skipping the write entirely when nothing changed."""
        if lines == self._dock_lines and self._active:
            return
        payload = self._erase_dock()
        self._dock_lines = list(lines)
        self._active = True
        payload += "\n".join(ERASE_LINE + line for line in self._dock_lines)
        self.output.write(payload)
        self.output.flush()
        self._cursor_row = max(0, len(self._dock_lines) - 1)

    def place_cursor(self, row_from_top: int, column: int) -> None:
        """Move the cursor into the dock, counting rows from the dock's top."""
        if not self._dock_lines:
            return
        row_from_top = max(0, min(row_from_top, len(self._dock_lines) - 1))
        up = self._cursor_row - row_from_top
        payload = ("\033[F" * up if up > 0 else "") + "\r"
        if column > 0:
            payload += f"\033[{column}C"
        self.output.write(payload + SHOW_CURSOR)
        self.output.flush()
        self._cursor_row = row_from_top

    def clear(self) -> None:
        if not self._dock_lines:
            return
        self.output.write(self._erase_dock() + RESET)
        self.output.flush()
        self._dock_lines = []
        self._active = False
        self._cursor_row = 0

    def close(self) -> None:
        self.clear()
        self.output.write(SHOW_CURSOR + RESET)
        self.output.flush()


class ScreenWriter:
    """Alternate-screen writer that repaints only the rows that changed."""

    def __init__(self, output: TextIO | None = None, background: str = "") -> None:
        self.output = output or sys.stdout
        self._previous: list[str] = []
        self._entered = False
        self._size: tuple[int, int] | None = None
        self.background = background

    def write_control(self, sequence: str) -> None:
        """Emit a raw control sequence (mouse reporting, and the like)."""
        if not sequence:
            return
        self.output.write(sequence)
        self.output.flush()

    def _fill(self, line: str, width: int) -> str:
        """Pad a row to the full width and keep the canvas colour behind it.

        Any reset inside the line would also clear the background, so the
        background sequence is re-applied after each one.
        """
        if not self.background:
            return line
        body = line.replace(RESET, RESET + self.background)
        padding = " " * max(0, width - display_width(line))
        return self.background + body + padding + RESET

    def enter(self) -> None:
        if self._entered:
            return
        self._entered = True
        self._previous = []
        self.output.write(ENTER_ALT_SCREEN + HIDE_CURSOR + self.background + "\033[2J")
        self.output.flush()

    def leave(self) -> None:
        if not self._entered:
            return
        self._entered = False
        self._previous = []
        self.output.write(RESET + SHOW_CURSOR + LEAVE_ALT_SCREEN)
        self.output.flush()

    def draw(self, lines: list[str], width: int, height: int) -> None:
        """Paint ``lines`` as the whole screen, emitting only changed rows."""
        size = (width, height)
        if size != self._size:
            # A resize invalidates every cached row; force a full repaint.
            self._size = size
            self._previous = []
            self.output.write("\033[2J")
        frame = [self._fill(truncate(line, width), width) for line in lines[:height]]
        frame += [self._fill("", width)] * max(0, height - len(frame))
        payload: list[str] = []
        for index, line in enumerate(frame):
            if index < len(self._previous) and self._previous[index] == line:
                continue
            payload.append(_cursor_to(index + 1) + ERASE_LINE + line)
        if not payload:
            return
        self._previous = frame
        self.output.write("".join(payload) + RESET)
        self.output.flush()

    def place_cursor(self, row: int, column: int) -> None:
        """Park the real terminal cursor at a screen position and reveal it."""
        self.output.write(_cursor_to(max(1, row), max(1, column)) + SHOW_CURSOR)
        self.output.flush()

    def hide_cursor(self) -> None:
        self.output.write(HIDE_CURSOR)
        self.output.flush()

    def close(self) -> None:
        self.leave()


def visible_lines(lines: list[str], width: int) -> list[str]:
    """Clip every line to ``width`` columns without counting escape codes."""
    return [line if display_width(line) <= width else truncate(line, width) for line in lines]
