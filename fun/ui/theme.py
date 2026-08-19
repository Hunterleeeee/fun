"""Colour capability detection and Fun's semantic palette.

Colour is addressed by *meaning* ("this is a risk badge") rather than by name
("this is yellow"), so a single palette swap restyles the whole UI and every
surface degrades coherently on terminals that cannot do truecolor.

Detection order follows the de-facto conventions: ``NO_COLOR`` disables colour
outright, ``FORCE_COLOR`` overrides detection, then ``COLORTERM``/``TERM`` decide
between 16.7M colours, 256 colours and the basic 16.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

TRUECOLOR = "truecolor"
ANSI256 = "256"
ANSI16 = "16"
NONE = "none"

_LEVELS = {NONE: 0, ANSI16: 1, ANSI256: 2, TRUECOLOR: 3}


def detect_color_support(env: dict[str, str] | None = None, is_tty: bool = True) -> str:
    """Return the richest colour mode the current terminal can render."""
    environ = os.environ if env is None else env
    if environ.get("NO_COLOR") is not None:
        return NONE
    forced = environ.get("FORCE_COLOR")
    if forced is not None:
        if forced in {"0", "false", "no"}:
            return NONE
        return {"1": ANSI16, "2": ANSI256, "3": TRUECOLOR}.get(forced, TRUECOLOR)
    if not is_tty:
        return NONE
    term = environ.get("TERM", "")
    if not term or term == "dumb":
        return NONE
    if environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return TRUECOLOR
    if "256" in term or "direct" in term:
        return ANSI256
    return ANSI16


def _to_256(red: int, green: int, blue: int) -> int:
    """Map an RGB triple onto the xterm-256 cube (or its greyscale ramp)."""
    if abs(red - green) < 12 and abs(green - blue) < 12:
        grey = round((red + green + blue) / 3)
        if grey < 8:
            return 16
        if grey > 248:
            return 231
        return 232 + round((grey - 8) / 247 * 23)
    level = lambda value: 0 if value < 48 else 1 if value < 115 else (value - 35) // 40
    return 16 + 36 * level(red) + 6 * level(green) + level(blue)


def _to_16(red: int, green: int, blue: int) -> int:
    """Map an RGB triple onto the basic 16-colour set."""
    bright = max(red, green, blue) > 160
    code = (1 if red > 100 else 0) | (2 if green > 100 else 0) | (4 if blue > 100 else 0)
    table = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7}
    return table[code] + (60 if bright else 0)


@dataclass(frozen=True)
class Color:
    red: int
    green: int
    blue: int

    def sequence(self, mode: str, background: bool = False) -> str:
        if mode == NONE:
            return ""
        base = 48 if background else 38
        if mode == TRUECOLOR:
            return f"\033[{base};2;{self.red};{self.green};{self.blue}m"
        if mode == ANSI256:
            return f"\033[{base};5;{_to_256(self.red, self.green, self.blue)}m"
        offset = _to_16(self.red, self.green, self.blue)
        return f"\033[{(40 if background else 30) + offset}m"


# Every theme defines the same semantic slots, so a component never asks for a
# colour by name — it asks for a meaning, and swapping the theme restyles the
# whole UI.  `info` is deliberately not another blue in the sky theme, or diff
# hunk headers would read as accent.
THEMES: dict[str, dict[str, "Color"]] = {
    # Sky: the default. A bright sky-blue accent on cool slate neutrals.
    "sky": {
        "canvas": Color(11, 15, 22),
        "accent": Color(56, 189, 248),
        "accent_soft": Color(2, 132, 199),
        "text": Color(226, 232, 240),
        "muted": Color(148, 163, 184),
        "faint": Color(87, 101, 120),
        "success": Color(52, 211, 153),
        "warning": Color(251, 191, 36),
        "danger": Color(248, 113, 113),
        "info": Color(167, 139, 250),
        "user": Color(226, 232, 240),
        "added": Color(52, 211, 153),
        "removed": Color(248, 113, 113),
    },
    # Dawn: tuned for light terminal backgrounds, where pale text vanishes.
    "dawn": {
        "canvas": Color(248, 250, 252),
        "accent": Color(2, 122, 191),
        "accent_soft": Color(3, 105, 161),
        "text": Color(30, 41, 59),
        "muted": Color(71, 85, 105),
        "faint": Color(148, 163, 184),
        "success": Color(4, 120, 87),
        "warning": Color(180, 83, 9),
        "danger": Color(190, 40, 40),
        "info": Color(109, 40, 217),
        "user": Color(30, 41, 59),
        "added": Color(4, 120, 87),
        "removed": Color(190, 40, 40),
    },
    # Ember: a warm accent for people who dislike blue-forward terminals.
    "ember": {
        "canvas": Color(20, 16, 13),
        "accent": Color(245, 158, 66),
        "accent_soft": Color(180, 108, 40),
        "text": Color(237, 233, 226),
        "muted": Color(168, 158, 145),
        "faint": Color(105, 97, 88),
        "success": Color(134, 194, 106),
        "warning": Color(233, 186, 80),
        "danger": Color(233, 108, 92),
        "info": Color(140, 176, 214),
        "user": Color(237, 233, 226),
        "added": Color(134, 194, 106),
        "removed": Color(233, 108, 92),
    },
    # Mono: structure carried by weight and glyph alone, for screenshots,
    # colour-blind users, and terminals with hostile palettes.
    "mono": {
        "canvas": Color(12, 12, 12),
        "accent": Color(245, 245, 245),
        "accent_soft": Color(190, 190, 190),
        "text": Color(225, 225, 225),
        "muted": Color(160, 160, 160),
        "faint": Color(105, 105, 105),
        "success": Color(225, 225, 225),
        "warning": Color(225, 225, 225),
        "danger": Color(245, 245, 245),
        "info": Color(180, 180, 180),
        "user": Color(225, 225, 225),
        "added": Color(225, 225, 225),
        "removed": Color(150, 150, 150),
    },
}

DEFAULT_THEME = "sky"
PALETTE = THEMES[DEFAULT_THEME]


def theme_names() -> list[str]:
    return sorted(THEMES)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDERLINE = "\033[4m"
REVERSE = "\033[7m"


@dataclass
class Theme:
    """Resolves semantic style names into escape sequences for one terminal."""

    mode: str = TRUECOLOR
    unicode: bool = True
    name: str = DEFAULT_THEME
    # Locale lives here because Theme is already the "how this terminal renders"
    # object threaded into every component, and language is part of that.  The
    # alternative was a second parameter on forty call sites, which is how the
    # chrome ended up hardcoded in one language in the first place.
    locale: str = "en-US"

    def text(self, key: str, **fields: object) -> str:
        """A localised UI string, formatted with ``fields``."""
        from ..i18n import t

        value = t(self.locale, key)
        return value.format(**fields) if fields else value

    @classmethod
    def detect(cls, env: dict[str, str] | None = None, is_tty: bool = True, name: str = DEFAULT_THEME) -> "Theme":
        environ = os.environ if env is None else env
        encoding = (environ.get("LC_ALL") or environ.get("LC_CTYPE") or environ.get("LANG") or "").lower()
        unicode_ok = "utf" in encoding or not encoding
        return cls(detect_color_support(environ, is_tty), unicode_ok, name if name in THEMES else DEFAULT_THEME)

    @property
    def palette(self) -> dict[str, "Color"]:
        return THEMES.get(self.name, THEMES[DEFAULT_THEME])

    @property
    def enabled(self) -> bool:
        return self.mode != NONE

    def at_least(self, mode: str) -> bool:
        return _LEVELS[self.mode] >= _LEVELS[mode]

    def blend(self, start: str, end: str, position: float) -> str:
        """A colour interpolated between two palette slots, for gradients."""
        first, second = self.palette.get(start), self.palette.get(end)
        if not first or not second or not self.enabled:
            return self.color(start)
        ratio = max(0.0, min(1.0, position))
        mixed = Color(
            round(first.red + (second.red - first.red) * ratio),
            round(first.green + (second.green - first.green) * ratio),
            round(first.blue + (second.blue - first.blue) * ratio),
        )
        return mixed.sequence(self.mode)

    def gradient(self, text: str, start: str = "accent", end: str = "accent_soft") -> str:
        """Paint ``text`` with a per-character gradient across two slots.

        Only worth doing in truecolor; at 256 or 16 colours the steps collapse
        into banding, so those terminals get the flat accent instead.
        """
        if not self.enabled or self.mode != TRUECOLOR or not text.strip():
            return self.style(text, start, bold=True)
        span = max(1, len(text) - 1)
        out = BOLD
        for index, char in enumerate(text):
            out += self.blend(start, end, index / span) + char
        return out + RESET

    def canvas(self) -> str:
        """The background fill for surfaces that own the whole screen."""
        return self.color("canvas", background=True) if self.enabled else ""

    def color(self, name: str, background: bool = False) -> str:
        color = self.palette.get(name)
        return color.sequence(self.mode, background) if color else ""

    def style(self, text: str, color: str = "", *, bold: bool = False, dim: bool = False, italic: bool = False, underline: bool = False, reverse: bool = False) -> str:
        """Wrap ``text`` in the requested styles, or return it untouched when colour is off."""
        if not self.enabled:
            return text
        prefix = ""
        if bold:
            prefix += BOLD
        if dim:
            prefix += DIM
        if italic:
            prefix += ITALIC
        if underline:
            prefix += UNDERLINE
        if reverse:
            prefix += REVERSE
        if color:
            prefix += self.color(color)
        return f"{prefix}{text}{RESET}" if prefix else text

    def glyph(self, unicode_glyph: str, ascii_glyph: str) -> str:
        """Return the unicode glyph when the locale can render it, else ASCII."""
        return unicode_glyph if self.unicode else ascii_glyph
