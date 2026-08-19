"""Terminal Markdown rendering.

Model replies are Markdown, and printing them raw wastes most of their
structure: headings, lists and fenced code all arrive as undifferentiated text.
This module turns them into styled terminal lines.

The hard part is wrapping.  Styling first and wrapping after would force the
wrapper to measure escape sequences; wrapping first and styling after loses
which span each fragment came from.  So inline markup is parsed into
``(text, tone)`` segments and :func:`wrap_segments` wraps *across* segments
while tracking display width, letting styles survive a line break intact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .syntax import CALL, COMMENT, KEYWORD, NAME, NUMBER, OPERATOR, STRING, tokenize
from .text import char_width, display_width, truncate
from .theme import Theme

SYNTAX_TONES = {
    KEYWORD: "accent",
    STRING: "success",
    NUMBER: "info",
    COMMENT: "faint",
    NAME: "info",
    CALL: "user",
    OPERATOR: "muted",
}

_FENCE = re.compile(r"^\s*(?:```+|~~~+)\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_INLINE = re.compile(
    r"(?P<code>`+[^`]+`+)"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>\*[^*\s][^*]*\*|_[^_\s][^_]*_)"
    r"|(?P<link>\[[^\]]+\]\([^)]+\))"
)


@dataclass
class Segment:
    text: str
    tone: str = "text"
    bold: bool = False
    italic: bool = False
    code: bool = False


def parse_inline(text: str) -> list[Segment]:
    """Split one line into styled segments, leaving unmatched text plain."""
    segments: list[Segment] = []
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            segments.append(Segment(text[position : match.start()]))
        if match.group("code"):
            body = match.group("code").strip("`")
            segments.append(Segment(body, "success", code=True))
        elif match.group("bold"):
            segments.append(Segment(match.group("bold")[2:-2], "text", bold=True))
        elif match.group("italic"):
            segments.append(Segment(match.group("italic")[1:-1], "text", italic=True))
        else:
            label, _, target = match.group("link")[1:].partition("](")
            segments.append(Segment(label, "accent"))
            segments.append(Segment(f" ({target.rstrip(')')})", "faint"))
        position = match.end()
    if position < len(text):
        segments.append(Segment(text[position:]))
    return segments or [Segment("")]


def wrap_segments(segments: list[Segment], width: int) -> list[list[Segment]]:
    """Wrap styled segments to ``width`` columns, splitting segments as needed."""
    width = max(4, width)
    rows: list[list[Segment]] = []
    current: list[Segment] = []
    used = 0

    def flush() -> None:
        nonlocal current, used
        rows.append(current)
        current, used = [], 0

    for segment in segments:
        # Keep the separators so whitespace inside a styled run is preserved.
        for word in re.split(r"(\s+)", segment.text):
            if not word:
                continue
            word_width = display_width(word)
            if word.isspace():
                if used and used + word_width <= width:
                    current.append(Segment(word, segment.tone, segment.bold, segment.italic, segment.code))
                    used += word_width
                elif used:
                    flush()
                continue
            if used + word_width > width:
                if used:
                    flush()
                if word_width > width:
                    # One pass over the word, cutting at each width boundary.
                    # The previous loop re-measured and re-sliced the *whole*
                    # remaining word on every row, so an unbroken 100 KB token —
                    # a base64 blob, a minified JSON line — took 15 seconds to
                    # wrap, on the UI thread, on every repaint.
                    start = 0
                    taken = 0
                    for position, char in enumerate(word):
                        step = char_width(char)
                        if taken + step > width:
                            current.append(Segment(word[start:position], segment.tone, segment.bold, segment.italic, segment.code))
                            flush()
                            start = position
                            taken = 0
                        taken += step
                    word = word[start:]
                    word_width = taken
                if not word:
                    continue
            current.append(Segment(word, segment.tone, segment.bold, segment.italic, segment.code))
            used += word_width
    if current or not rows:
        flush()
    return rows


def paint(theme: Theme, row: list[Segment]) -> str:
    """Apply theme styling to one wrapped row.

    Adjacent segments sharing a style are merged before styling, so a sentence
    emits one escape sequence rather than one per word — this matters over SSH,
    where the dock repaints on every keystroke.
    """
    merged: list[Segment] = []
    for segment in row:
        key = (segment.tone, segment.bold, segment.italic, segment.code)
        if merged and (merged[-1].tone, merged[-1].bold, merged[-1].italic, merged[-1].code) == key:
            merged[-1].text += segment.text
        else:
            merged.append(Segment(segment.text, *key[:1], *key[1:]))
    return "".join(
        theme.style(item.text, item.tone, bold=item.bold, italic=item.italic) for item in merged
    )


def highlight_code(theme: Theme, source: str, language: str | None = None) -> list[str]:
    """Return syntax-highlighted lines for a block of source."""
    lines: list[str] = []
    for line in source.split("\n"):
        rendered = ""
        for kind, text in tokenize(line, language):
            tone = SYNTAX_TONES.get(kind)
            rendered += theme.style(text, tone) if tone else text
        lines.append(rendered)
    return lines


def _code_block(theme: Theme, body: list[str], language: str | None, indent: str, budget: int) -> list[str]:
    """Render a fenced block.  Code is clipped, never wrapped.

    Re-flowing source across lines changes what it means, so a line too wide for
    the terminal is truncated with a marker instead.
    """
    bar = theme.style(theme.glyph("│", "|"), "faint")
    room = max(8, budget - 4)
    lines = []
    for line in highlight_code(theme, "\n".join(body), language):
        lines.append(f"{indent}  {bar} {truncate(line, room)}")
    return lines


def render(theme: Theme, text: str, width: int = 80, indent: str = "") -> list[str]:
    """Render Markdown into styled, width-bounded terminal lines."""
    budget = max(8, width - display_width(indent))
    out: list[str] = []
    fence_language: str | None = None
    fence_body: list[str] = []
    in_fence = False

    def emit_segments(segments: list[Segment], prefix: str = "", hanging: str = "") -> None:
        rows = wrap_segments(segments, budget - display_width(prefix))
        for index, row in enumerate(rows):
            lead = prefix if index == 0 else (hanging or " " * display_width(prefix))
            out.append(indent + lead + paint(theme, row))

    for raw in text.split("\n"):
        fence = _FENCE.match(raw)
        if fence:
            if in_fence:
                out.extend(_code_block(theme, fence_body, fence_language, indent, budget))
                in_fence, fence_body, fence_language = False, [], None
            else:
                in_fence = True
                fence_language = fence.group(1) or None
            continue
        if in_fence:
            fence_body.append(raw)
            continue
        if not raw.strip():
            out.append("")
            continue
        if _RULE.match(raw):
            out.append(indent + theme.style(theme.glyph("─", "-") * budget, "faint"))
            continue
        heading = _HEADING.match(raw)
        if heading:
            level = len(heading.group(1))
            tone = "accent" if level <= 2 else "text"
            emit_segments([Segment(heading.group(2), tone, bold=True)])
            continue
        quote = _QUOTE.match(raw)
        if quote:
            bar = theme.style(theme.glyph("▏", "|"), "accent_soft")
            emit_segments([Segment(piece.text, "muted", piece.bold, piece.italic, piece.code) for piece in parse_inline(quote.group(1))], prefix=f"{bar} ")
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            pad = " " * len(bullet.group(1))
            dot = theme.style(theme.glyph("•", "*"), "accent")
            emit_segments(parse_inline(bullet.group(2)), prefix=f"{pad}{dot} ", hanging=f"{pad}  ")
            continue
        ordered = _ORDERED.match(raw)
        if ordered:
            pad = " " * len(ordered.group(1))
            marker = theme.style(f"{ordered.group(2)}.", "accent")
            emit_segments(parse_inline(ordered.group(3)), prefix=f"{pad}{marker} ", hanging=f"{pad}   ")
            continue
        emit_segments(parse_inline(raw))

    if in_fence and fence_body:
        # An unterminated fence still renders; streaming output often ends
        # mid-block.  Clipped to the same budget as the closed-fence path — this
        # branch skipped it, so a wide source line escaped the column while the
        # block was still streaming, which is exactly when this branch runs.
        bar = theme.style(theme.glyph("│", "|"), "faint")
        room = max(4, width - display_width(indent) - 4)
        for line in highlight_code(theme, "\n".join(fence_body), fence_language):
            out.append(f"{indent}  {bar} {truncate(line, room)}")
    return out or [""]
