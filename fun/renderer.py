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

    def event(self, event_type: str, payload: dict[str, object] | None = None) -> str:
        payload = payload or {}
        if event_type in {"plan.created", "task.started"}:
            return self.activity(event_type.replace(".", " "))
        if event_type in {"tool.requested", "model.tool_call"}:
            return self.activity(str(payload.get("name", event_type)))
        if event_type in {"tool.completed", "validation.completed", "checkpoint.restored"}:
            return self.success(str(payload.get("text", event_type)))
        if event_type in {"tool.failed", "validation.failed", "checkpoint.restore_failed"}:
            return self.error(str(payload.get("text", event_type)))
        return event_type
