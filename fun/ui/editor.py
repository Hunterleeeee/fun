"""A real text buffer with a cursor.

The previous composer could only append and backspace: pressing Left did
nothing, and there was no way to fix a typo in the middle of a line without
deleting everything after it.  This module replaces it with an editable buffer
supporting the readline/emacs motions people already have in their fingers.

Two things need care:

* **Positions are character offsets, columns are display widths.**  A cursor
  after two Chinese characters sits at offset 2 but column 4.  Moving up or
  down a visual line has to convert between the two, or the cursor drifts.
* **Word motions must agree with word deletion.**  ``Alt+F`` and ``Ctrl+W``
  share one definition of a word boundary so that moving and killing stay
  symmetric.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .text import display_width, wrap

WORD_SEPARATORS = set(" \t\n/\\.,;:!?'\"()[]{}<>=+-*&|^%$#@~`")


def _is_word_char(char: str) -> bool:
    return char not in WORD_SEPARATORS


@dataclass
class Editor:
    """An editable multi-line buffer with a cursor and kill ring."""

    text: str = ""
    cursor: int = 0
    history: list[str] = field(default_factory=list)
    history_index: int | None = None
    pending: str = ""
    killed: str = ""

    # ------------------------------------------------------------- internals

    def _clamp(self) -> None:
        self.cursor = max(0, min(len(self.text), self.cursor))

    @property
    def line_start(self) -> int:
        return self.text.rfind("\n", 0, self.cursor) + 1

    @property
    def line_end(self) -> int:
        found = self.text.find("\n", self.cursor)
        return len(self.text) if found < 0 else found

    @property
    def column(self) -> int:
        """Cursor position measured in terminal columns within its line."""
        return display_width(self.text[self.line_start : self.cursor])

    # -------------------------------------------------------------- mutation

    def insert(self, chunk: str) -> None:
        self.text = self.text[: self.cursor] + chunk + self.text[self.cursor :]
        self.cursor += len(chunk)
        self.history_index = None

    def newline(self) -> None:
        self.insert("\n")

    def backspace(self) -> None:
        if self.cursor <= 0:
            return
        self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
        self.cursor -= 1

    def delete(self) -> None:
        if self.cursor >= len(self.text):
            return
        self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0
        self.history_index = None

    def set(self, value: str) -> None:
        self.text = value
        self.cursor = len(value)

    # ---------------------------------------------------------------- motion

    def move_left(self) -> None:
        self.cursor -= 1
        self._clamp()

    def move_right(self) -> None:
        self.cursor += 1
        self._clamp()

    def move_home(self) -> None:
        self.cursor = self.line_start

    def move_end(self) -> None:
        self.cursor = self.line_end

    def move_buffer_start(self) -> None:
        self.cursor = 0

    def move_buffer_end(self) -> None:
        self.cursor = len(self.text)

    def word_left(self) -> int:
        """Return the offset one word to the left of the cursor."""
        index = self.cursor
        while index > 0 and not _is_word_char(self.text[index - 1]):
            index -= 1
        while index > 0 and _is_word_char(self.text[index - 1]):
            index -= 1
        return index

    def word_right(self) -> int:
        """Return the offset one word to the right of the cursor."""
        index = self.cursor
        length = len(self.text)
        while index < length and not _is_word_char(self.text[index]):
            index += 1
        while index < length and _is_word_char(self.text[index]):
            index += 1
        return index

    def move_word_left(self) -> None:
        self.cursor = self.word_left()

    def move_word_right(self) -> None:
        self.cursor = self.word_right()

    def move_up(self) -> bool:
        """Move to the previous logical line, keeping the visual column.

        Returns False when already on the first line, so the caller can fall
        back to history navigation the way a shell does.
        """
        start = self.line_start
        if start == 0:
            return False
        column = self.column
        previous_start = self.text.rfind("\n", 0, start - 1) + 1
        self.cursor = self._offset_at_column(previous_start, start - 1, column)
        return True

    def move_down(self) -> bool:
        end = self.line_end
        if end >= len(self.text):
            return False
        column = self.column
        next_start = end + 1
        next_end = self.text.find("\n", next_start)
        next_end = len(self.text) if next_end < 0 else next_end
        self.cursor = self._offset_at_column(next_start, next_end, column)
        return True

    def _offset_at_column(self, start: int, end: int, column: int) -> int:
        """Find the offset in ``[start, end)`` closest to a display column."""
        used = 0
        index = start
        while index < end:
            step = display_width(self.text[index])
            if used + step > column:
                break
            used += step
            index += 1
        return index

    # ------------------------------------------------------------------ kill

    def _remember_kill(self, text: str) -> None:
        """Record a kill, ignoring an empty one.

        Ctrl-K at the end of a line killed nothing and still overwrote the ring,
        so the word you had just killed became unrecoverable.  readline does not
        do that either.
        """
        if text:
            self.killed = text

    def kill_to_end(self) -> None:
        end = self.line_end
        self._remember_kill(self.text[self.cursor : end])
        self.text = self.text[: self.cursor] + self.text[end:]

    def kill_to_start(self) -> None:
        start = self.line_start
        self._remember_kill(self.text[start : self.cursor])
        self.text = self.text[:start] + self.text[self.cursor :]
        self.cursor = start

    def kill_word_left(self) -> None:
        target = self.word_left()
        self._remember_kill(self.text[target : self.cursor])
        self.text = self.text[:target] + self.text[self.cursor :]
        self.cursor = target

    def kill_word_right(self) -> None:
        target = self.word_right()
        self._remember_kill(self.text[self.cursor : target])
        self.text = self.text[: self.cursor] + self.text[target:]

    def yank(self) -> None:
        if self.killed:
            self.insert(self.killed)

    # --------------------------------------------------------------- history

    def remember(self) -> None:
        entry = self.text.strip()
        if entry and (not self.history or self.history[-1] != entry):
            self.history.append(entry)
        self.history_index = None

    def history_previous(self) -> bool:
        if not self.history:
            return False
        if self.history_index is None:
            self.pending = self.text
            self.history_index = len(self.history)
        if self.history_index == 0:
            return True
        self.history_index -= 1
        self.set(self.history[self.history_index])
        return True

    def history_next(self) -> bool:
        if self.history_index is None:
            return False
        self.history_index += 1
        if self.history_index >= len(self.history):
            self.history_index = None
            self.set(self.pending)
            self.pending = ""
            return True
        self.set(self.history[self.history_index])
        return True

    # ------------------------------------------------------------- rendering

    def submit(self) -> str:
        """Take the buffer's contents and reset it."""
        value = self.text
        self.remember()
        self.text = ""
        self.cursor = 0
        return value

    def visual_lines(self, width: int) -> tuple[list[str], int, int]:
        """Wrap the buffer and locate the cursor within the wrapped output.

        Returns ``(lines, row, column)`` so a renderer can draw the cursor
        without re-deriving the wrap.
        """
        width = max(4, width)
        lines: list[str] = []
        cursor_row, cursor_column = 0, 0
        found = False
        offset = 0
        for logical in self.text.split("\n"):
            wrapped = wrap(logical, width) or [""]
            consumed = 0
            for piece in wrapped:
                # Locate the piece in the logical line rather than reconstructing
                # what wrap() dropped.  The old version assumed wrap only ever
                # removed the space *at* the break, but it also strips trailing
                # whitespace — so a line ending in a space had no piece covering
                # the cursor, and cursor_row silently stayed 0: typing a space at
                # the end of a middle line teleported the caret to the top-left.
                start_in_line = logical.find(piece, consumed) if piece else consumed
                if start_in_line < 0:
                    start_in_line = consumed
                start = offset + start_in_line
                end = start + len(piece)
                if not found and start <= self.cursor <= end:
                    cursor_row = len(lines)
                    cursor_column = display_width(self.text[start : self.cursor])
                    found = True
                lines.append(piece)
                consumed = start_in_line + len(piece)
            if not found and offset <= self.cursor <= offset + len(logical):
                # The cursor sits in whitespace that wrapping removed; put it at
                # the end of that logical line's last row rather than nowhere.
                cursor_row = len(lines) - 1
                cursor_column = display_width(lines[-1])
                found = True
            offset += len(logical) + 1
        if not found and lines:
            cursor_row = len(lines) - 1
            cursor_column = display_width(lines[-1])
        return lines or [""], cursor_row, cursor_column

    def render(self, width: int, cursor_style: str = "\033[7m", reset: str = "\033[0m", show_cursor: bool = True) -> list[str]:
        """Render wrapped lines with the cursor drawn as a reverse-video cell."""
        lines, row, column = self.visual_lines(width)
        if not show_cursor:
            return lines
        target = lines[row]
        # Walk the line by display column so wide characters are not split.
        used = 0
        index = 0
        for index, char in enumerate(target):
            if used >= column:
                break
            used += display_width(char)
        else:
            index = len(target)
        if used < column:
            index = len(target)
        head, rest = target[:index], target[index:]
        char = rest[0] if rest else " "
        tail = rest[1:] if rest else ""
        lines[row] = f"{head}{cursor_style}{char}{reset}{tail}"
        return lines
