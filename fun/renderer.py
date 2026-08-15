from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TerminalRenderer:
    """Minimal single-column renderer for Runtime events."""

    color: bool = True

    def activity(self, text: str) -> str:
        return f"◌ {text}"

    def plan(self, steps: list[str], statuses: list[str] | None = None) -> str:
        lines = ["◇ PLAN"]
        statuses = statuses or []
        markers = {"done": "✓", "active": "●", "blocked": "×", "pending": "○"}
        for index, step in enumerate(steps):
            status = statuses[index] if index < len(statuses) else ("active" if index == 0 else "pending")
            lines.append(f"  {markers.get(status, '○')} {step}")
        return "\n".join(lines)

    def finding(self, text: str) -> str:
        return f"! {text}"

    def success(self, text: str) -> str:
        return f"✓ {text}"

    def error(self, text: str) -> str:
        return f"× {text}"

    def event(self, event_type: str, payload: dict[str, object] | None = None) -> str:
        payload = payload or {}
        if event_type in {"plan.created", "task.started", "agent.node"}:
            return self.activity(str(payload.get("node", event_type.replace(".", " "))))
        if event_type in {"tool.requested", "model.tool_call", "tool.executing"}:
            return self.activity(str(payload.get("name", event_type)))
        if event_type == "approval.pending":
            return self.finding(f"approval required: {payload.get('name', 'tool')}")
        if event_type == "approval.rejected":
            return self.error(f"approval rejected: {payload.get('name', 'tool')}")
        if event_type == "recovery.required":
            return self.error(f"recovery required: {payload.get('reason', 'unknown')}")
        if event_type in {"tool.completed", "validation.completed", "checkpoint.restored"}:
            return self.success(str(payload.get("text", event_type)))
        if event_type in {"tool.failed", "validation.failed", "checkpoint.restore_failed"}:
            return self.error(str(payload.get("text", event_type)))
        return event_type
