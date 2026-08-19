"""Overlay dialogs: single-field prompts, multi-field forms and pick lists.

Modals own their own key handling so the application loop stays a simple
dispatcher: while a modal is open it receives every key, and it reports back
whether it is finished.  Secret fields never render their value.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import components
from .input import paste_text
from .text import display_width, pad, strip_ansi as strip_style, truncate, wrap
from .theme import RESET, Theme


# input.py emits kill_to_start / kill_to_end; nothing ever emits "kill_line",
# so the branches keyed on it were unreachable and Ctrl-U / Ctrl-K did nothing
# in any dialog.
KILL_KEYS = {"kill_line", "kill_to_start", "kill_to_end", "kill_word_left"}


@dataclass
class PaletteRow:
    """One palette entry, or a group heading when ``command`` is empty."""

    label: str
    command: str = ""
    key: str = ""
    heading: bool = False


@dataclass
class Modal:
    kind: str
    title: str
    callback: Callable[[Any], None] | None = None
    value: str = ""
    fields: list[tuple[str, bool]] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)
    index: int = 0
    loading: bool = False
    max_chars: int = 12000
    done: bool = False
    groups: list[tuple[str, list[tuple[str, str, str]]]] = field(default_factory=list)
    rows: list[PaletteRow] = field(default_factory=list)
    max_rows: int = 14
    # Identifies this dialog to work that was started for it.  A background
    # loader outlives the modal that started it, and its late result used to be
    # applied to whichever select happened to be open by then.
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    # ------------------------------------------------------------------ input

    def handle(self, key: str) -> bool:
        """Process one key.  Returns True when the modal has closed."""
        pasted = paste_text(key)
        if pasted is not None:
            self._paste(pasted)
            return False
        if key in {"escape", "cancel", "eof"}:
            self._finish(None)
            return True
        if self.kind == "palette":
            return self._handle_palette(key)
        if self.kind == "select":
            return self._handle_select(key)
        if self.kind == "prompt":
            return self._handle_prompt(key)
        return self._handle_fields(key)

    # ---------------------------------------------------------------- palette

    def rebuild(self) -> None:
        """Rebuild the visible rows for the current query.

        Headings are only emitted for groups that still have a match, so
        filtering never leaves a bare section title behind.
        """
        from .completion import score

        # Names win over descriptions.  The fuzzy scorer is deliberately
        # forgiving, so "th" also subsequence-matches "clear **t**he transcrip**t**";
        # in a file picker that is generous, in a command list it is noise.  So
        # if the query matches any command *name*, the description matches are
        # dropped entirely rather than ranked below them.
        query = self.value.strip()
        by_name = {
            name
            for _, entries in self.groups
            for name, _summary, _key in entries
            if query and score(query, name.lstrip("/")) is not None
        }

        def matches(name: str, summary: str) -> bool:
            if not query:
                return True
            if by_name:
                return name in by_name
            return score(query, summary) is not None

        rows: list[PaletteRow] = []
        for group, entries in self.groups:
            matched = [PaletteRow(summary or name, name, key) for name, summary, key in entries if matches(name, summary)]
            if matched:
                rows.append(PaletteRow(group, heading=True))
                rows.extend(matched)
        self.rows = rows
        selectable = [index for index, row in enumerate(rows) if not row.heading]
        if not selectable:
            self.index = 0
        elif self.index not in selectable:
            self.index = selectable[0]

    def _move_palette(self, step: int) -> None:
        selectable = [index for index, row in enumerate(self.rows) if not row.heading]
        if not selectable:
            return
        position = selectable.index(self.index) if self.index in selectable else 0
        self.index = selectable[(position + step) % len(selectable)]

    def _handle_palette(self, key: str) -> bool:
        if key == "enter":
            row = self.rows[self.index] if 0 <= self.index < len(self.rows) else None
            self._finish(row.command if row and not row.heading else None)
            return True
        if key in {"up", "down"}:
            self._move_palette(-1 if key == "up" else 1)
            return False
        if key == "backspace":
            self.value = self.value[:-1]
        elif key in KILL_KEYS:
            self.value = ""
        elif len(key) == 1 and key.isprintable():
            self.value += key
        else:
            return False
        self.rebuild()
        return False

    def _handle_select(self, key: str) -> bool:
        if not self.options:
            return False
        if key in {"up", "down"}:
            self.index = (self.index + (1 if key == "down" else -1)) % len(self.options)
        elif key == "enter":
            if self.loading:
                return False
            self._finish(self.options[self.index])
            return True
        return False

    def _handle_prompt(self, key: str) -> bool:
        if key == "enter":
            self._finish(self.value)
            return True
        if key == "newline":
            self.value += "\n"
        elif key == "backspace":
            self.value = self.value[:-1]
        elif key in KILL_KEYS:
            self.value = ""
        elif len(key) == 1 and key.isprintable() and len(self.value) < self.max_chars:
            self.value += key
        return False

    def _handle_fields(self, key: str) -> bool:
        if key == "enter":
            name = self.fields[self.index][0]
            self.values[name] = self.value
            if self.index + 1 < len(self.fields):
                self.index += 1
                self.value = ""
                return False
            self._finish(dict(self.values))
            return True
        if key == "newline":
            self.value += "\n"
        elif key == "backspace":
            self.value = self.value[:-1]
        elif key in KILL_KEYS:
            self.value = ""
        elif len(key) == 1 and key.isprintable():
            self.value += key
        return False

    def _paste(self, text: str) -> None:
        """Insert pasted text as text.

        Every field here is single-line, so newlines are folded to spaces rather
        than submitting the dialog: a key copied with a trailing newline should
        not press Enter for you.
        """
        clean = " ".join(text.split()) if self.kind in {"fields", "palette", "select"} else text
        if self.kind == "select":
            return
        if self.kind == "palette":
            self.value += clean
            self.rebuild()
            return
        self.value = (self.value + clean)[: self.max_chars]

    def _finish(self, result: Any) -> None:
        self.done = True
        if self.callback:
            self.callback(result)

    # -------------------------------------------------------------- rendering

    def _window(self) -> list[tuple[int, PaletteRow]]:
        """The slice of rows to draw, keeping the selection on screen.

        A long registry would otherwise push the composer off the terminal, so
        the list scrolls rather than growing.  The window is expressed in row
        indexes, not positions, so the highlight comparison stays honest.
        """
        rows = list(enumerate(self.rows))
        if len(rows) <= self.max_rows:
            return rows
        start = min(max(0, self.index - self.max_rows // 2), len(rows) - self.max_rows)
        return rows[start:start + self.max_rows]

    def palette_lines(self, theme: Theme, width: int) -> list[str]:
        """A filled, grouped, searchable command surface.

        Rendered as a solid panel rather than a bordered box: at this size a
        border is noise, and the full-width selection bar needs a surface to sit
        on rather than a rule to bump into.
        """
        # No floor above the terminal: max(36, ...) won below width 44 and every
        # palette row rendered wider than the screen, wrapping inside an
        # alternate screen that has no scrollback to recover from.
        inner = min(max(24, width - 4), 68)
        content = max(8, inner - 4)
        surface = theme.color("canvas", background=True) if theme.enabled else ""
        reset = RESET if theme.enabled else ""

        def row(styled: str, plain_width: int, highlight: bool = False) -> str:
            filler = " " * max(0, content - plain_width)
            body = f"  {styled}{filler}  "
            if highlight and theme.enabled:
                return theme.color("accent", background=True) + theme.color("canvas") + strip_style(body) + reset
            return f"{surface}{body}{reset}" if surface else body

        blank = row("", 0)
        lines = [blank]
        esc = theme.style("esc", "faint")
        head = theme.style(self.title, "text", bold=True)
        lines.append(row(head + " " * max(0, content - display_width(self.title) - 3) + esc, content))
        lines.append(blank)
        # The block caret sits *on* the first character while typing and on a
        # blank cell before that, so the placeholder never hides the cursor.
        typed = self.value
        caret_cell = typed[:1] or " "
        trailing = typed[1:] if typed else theme.text("ui_palette_search")
        trailing = truncate(trailing, max(1, content - display_width(caret_cell)))
        query = theme.style(caret_cell, "text", reverse=True) + theme.style(trailing, "text" if typed else "faint")
        lines.append(row(query, display_width(caret_cell) + display_width(trailing)))
        lines.append(blank)
        for position, item in self._window():
            if item.heading:
                lines.append(blank)
                lines.append(row(theme.style(item.label, "info", bold=True), display_width(item.label)))
                continue
            label = truncate(item.label, max(4, content - display_width(item.key) - 1))
            gap = " " * max(1, content - display_width(label) - display_width(item.key))
            plain = label + gap + item.key
            if position == self.index:
                lines.append(row(plain, display_width(plain), highlight=True))
            else:
                lines.append(row(theme.style(label, "text") + gap + theme.style(item.key, "faint"), display_width(plain)))
        if not self.rows:
            empty = theme.text("ui_palette_empty")
            lines.append(row(theme.style(empty, "faint"), display_width(empty)))
        commands = [index for index, item in enumerate(self.rows) if not item.heading]
        if len(self.rows) > self.max_rows and commands:
            position = commands.index(self.index) + 1 if self.index in commands else 1
            note = theme.text("ui_palette_hint", position=position, total=len(commands))
            lines.append(blank)
            lines.append(row(theme.style(note, "faint"), display_width(note)))
        lines.append(blank)
        return lines

    def lines(self, theme: Theme, width: int) -> list[str]:
        """Render the dialog.  Rows are filled, not outlined only, so the modal
        reads as a surface sitting above the canvas rather than as stray rules."""
        if self.kind == "palette":
            return self.palette_lines(theme, width)
        inner = max(24, min(width - 4, 72))
        corner = ("╭", "╮", "╰", "╯", "─", "│") if theme.unicode else ("+", "+", "+", "+", "-", "|")
        top_left, top_right, bottom_left, bottom_right, bar, side = corner
        title = truncate(self.title, inner - 4)
        head = theme.style(f"{top_left}{bar} {title} " + bar * max(0, inner - display_width(title) - 5) + top_right, "accent")
        edge = theme.style(side, "accent")
        content = max(4, inner - 4)

        def row(line: str) -> str:
            return f"{edge} {pad(truncate(line, content), content)} {edge}"

        body = [head]
        body.extend(row(line) for line in self._body(theme, content))
        body.append(row(theme.style(self._footer(theme), "faint")))
        body.append(theme.style(bottom_left + bar * max(0, inner - 2) + bottom_right, "accent"))
        return body

    def _body(self, theme: Theme, width: int) -> list[str]:
        if self.kind == "select":
            caret = theme.glyph("❯", ">")
            if self.loading and not self.options:
                return [theme.style(theme.text("ui_loading"), "muted")]
            rows = []
            for index, option in enumerate(self.options):
                selected = index == self.index
                marker = theme.style(caret, "accent") if selected else " "
                rows.append(f"{marker} {theme.style(truncate(option, width - 2), 'text' if selected else 'muted')}")
            return rows
        if self.kind == "prompt":
            shown = wrap(self.value, width) if self.value else [theme.style("(empty)", "faint")]
            return shown[-10:]
        name, secret = self.fields[self.index]
        shown = "•" * len(self.value) if secret else self.value
        label = theme.style(f"{name}: ", "muted")
        return [label + truncate(shown, max(4, width - len(name) - 2))]

    def _footer(self, theme: Theme) -> str:
        if self.kind == "select":
            return theme.text("ui_select_footer")
        if self.kind == "prompt":
            return theme.text("ui_prompt_footer", used=len(self.value), total=self.max_chars)
        return theme.text("ui_fields_footer", position=f"{self.index + 1}/{len(self.fields)}")


def palette_modal(title: str, groups: list[tuple[str, list[tuple[str, str, str]]]], callback: Callable[[str | None], None]) -> Modal:
    modal = Modal("palette", title, callback, groups=groups)
    modal.rebuild()
    return modal


def prompt_modal(title: str, initial: str, callback: Callable[[str | None], None]) -> Modal:
    return Modal("prompt", title, callback, value=initial)


def field_modal(title: str, fields: Sequence[str | tuple[str, bool]], callback: Callable[[dict[str, str] | None], None]) -> Modal:
    normalized = [(item[0], bool(item[1])) if isinstance(item, tuple) else (item, False) for item in fields]
    return Modal("fields", title, callback, fields=normalized)


def select_modal(title: str, options: Sequence[str], callback: Callable[[str | None], None]) -> Modal:
    return Modal("select", title, callback, options=[str(item) for item in options])
