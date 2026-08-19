"""Backwards-compatible aliases for the UI state model.

The state model moved to :mod:`fun.ui.state` when the presentation layer was
split into text, theme, component, state and surface modules.  These aliases
keep older imports working.
"""
from __future__ import annotations

from .ui.state import ToolCard, TranscriptItem, UiState
from .ui.state import UiState as TerminalUiState

__all__ = ["ToolCard", "TranscriptItem", "UiState", "TerminalUiState"]
