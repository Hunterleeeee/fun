"""Fun's terminal presentation layer.

The package is deliberately dependency-free.  It is split so that the pieces
that decide *what* a frame contains stay separate from the pieces that decide
*how* bytes reach the terminal:

``text``        width-aware string primitives (east-asian width, ANSI-safe)
``theme``       colour capability detection and the semantic palette
``components``  reusable blocks: plan, tool card, approval, status bar, diff
``state``       the UI state model a frontend renders
``screen``      incremental frame writer that only repaints changed lines
``stream``      default frontend: scrollback-preserving, redraws only the dock
``fullscreen``  alternate-screen frontend for a fixed, dense layout
"""
from __future__ import annotations

from .text import display_width, fit, pad, truncate, strip_ansi, wrap
from .theme import Theme, detect_color_support

__all__ = ["display_width", "fit", "pad", "truncate", "strip_ansi", "wrap", "Theme", "detect_color_support"]
