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


# The one argument that identifies a call, per tool.  Printing all of them put
# `expected_hash=ab12 patch=<the entire diff>` in an edit's header — noise in
# the place a reader scans first.
HEADLINE_ARGUMENT = {"read": "path", "edit": "path", "explore": "path", "exec": "command"}


def _format_arguments(arguments: dict[str, Any], width: int, name: str = "") -> str:
    """The header line's argument: the identifying one, without its key."""
    if not arguments:
        return ""
    headline = HEADLINE_ARGUMENT.get(name)
    if headline and headline in arguments:
        return truncate(str(arguments[headline]), max(8, width))
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

# A successful read or listing is not worth reprinting: the agent asked for it,
# the agent got it, and pouring the file into the transcript pushes the actual
# answer off the screen.  A failure is the opposite — it is the whole reason to
# be looking.
QUIET_WHEN_OK = frozenset({"read", "explore"})

#: Machine tags the tools return, and the sentence each one means.  The tag is
#: what the *model* is told, because it needs a stable token; showing the same
#: token to the person watching means the screen says "APPROVAL_REQUIRED" right
#: after they pressed n, instead of confirming that nothing ran.
REFUSAL_MESSAGES = {
    "APPROVAL_DENIED": "refuse_approval_denied",
    "APPROVAL_REQUIRED": "refuse_approval_required",
    "CRITICAL_OPERATION_BLOCKED": "refuse_critical_blocked",
    "MODE_FORBIDS_TOOL": "refuse_mode_forbids",
    "FILE_CHANGED_SINCE_READ": "refuse_file_changed",
    "PATCH_FAILED": "refuse_patch_failed",
    "INVALID_TIMEOUT": "refuse_invalid_timeout",
    "INVALID_ARGUMENTS": "refuse_invalid_arguments",
    "INVALID_TOOL_ARGUMENTS": "refuse_invalid_arguments",
    "INVALID_COMMAND_PLAN": "refuse_invalid_arguments",
    "UNSUPPORTED_TOOL": "refuse_unsupported_tool",
    "REPAIR_BUDGET_EXCEEDED": "refuse_repair_budget",
    "COMMAND_TIMEOUT": "refuse_command_timeout",
    "EXEC_FAILED": "refuse_exec_failed",
    "TOOL_EXECUTION_FAILED": "refuse_tool_execution_failed",
}


def explain_refusal(theme: Theme, text: str) -> str | None:
    """The sentence a refusal tag stands for, or None if this is ordinary output."""
    head = (text or "").strip().split("\n", 1)[0].split(":", 1)[0].strip()
    key = REFUSAL_MESSAGES.get(head)
    return theme.text(key) if key else None


def tool_body(theme: Theme, view: ToolView, width: int = 80, collapsed: bool = False, output_limit: int = 12, expanded: bool = False) -> list[str]:
    """Output and error lines for a tool call, without any gutter of their own."""
    lines: list[str] = []
    if view.status == "approval":
        return approval_body(theme, view, width)
    explained = explain_refusal(theme, view.output)
    if explained:
        # The tag itself stays in the event log and in what the model was told.
        return [theme.style(piece, "warning") for piece in wrap(explained, width)]
    body = view.output.splitlines() if view.output else []
    quiet = view.name in QUIET_WHEN_OK and view.status == "completed" and not expanded
    if view.output and (collapsed or quiet):
        return [theme.style(truncate(theme.text("ui_output_hidden", lines=len(body), chars=len(view.output)), width), "faint")]
    if view.output:
        # Failures are truncated from the *front*.  The assertion, the traceback
        # and the summary line are all at the end, so showing the first twelve
        # lines of a failing test run hides precisely the part worth reading.
        failed = view.status == "failed" or view.exit_code not in (None, 0)
        hidden = max(0, len(body) - output_limit)
        shown = body[-output_limit:] if failed and hidden else body[:output_limit]
        language = _output_language(view)
        if hidden and failed:
            lines.append(theme.style(truncate(theme.text("ui_earlier_lines", hidden=hidden), width), "faint"))
        if language == "diff":
            lines.extend(truncate(_diff_line(theme, raw, width), width) for raw in shown)
        elif language:
            from .markdown import highlight_code

            lines.extend(truncate(raw, width) for raw in highlight_code(theme, "\n".join(shown), language))
        else:
            for raw in shown:
                lines.extend(theme.style(piece, "muted") for piece in wrap(raw, width))
        if hidden and not failed:
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
    """The blocking recovery choice, as a radio list that says what each one does.

    A person meeting this screen has just restarted after a crash.  Naming the
    tool and its internal call id told them which row of the event log this was
    and nothing else: not what had been asked, not what the risk of resuming is.
    Resuming re-runs the command — which for anything that already half-ran is
    the one consequence they most need spelled out.
    """
    lines = [theme.style(theme.text("ui_recovery_needed"), "warning", bold=True)]
    lines.extend(theme.style(piece, "text") for piece in wrap(theme.text("ui_recovery_explain"), width))
    goal = str(pending.get("goal", "")).strip()
    if goal:
        lines.extend(theme.style(piece, "muted") for piece in wrap(theme.text("ui_recovery_goal", goal=goal), width))
    lines.append("")
    name = pending.get("name", "tool")
    arguments = str(pending.get("arguments", "")).strip()
    # The call id identifies the row in the event log, so it is kept — but as a
    # trailing detail on the same line, not as a line of its own competing with
    # the command the person actually has to judge.
    call_id = str(pending.get("call_id", "")).strip()
    head = theme.style(theme.text("ui_recovery_call") + ": ", "faint") + theme.style(truncate(name, max(8, width - 16)), "text", bold=True)
    lines.append(head + (theme.style(f"  {truncate(call_id, 16)}", "faint") if call_id else ""))
    if arguments:
        lines.extend(theme.style(piece, "text") for piece in wrap(arguments, width))
    lines.append("")
    options = (
        ("r", theme.text("ui_recovery_resume"), theme.text("ui_recovery_resume_why"), True),
        ("d", theme.text("ui_recovery_discard"), theme.text("ui_recovery_discard_why"), False),
        ("f", theme.text("ui_recovery_mark_failed"), theme.text("ui_recovery_mark_failed_why"), False),
        ("s", theme.text("ui_recovery_stop"), theme.text("ui_recovery_stop_why"), False),
    )
    for key, label, why, selected in options:
        glyph = theme.glyph("●" if selected else "○", "*" if selected else "o")
        lines.append(
            f"{theme.style(glyph, 'success' if selected else 'faint')} "
            f"{theme.style(label, 'text' if selected else 'muted')}  {theme.style(key, 'faint')}"
        )
        for piece in wrap(why, max(8, width - 2)):
            lines.append("  " + theme.style(piece, "faint"))
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
