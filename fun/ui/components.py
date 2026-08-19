"""Reusable visual blocks.

Every component takes a :class:`~fun.ui.theme.Theme` and a column budget and
returns a list of ready-to-print lines.  Components never touch the terminal
themselves, which keeps them trivially testable: a test renders a component at a
given width and asserts on the strings.

The visual language is a left gutter rather than boxes.  Gutters survive
resizing, wrap cleanly, keep copied text readable, and do not force every line
to be padded to the full terminal width.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .text import display_width, fit, truncate, wrap
from .theme import RESET, REVERSE, Theme

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
ASCII_SPINNER_FRAMES = ("|", "/", "-", "\\")

PLAN_MARKERS = {"done": ("✓", "success"), "active": ("●", "accent"), "blocked": ("×", "danger"), "pending": ("○", "faint")}
PLAN_MARKERS_ASCII = {"done": ("x", "success"), "active": ("*", "accent"), "blocked": ("!", "danger"), "pending": ("o", "faint")}

STATUS_TONES = {
    "completed": ("✓", "success"),
    "failed": ("×", "danger"),
    "running": ("•", "accent"),
    "queued": ("·", "faint"),
    "approval": ("⚠", "warning"),
}


def spinner(theme: Theme, tick: int) -> str:
    """Return the spinner glyph for ``tick``, degrading to ASCII when needed."""
    frames = SPINNER_FRAMES if theme.unicode else ASCII_SPINNER_FRAMES
    return frames[tick % len(frames)]


def badge(theme: Theme, text: str, tone: str = "muted") -> str:
    """A small inline tag used for risk levels and task state."""
    return theme.style(f" {text} ", tone, reverse=True)


def banner(theme: Theme, width: int, version: str = "") -> list[str]:
    """The startup wordmark.  Collapses to a single line on narrow terminals."""
    tagline = "Coding should feel good."
    if width < 46 or not theme.unicode:
        title = theme.style("Fun", "accent", bold=True)
        suffix = theme.style(f"  {version}" if version else "", "faint")
        return [f"  {title}{suffix}", f"  {theme.style(tagline, 'muted')}"]
    art = ["┏━╸╻ ╻┏┓╻", "┣╸ ┃ ┃┃┗┫", "╹  ┗━┛╹ ╹"]
    lines = [f"  {theme.style(row, 'accent', bold=True)}" for row in art]
    footer = theme.style(tagline, "muted")
    if version:
        footer += theme.style(f"   {version}", "faint")
    lines.append(f"  {footer}")
    return lines


def _format_arguments(arguments: dict[str, Any], width: int) -> str:
    if not arguments:
        return ""
    parts = []
    for key, value in arguments.items():
        rendered = value if isinstance(value, str) else repr(value)
        parts.append(f"{key}={rendered}")
    return truncate(" ".join(parts), max(8, width))


@dataclass


class ToolView:
    """The subset of a tool call the card needs; keeps components decoupled."""

    name: str
    status: str = "queued"
    arguments: dict[str, Any] | None = None
    elapsed_ms: int | None = None
    exit_code: int | None = None
    output: str = ""
    error: str = ""
    risk: str = ""


def _output_language(view: ToolView) -> str | None:
    """Decide how a tool's output should be highlighted."""
    from .syntax import guess_language

    text = view.output
    if view.name == "edit" or text.lstrip().startswith(("--- ", "diff --git", "@@ ")):
        return "diff"
    if view.name == "read":
        path = str((view.arguments or {}).get("path", ""))
        return guess_language(path)
    return None


def _diff_line(theme: Theme, raw: str, width: int) -> str:
    """Colour one unified-diff line by its kind."""
    if raw.startswith(("+++", "---", "diff --git", "index ")):
        return theme.style(raw, "muted")
    if raw.startswith("@@"):
        return theme.style(raw, "info")
    if raw.startswith("+"):
        return theme.style(raw, "added")
    if raw.startswith("-"):
        return theme.style(raw, "removed")
    return theme.style(raw, "faint")


def hint_bar(theme: Theme, hints: Sequence[tuple[str, str]], width: int = 80) -> str:
    """Key hints as ``key label`` pairs, dropping whole hints rather than cutting one in half."""
    gap = theme.glyph("  ·  ", "  |  ")
    separator = theme.style(gap, "faint")
    parts: list[str] = []
    used = 1
    for key, label in hints:
        plain = f"{key} {label}"
        cost = display_width(plain) + (len(gap) if parts else 0)
        if parts and used + cost > width:
            break
        parts.append(f"{theme.style(key, 'accent')} {theme.style(label, 'faint')}")
        used += cost
    return truncate(" " + separator.join(parts), width)


def background_block(theme: Theme, tasks: Sequence[dict[str, str]], width: int = 80) -> list[str]:
    """One line per background sub-agent task."""
    lines: list[str] = []
    for task in tasks:
        status = task.get("status", "?")
        glyph, tone = STATUS_TONES.get({"completed": "completed", "failed": "failed", "cancelled": "failed"}.get(status, "running"), ("•", "muted"))
        detail = task.get("result") or task.get("error") or task.get("goal", "")
        text = f"{task.get('id', '?')} · {status} · {detail}"
        lines.append(f"  {theme.style(glyph, tone)} {theme.style(truncate(text, max(10, width - 4)), 'muted')}")
    return lines


# ---------------------------------------------------------------- spine bodies
# The layout draws structure (nodes, the vertical spine, indentation); these
# return only the *content* that hangs off it, so the two concerns stay apart.

def tool_body(theme: Theme, view: ToolView, width: int = 80, collapsed: bool = False, output_limit: int = 12) -> list[str]:
    """Output and error lines for a tool call, without any gutter of their own."""
    lines: list[str] = []
    if view.status == "approval":
        return approval_body(theme, view, width)
    if view.output and collapsed:
        return [theme.style(truncate(theme.text("ui_hidden_chars", chars=len(view.output)), width), "faint")]
    if view.output:
        body = view.output.splitlines()
        hidden = max(0, len(body) - output_limit)
        language = _output_language(view)
        if language == "diff":
            lines.extend(truncate(_diff_line(theme, raw, width), width) for raw in body[:output_limit])
        elif language:
            from .markdown import highlight_code

            lines.extend(truncate(raw, width) for raw in highlight_code(theme, "\n".join(body[:output_limit]), language))
        else:
            for raw in body[:output_limit]:
                lines.extend(theme.style(piece, "muted") for piece in wrap(raw, width))
        if hidden:
            lines.append(theme.style(truncate(theme.text("ui_more_lines", hidden=hidden), width), "faint"))
    if view.error:
        lines.extend(theme.style(piece, "danger") for piece in wrap(view.error, width))
    return lines


def approval_body(theme: Theme, view: ToolView, width: int = 80) -> list[str]:
    """The approval gate as a radio list: the choice is the point, not the keys."""
    risk = view.risk or "unknown"
    tone = {"critical": "danger", "high": "danger", "medium": "warning"}.get(risk, "info")
    lines = [theme.style(theme.text("ui_needs_approval"), "warning", bold=True) + "  " + badge(theme, risk, tone)]
    arguments = _format_arguments(view.arguments or {}, width)
    if arguments:
        lines.extend(theme.style(piece, "text") for piece in wrap(arguments, width))
    lines.append("")
    choices = [("y", theme.text("ui_allow_once"), True), ("a", theme.text("ui_allow_session"), False), ("n", theme.text("ui_deny"), False)]
    for key, label, selected in choices:
        glyph = theme.glyph("●" if selected else "○", "*" if selected else "o")
        tone_glyph = "success" if selected else "faint"
        text_tone = "text" if selected else "muted"
        lines.append(f"{theme.style(glyph, tone_glyph)} {theme.style(label, text_tone)}  {theme.style(key, 'faint')}")
    return lines


def plan_body(theme: Theme, steps: Sequence[str], statuses: Sequence[str] = (), width: int = 80) -> list[str]:
    """Plan steps without the heading, for hanging off a spine node."""
    markers = PLAN_MARKERS if theme.unicode else PLAN_MARKERS_ASCII
    lines: list[str] = []
    for index, step in enumerate(steps):
        status = statuses[index] if index < len(statuses) else ("active" if index == 0 else "pending")
        glyph, tone = markers.get(status, markers["pending"])
        pieces = wrap(step, max(8, width - 2))
        tone_text = "text" if status == "active" else ("muted" if status != "pending" else "faint")
        lines.append(f"{theme.style(glyph, tone)} {theme.style(pieces[0] if pieces else '', tone_text)}")
        lines.extend(f"  {theme.style(piece, tone_text)}" for piece in pieces[1:])
    return lines


def recovery_body(theme: Theme, pending: dict[str, str], width: int = 80) -> list[str]:
    """The blocking recovery choice, also as a radio list."""
    lines = [theme.style(theme.text("ui_recovery_needed"), "warning", bold=True)]
    detail = f"{pending.get('name', 'tool')} · {pending.get('call_id', '?')}"
    lines.append(theme.style(truncate(detail, width), "text"))
    arguments = str(pending.get("arguments", ""))
    if arguments:
        lines.extend(theme.style(piece, "muted") for piece in wrap(arguments, width))
    lines.append("")
    for key, label, selected in (("r", theme.text("ui_recovery_resume"), True), ("d", theme.text("ui_recovery_discard"), False), ("f", theme.text("ui_recovery_mark_failed"), False), ("s", theme.text("ui_recovery_stop"), False)):
        glyph = theme.glyph("●" if selected else "○", "*" if selected else "o")
        lines.append(
            f"{theme.style(glyph, 'success' if selected else 'faint')} "
            f"{theme.style(label, 'text' if selected else 'muted')}  {theme.style(key, 'faint')}"
        )
    return lines


def completion_menu(theme: Theme, candidates: Sequence[Any], index: int, width: int = 80, kind: str = "command") -> list[str]:
    """The popup shown above the composer while completing.

    The highlighted row gets the accent edge rather than a full-width inverse
    bar: at 40 columns an inverse bar dominates the screen, while an edge reads
    the same at any width and matches the input panel below it.
    """
    if not candidates:
        return []
    edge = theme.glyph("▌", "|")
    lines: list[str] = []
    name_width = min(max((display_width(item.value) for item in candidates), default=8), max(12, width // 2))
    for position, item in enumerate(candidates):
        selected = position == index
        marker = theme.style(edge, "accent", bold=True) if selected else " "
        name = truncate(item.value, name_width)
        padded = name + " " * max(0, name_width - display_width(name))
        row = f"{marker} {theme.style(padded, 'text' if selected else 'muted', bold=selected)}"
        detail = getattr(item, "detail", "")
        if detail:
            room = width - display_width(row) - 2
            if room > 6:
                row += "  " + theme.style(truncate(detail, room), "faint")
        lines.append(truncate(row, width))
    hint = theme.text("ui_complete_command" if kind == "command" else "ui_complete_file")
    lines.append(" " + theme.style(truncate(hint, max(8, width - 2)), "faint"))
    return lines
