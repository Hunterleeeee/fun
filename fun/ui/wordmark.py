"""The block wordmark used on the start screen.

A bitmap font is spelled out rather than generated so the letterforms can be
tuned by eye.  Only the characters Fun actually renders are defined; asking for
anything else falls back to a plain letter so a caller can never crash the UI
by passing an unexpected string.
"""
from __future__ import annotations

HEIGHT = 5

GLYPHS: dict[str, list[str]] = {
    "F": [
        "█████████",
        "██       ",
        "███████  ",
        "██       ",
        "██       ",
    ],
    "U": [
        "██     ██",
        "██     ██",
        "██     ██",
        "██     ██",
        " ███████ ",
    ],
    "N": [
        "███    ██",
        "████   ██",
        "██ ██  ██",
        "██  ██ ██",
        "██   ████",
    ],
}

_BLANK = ["         "] * HEIGHT


def render(word: str, gap: str = "  ") -> list[str]:
    """Return the wordmark for ``word`` as ``HEIGHT`` rows of block characters."""
    letters = [GLYPHS.get(char.upper(), _BLANK) for char in word]
    if not letters:
        return [""] * HEIGHT
    return [gap.join(letter[row] for letter in letters) for row in range(HEIGHT)]


def width(word: str, gap: str = "  ") -> int:
    rows = render(word, gap)
    return len(rows[0]) if rows else 0
