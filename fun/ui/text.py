"""Width-aware string primitives for terminal layout.

Every helper here measures text in *terminal columns*, not in Python
characters.  Two things break the naive ``len(text)`` assumption:

* East-asian wide characters occupy two columns, so a 20-character Chinese
  string overflows a 20-column box.
* ANSI escape sequences occupy zero columns, so colouring a string silently
  inflates ``len`` and makes width guarantees meaningless.

Combining marks and zero-width joiners are measured as zero columns so that
accented text and emoji sequences do not over-report their footprint.
"""
from __future__ import annotations

import re
import unicodedata

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]")
# Defined here rather than imported from .theme: this module is the bottom of
# the UI stack and must not depend on anything above it.
RESET = "\033[0m"
_ANSI_SPLIT = re.compile(r"(\x1b\[[0-9;:?]*[ -/]*[@-~])")
_ZERO_WIDTH = {"​", "‌", "‍", "﻿"}


def strip_ansi(text: str) -> str:
    """Return ``text`` without any ANSI escape sequences."""
    return ANSI_PATTERN.sub("", text)


def char_width(char: str) -> int:
    """Return the column count of a single character."""
    if char in _ZERO_WIDTH:
        return 0
    if unicodedata.combining(char):
        return 0
    category = unicodedata.category(char)
    if category in {"Mn", "Me", "Cf"}:
        return 0
    if category == "Cc":
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    """Return how many terminal columns ``text`` occupies, ignoring ANSI codes."""
    return sum(char_width(char) for char in strip_ansi(text))


# Everything below this line is content the model produced, or content a file
# the model read told it to produce.  A terminal treats some of those bytes as
# commands, not text.
_CONTROL_ALLOWED = {"\n", "\t"}


def sanitize(text: str) -> str:
    """Strip escape sequences and control characters from untrusted content.

    Model output reaches the terminal verbatim, so a reply containing
    ``\x1b]0;...\x07`` rewrites the window title, ``\x1b[2J`` clears the
    screen, and on an OSC-52 terminal the clipboard can be written — all
    reachable by prompt injection through a file the ``read`` tool ingested.
    The UI adds its own styling afterwards, so nothing legitimate is lost.
    Carriage returns are dropped rather than kept, since a lone ``\r`` erases
    the line that was just drawn.
    """
    if not text:
        return text
    stripped = ANSI_PATTERN.sub("", text)
    stripped = _OTHER_ESCAPES.sub("", stripped)
    return "".join(char for char in stripped if char in _CONTROL_ALLOWED or unicodedata.category(char) != "Cc")


# OSC (\x1b] ... BEL or ST) and the single-character escapes CSI does not cover.
_OTHER_ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]|\x1b.")


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Shorten ``text`` to at most ``width`` columns, closing any open style.

    The cut discards everything after it — including the reset that would have
    closed a style opened before it.  For a foreground colour the bleed is
    subtle; for ``reverse`` it is a block of inverse video running to the right
    edge of the screen, which is what the mode tab strip did at narrow widths.
    So if anything was opened and not closed, this appends the reset itself.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    marker_width = display_width(ellipsis)
    budget = max(0, width - marker_width)
    result: list[str] = []
    used = 0
    for token in _ANSI_SPLIT.split(text):
        if not token:
            continue
        if ANSI_PATTERN.fullmatch(token):
            result.append(token)
            continue
        for char in token:
            size = char_width(char)
            if used + size > budget:
                return _close_styles("".join(result) + ellipsis)
            result.append(char)
            used += size
    return _close_styles("".join(result) + ellipsis)


def _close_styles(text: str) -> str:
    """Append a reset when ``text`` ends with an SGR sequence still open."""
    opened = False
    for token in ANSI_PATTERN.findall(text):
        if not token.endswith("m"):
            continue
        parameters = token[2:-1]
        opened = not (parameters in {"", "0"} or parameters.split(";")[0] == "0")
    return text + RESET if opened else text


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad ``text`` with spaces to exactly ``width`` columns."""
    missing = max(0, width - display_width(text))
    if align == "right":
        return " " * missing + text
    if align == "center":
        left = missing // 2
        return " " * left + text + " " * (missing - left)
    return text + " " * missing


def fit(text: str, width: int, align: str = "left", ellipsis: str = "…") -> str:
    """Force ``text`` to occupy exactly ``width`` columns."""
    return pad(truncate(text, width, ellipsis), width, align)


def _wrap_segment(segment: str, width: int) -> list[str]:
    """Wrap one already-ANSI-free paragraph, breaking CJK runs anywhere."""
    lines: list[str] = []
    current = ""
    current_width = 0
    word = ""
    word_width = 0

    def flush_word() -> None:
        nonlocal current, current_width, word, word_width
        if not word:
            return
        if current_width and current_width + word_width > width:
            lines.append(current)
            current, current_width = "", 0
        while word_width > width:
            take = ""
            take_width = 0
            for char in word:
                size = char_width(char)
                if take_width + size > width:
                    break
                take += char
                take_width += size
            lines.append((current + take) if current_width == 0 else current)
            if current_width == 0:
                word = word[len(take):]
                word_width -= take_width
            else:
                current, current_width = "", 0
        current += word
        current_width += word_width
        word, word_width = "", 0

    for char in segment:
        size = char_width(char)
        if char == " ":
            flush_word()
            if current_width + size <= width:
                current += char
                current_width += size
            else:
                lines.append(current)
                current, current_width = "", 0
            continue
        # CJK has no spaces, so treat every wide character as its own word.
        if size == 2:
            flush_word()
            if current_width + size > width:
                lines.append(current)
                current, current_width = "", 0
            current += char
            current_width += size
            continue
        word += char
        word_width += size
    flush_word()
    if current or not lines:
        lines.append(current)
    return [line.rstrip() if line.strip() else line for line in lines]


def wrap(text: str, width: int) -> list[str]:
    """Wrap ``text`` to ``width`` columns, honouring existing newlines.

    ANSI sequences are stripped before measuring and re-applied per line by the
    caller; callers that need colour should wrap first and colour after.
    """
    if width <= 0:
        return [""]
    lines: list[str] = []
    for paragraph in strip_ansi(text).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        lines.extend(_wrap_segment(paragraph, width))
    return lines or [""]
