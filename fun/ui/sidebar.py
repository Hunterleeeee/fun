"""The right rail: what the Runtime knows, standing still.

The transcript answers "what was said"; it is a stream, and everything in it
scrolls away.  The rail answers "where do things stand" — goal, plan, the tool
ledger, background agents, context — and it does not move.  That split is the
point: in a Runtime-first agent the durable state *is* the product, so it earns
a column of its own rather than being reconstructed by scrolling back or by
typing ``/status``.

Cards are laid out by priority, not by a fixed grid.  Whatever does not fit is
dropped whole, from the bottom, and the rail says how many rows it dropped — a
card cut in half mid-list reads as a bug, while a named omission reads as a
decision.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .layout import NODE_TONES, ASCII_NODES
from .text import display_width, pad, truncate, wrap
from .theme import Theme

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import UiState

MIN_TOTAL_WIDTH = 92
MIN_RAIL = 24
MAX_RAIL = 34

PLAN_GLYPHS = {"done": ("✓", "success"), "active": ("◐", "accent"), "blocked": ("×", "danger"), "pending": ("○", "faint")}
TASK_TONES = {
    "idle": "faint", "working": "accent", "ready": "success", "completed": "success",
    "failed": "danger", "stopped": "warning", "paused": "warning", "recovery": "warning",
}


def rail_width(width: int) -> int:
    """How wide the rail should be at a given terminal width."""
    return max(MIN_RAIL, min(MAX_RAIL, width // 4))


def fits(width: int) -> bool:
    """Whether a rail can be shown without starving the transcript.

    Below this the main column would drop under ~60 columns, where wrapped code
    and diffs stop being readable — the rail would be costing more than it says.
    """
    return width >= MIN_TOTAL_WIDTH


def _glyph(theme: Theme, symbol: str) -> str:
    return symbol if theme.unicode else ASCII_NODES.get(symbol, "*")


def _heading(theme: Theme, text: str, note: str, width: int) -> str:
    gap = " " * max(1, width - display_width(text) - display_width(note))
    return theme.style(text, "info", bold=True) + gap + theme.style(note, "faint")


def _entry(theme: Theme, symbol: str, tone: str, text: str, note: str, width: int) -> list[str]:
    """One rail row: glyph, label, and an optional right-aligned note."""
    room = max(4, width - 2 - (display_width(note) + 1 if note else 0))
    label = truncate(text, room)
    gap = " " * max(1, width - 2 - display_width(label) - display_width(note)) if note else ""
    body = theme.style(label, "text" if tone == "accent" else "muted") + gap + theme.style(note, "faint")
    return [f"{theme.style(_glyph(theme, symbol), tone)} {body}"]


def _task_card(theme: Theme, state: "UiState", width: int) -> list[str]:
    tone = TASK_TONES.get(state.task_state, "muted")
    word = theme.text(f"ui_state_{state.task_state}") if state.task_state in TASK_TONES else (state.task_state or theme.text("ui_state_idle"))
    note = ""
    if state.plan:
        note = f"{sum(1 for item in state.plan_status if item == 'done')}/{len(state.plan)}"
    lines = [_heading(theme, theme.text("ui_rail_task"), note, width)]
    lines.extend(_entry(theme, "●", tone, word, state.agent_mode, width))
    if state.goal:
        lines.extend(f"  {theme.style(piece, 'faint')}" for piece in wrap(state.goal, max(6, width - 2))[:2])
    if state.tools:
        # A tally, not a list: the spine already *is* the event log, and copying
        # it into the rail would put the same rows on screen twice.
        failed = sum(1 for card in state.tools.values() if card.status == "failed")
        tally = theme.text("ui_tool_count", count=len(state.tools)) + (theme.text("ui_tool_failed_count", failed=failed) if failed else "")
        lines.append(f"  {theme.style(truncate(tally, max(4, width - 2)), 'faint')}")
    return lines


def _plan_card(theme: Theme, state: "UiState", width: int) -> list[str]:
    if not state.plan:
        return []
    lines = [_heading(theme, theme.text("ui_rail_plan"), "", width)]
    for index, step in enumerate(state.plan):
        status = state.plan_status[index] if index < len(state.plan_status) else "pending"
        symbol, tone = PLAN_GLYPHS.get(status, PLAN_GLYPHS["pending"])
        lines.extend(_entry(theme, symbol, tone, step, "", width))
    return lines


def _alert_card(theme: Theme, state: "UiState", width: int) -> list[str]:
    """A pending recovery decision outranks everything else on the rail."""
    if not state.recovery:
        return []
    lines = [_heading(theme, theme.text("ui_rail_recovery"), "", width)]
    detail = f"{state.recovery.get('name', 'tool')} · {state.recovery.get('call_id', '?')}"
    lines.extend(_entry(theme, "⚠", "warning", detail, "", width))
    return lines


def _background_card(theme: Theme, state: "UiState", width: int) -> list[str]:
    if not state.background:
        return []
    lines = [_heading(theme, theme.text("ui_rail_background"), str(len(state.background)), width)]
    for task in state.background[:4]:
        status = task.get("status", "")
        symbol, tone = {"completed": ("✓", "success"), "failed": ("×", "danger"), "cancelled": ("×", "danger")}.get(status, ("◐", "accent"))
        lines.extend(_entry(theme, symbol, tone, task.get("id", "?"), status, width))
    return lines


def _context_card(theme: Theme, state: "UiState", width: int) -> list[str]:
    rows = [item for item in (state.workspace, state.model_name or "no model", state.approval_mode, state.usage_text) if item]
    if not rows:
        return []
    lines = [_heading(theme, theme.text("ui_rail_context"), "", width)]
    lines.extend(f"  {theme.style(truncate(row, max(4, width - 2)), 'faint')}" for row in rows)
    return lines


CARDS = (_alert_card, _task_card, _plan_card, _background_card, _context_card)


def rail(theme: Theme, state: "UiState", width: int, height: int) -> list[str]:
    """Build the rail, dropping whole cards from the bottom when short of rows."""
    blocks = [block for build in CARDS if (block := build(theme, state, width))]
    lines: list[str] = []
    dropped = 0
    for block in blocks:
        # +1 for the blank separator this block would add.
        if lines and len(lines) + len(block) + 1 > height - 1:
            dropped += len(block)
            continue
        if lines:
            lines.append("")
        lines.extend(block)
    if dropped:
        lines.append("")
        lines.append(theme.style(truncate(theme.text("ui_rail_more", dropped=dropped), width), "faint"))
    return [truncate(line, width) for line in lines[:height]]


def split(theme: Theme, body: list[str], rail_lines: list[str], main: int, width: int) -> list[str]:
    """Join the main column and the rail with a single dividing rule."""
    divider = theme.style(theme.glyph("│", "|"), "faint")
    rows = max(len(body), len(rail_lines))
    joined: list[str] = []
    for index in range(rows):
        left = pad(truncate(body[index] if index < len(body) else "", main), main)
        right = rail_lines[index] if index < len(rail_lines) else ""
        joined.append(f"{left} {divider} {right}")
    return joined
