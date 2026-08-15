from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TerminalRenderer:
    """Minimal single-column renderer for Runtime events."""

    color: bool = True

    def activity(self, text: str) -> str:
        return f"◌ {text}"

    def plan(self, steps: list[str]) -> str:
        lines = ["◇ PLAN"]
        for index, step in enumerate(steps):
            marker = "●" if index == 0 else "○"
            lines.append(f"  {marker} {step}")
        return "\n".join(lines)

    def finding(self, text: str) -> str:
        return f"! {text}"

    def success(self, text: str) -> str:
        return f"✓ {text}"

    def error(self, text: str) -> str:
        return f"× {text}"
