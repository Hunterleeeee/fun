"""Fun's layout: an event spine, and a centred start screen.

Two ideas carry the design.

**The spine.**  Fun's whole thesis is that the Runtime — not the model — owns
what actually happened, and that every step is a durable, replayable event.  So
the session view is drawn as a single vertical spine with a status node per
event, the way a build pipeline or a commit graph reads.  Scanning the left
column alone tells you what ran, in what order, and what its verdict was; the
detail hangs off to the right and can be collapsed without breaking the line.

**The dock.**  Input is a filled panel with an accent edge, carrying the mode,
model and usage *inside* it, so the thing you are about to act with states its
own context instead of scattering it across a status bar.

Composition happens through :class:`Canvas`, which addresses the screen in
display columns.  Slicing rows by character index breaks the moment a row holds
a wide character or an escape sequence, so placements are collected and
resolved together against a plain background.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import wordmark
from .text import display_width, pad, truncate
from .theme import Theme

# Spine glyphs.  ASCII fallbacks keep the structure legible without Unicode.
SPINE = {
    "line": ("│", "|"),
    "node": ("●", "*"),
    "branch": ("├", "+"),
    "start": ("╭", "."),
    "end": ("╵", "'"),
}

NODE_TONES = {
    "completed": ("✓", "success"),
    "failed": ("×", "danger"),
    "running": ("◐", "accent"),
    "queued": ("○", "faint"),
    "approval": ("⚠", "warning"),
    "user": ("●", "user"),
    "assistant": ("●", "accent"),
    "system": ("·", "muted"),
    "plan": ("◇", "accent"),
}

ASCII_NODES = {"✓": "v", "×": "x", "◐": "*", "○": "o", "⚠": "!", "●": "@", "◇": "#", "·": "."}


@dataclass
class Canvas:
    """Column-addressed compositor for centred, absolutely-placed layouts."""

    width: int
    height: int
    fill: str = " "
    _rows: list[str] = field(default_factory=list, init=False)
    _placements: dict[int, list[tuple[int, str, int]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._rows = [self.fill * self.width for _ in range(self.height)]

    def put(self, row: int, column: int, styled: str, plain_width: int) -> None:
        if 0 <= row < self.height:
            self._placements.setdefault(row, []).append((max(0, column), styled, plain_width))

    def center(self, row: int, styled: str, plain_width: int) -> None:
        self.put(row, (self.width - plain_width) // 2, styled, plain_width)

    def render(self) -> list[str]:
        out: list[str] = []
        for index, background in enumerate(self._rows):
            items = sorted(self._placements.get(index, []))
            if not items:
                out.append("")
                continue
            line, cursor = "", 0
            for column, styled, plain_width in items:
                if column < cursor:
                    continue  # overlapping placement: first one wins
                line += background[cursor:column] + styled
                cursor = column + plain_width
            out.append(line.rstrip())
        return out


class Spine:
    """Builds spine-prefixed lines for one session view."""

    def __init__(self, theme: Theme, width: int, indent: str = "  ") -> None:
        self.theme = theme
        self.width = width
        self.indent = indent
        self.lines: list[str] = []

    def _glyph(self, name: str) -> str:
        unicode_glyph, ascii_glyph = SPINE[name]
        return self.theme.glyph(unicode_glyph, ascii_glyph)

    @property
    def bar(self) -> str:
        return self.theme.style(self._glyph("line"), "faint")

    @property
    def body_width(self) -> int:
        # indent + spine glyph + two spaces of gutter
        return max(12, self.width - display_width(self.indent) - 3)

    def node(self, kind: str, title: str, meta: str = "", tone_override: str | None = None) -> None:
        """Emit a node row: a status glyph on the spine plus a title and meta."""
        glyph, tone = NODE_TONES.get(kind, ("•", "muted"))
        if not self.theme.unicode:
            glyph = ASCII_NODES.get(glyph, glyph)
        tone = tone_override or tone
        head = f"{self.indent}{self.theme.style(glyph, tone)} {title}"
        if meta:
            used = display_width(head) + display_width(meta) + 1
            if used <= self.width:
                head += " " * (self.width - used + 1) + self.theme.style(meta, "faint")
            else:
                head += f" {self.theme.style(meta, 'faint')}"
        self.lines.append(truncate(head, self.width))

    def body(self, rows: list[str]) -> None:
        """Hang detail lines off the spine."""
        for row in rows:
            self.lines.append(truncate(f"{self.indent}{self.bar}  {row}", self.width))

    def gap(self) -> None:
        self.lines.append(f"{self.indent}{self.bar}")

    def close(self) -> None:
        self.lines.append(f"{self.indent}{self.theme.style(self._glyph('end'), 'faint')}")


def dock_panel(
    theme: Theme,
    editor_lines: list[str],
    width: int,
    mode: str = "Build",
    model: str = "",
    approval: str = "smart",
    usage: str = "",
    state: str = "ready",
    spinner: str = "",
) -> list[str]:
    """The input panel: an accent edge, the draft, and its own context line."""
    # The panel shares the spine's left margin so the whole screen has one
    # left edge instead of two competing ones.
    margin = "  "
    edge = margin + theme.style(theme.glyph("▌", "|"), "accent", bold=True)
    lines = [edge]
    for row in editor_lines or [""]:
        lines.append(truncate(f"{edge}  {row}", width))
    # A spinner alone says "something is happening" but not what; the state word
    # stays next to it so the panel always names its own condition.
    label = state if state not in {"ready", "idle", ""} else ""
    context_plain = " · ".join(part for part in (mode, label, model or "no model", approval, usage) if part)
    context = theme.style(mode, "accent")
    remainder = context_plain[len(mode):]
    context += theme.style(remainder, "danger" if state == "failed" else "muted")
    if spinner:
        context = theme.style(spinner, "accent") + " " + context
    lines.append(truncate(f"{edge}  {context}", width))
    lines.append(edge)
    return lines


def hero_block(
    theme: Theme,
    width: int,
    height: int,
    version: str = "",
) -> list[str]:
    """The empty-session body: ambient field, gradient wordmark, tagline.

    This is *only* the body.  The input panel, hints, completion popup and caret
    all come from the one dock that every screen shares — a second copy of them
    here is exactly how `@`, `/` and the editor itself ended up missing from the
    start screen while appearing to be implemented.
    """
    width = max(40, width)
    height = max(6, height)
    canvas = Canvas(width, height)
    glyphs = AMBIENT_GLYPHS if theme.unicode else AMBIENT_ASCII
    for (row, column), glyph in _ambient(width - 2, height, glyphs).items():
        canvas.put(row, column + 1, theme.style(glyph, "faint"), 1)

    art = wordmark.render("FUN")
    art_width = display_width(art[0])
    block = wordmark.HEIGHT + 3
    top = max(0, (height - block) // 2)
    if height >= block and width >= art_width + 4:
        for index, row in enumerate(art):
            canvas.center(top + index, theme.gradient(row), art_width)
        tagline = "Runtime-first coding agent"
        canvas.center(top + wordmark.HEIGHT + 1, theme.style(tagline, "muted"), display_width(tagline))
        thesis = theme.text("ui_thesis")
        canvas.center(top + wordmark.HEIGHT + 2, theme.style(thesis, "faint"), display_width(thesis))
    else:
        title = f"FUN  {version}".strip()
        canvas.center(max(0, height // 2), theme.style(title, "accent", bold=True), display_width(title))
    return canvas.render()


def intro(
    theme: Theme,
    width: int,
    version: str = "",
    workspace: str = "",
    model: str = "",
    mode: str = "Build",
    approval: str = "smart",
    session: str = "",
) -> list[str]:
    """A compact opening banner for the streaming frontend.

    The full :func:`hero` centres itself in the viewport, which only makes sense
    when a frontend owns the whole screen.  In scrollback that would just be a
    page of blank lines above the prompt, so streaming gets this instead: the
    same wordmark and context, printed once, taking only the rows it needs.
    """
    width = max(32, width)
    lines: list[str] = [""]
    art = wordmark.render("FUN")
    if width >= display_width(art[0]) + 4 and theme.unicode:
        lines.extend(f"  {theme.style(row, 'accent', bold=True)}" for row in art)
    else:
        lines.append(f"  {theme.style('FUN', 'accent', bold=True)}")
    lines.append("")
    tagline = "Runtime-first coding agent"
    if version:
        tagline += f"   {version}"
    lines.append(f"  {theme.style(tagline, 'muted')}")
    context = " · ".join(part for part in (workspace, model or "no model", mode, approval) if part)
    if context:
        lines.append(f"  {theme.style(truncate(context, width - 2), 'faint')}")
    if session:
        lines.append(f"  {theme.style(truncate(session, width - 2), 'faint')}")
    lines.append("")
    lines.append(f"  {theme.style(theme.text('ui_composer_placeholder'), 'muted')}")
    return [truncate(line, width) for line in lines]


# The ambient field is drawn from Fun's own event vocabulary rather than
# decorative sparkles: the glyphs that mark plans, nodes and verdicts, scattered
# at the faintest tone. It reads as texture up close and as the product's own
# language on inspection, so the canvas carries weight without borrowing anyone
# else's signature.
AMBIENT_GLYPHS = ("◇", "●", "○", "✓", "│", "·", "╵", "◐")
AMBIENT_ASCII = ("#", "*", "o", "v", "|", ".", "'", "%")

FRAME = {
    "top_left": ("╭", "."), "top_right": ("╮", "."),
    "bottom_left": ("╰", "'"), "bottom_right": ("╯", "'"),
    "horizontal": ("─", "-"), "vertical": ("│", "|"),
}


def _ambient(width: int, height: int, glyphs: tuple[str, ...], seed: int = 0x5EED) -> dict[tuple[int, int], str]:
    """A deterministic sparse scatter, so the screen does not shimmer on repaint.

    A PRNG would give a different field every frame and make the incremental
    writer repaint everything; this hash is stable for a given size.
    """
    field: dict[tuple[int, int], str] = {}
    for row in range(height):
        for column in range(width):
            value = (row * 73856093) ^ (column * 19349663) ^ seed
            value = (value * 2654435761) & 0xFFFFFFFF
            if value % 331 == 0:
                field[(row, column)] = glyphs[(value >> 8) % len(glyphs)]
    return field


def mode_tabs(theme: Theme, active: str, modes: tuple[str, ...] = ("Build", "Plan", "Review")) -> tuple[str, int]:
    """A tab strip naming every mode, so Tab is discoverable rather than folklore."""
    styled, plain = "", ""
    for index, name in enumerate(modes):
        if index:
            styled += theme.style("   ", "faint")
            plain += "   "
        if name == active:
            styled += theme.style(f" {name} ", "accent", reverse=True, bold=True)
        else:
            styled += theme.style(f" {name} ", "faint")
        plain += f" {name} "
    return styled, display_width(plain)


def frame_canvas(
    theme: Theme,
    rows: list[str],
    width: int,
    height: int,
    session: str = "",
    workspace: str = "",
    mode: str = "",
    approval: str = "",
    version: str = "",
) -> list[str]:
    """Wrap a finished frame in the canvas border.

    Framing happens here, once, around whatever the layout produced — when the
    border belonged to the start screen it simply vanished the moment a session
    had content.
    """
    horizontal = theme.glyph(*FRAME["horizontal"])
    vertical = theme.style(theme.glyph(*FRAME["vertical"]), "accent_soft")
    inner = max(8, width - 2)

    def rail(left: str, right: str, lead: str, tail: str) -> str:
        """One border rule.  Corners and the leading rule cost three columns.

        Both ends are clipped.  Only ``tail`` used to be, so a long workspace
        path produced a bottom border wider than the terminal — the right corner
        and the whole footer silently disappeared off the edge.
        """
        budget = max(4, width - 3)
        tail_text = f" {truncate(tail, max(4, width // 2))} " if tail else ""
        lead_room = max(0, budget - display_width(tail_text) - 2)
        lead_text = f" {truncate(lead, lead_room)} " if lead and lead_room >= 2 else ""
        filler = max(0, width - 3 - display_width(lead_text) - display_width(tail_text))
        return (
            theme.style(left + horizontal, "accent_soft")
            + theme.style(lead_text, "accent")
            + theme.style(horizontal * filler, "faint")
            + theme.style(tail_text, "faint")
            + theme.style(right, "accent_soft")
        )

    top = rail(theme.glyph(*FRAME["top_left"]), theme.glyph(*FRAME["top_right"]), "fun", session)
    footer = " · ".join(part for part in (mode, approval, version) if part)
    bottom = rail(theme.glyph(*FRAME["bottom_left"]), theme.glyph(*FRAME["bottom_right"]), workspace, footer)
    body = rows[: max(0, height - 2)]
    body += [""] * max(0, height - 2 - len(body))
    framed = [top]
    for line in body:
        framed.append(vertical + " " + pad(truncate(line, inner - 2), inner - 2) + " " + vertical)
    framed.append(bottom)
    return framed
