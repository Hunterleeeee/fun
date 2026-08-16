from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TerminalRenderer:
    """Minimal single-column renderer for Runtime events."""

    color: bool = True

    def _style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def activity(self, text: str) -> str:
        return f"◌ {text}"

    def header(self, workspace: str, configured: bool, approval: str) -> str:
        state = "READY" if configured else "SETUP REQUIRED"
        width = 59
        def row(text: str) -> str:
            return f"│ {text[:width]:<{width}}│"
        return "\n".join([
            self._style("╭─ FUN WORKSPACE ─────────────────────────────────────────────╮", "36"),
            row(self._style("Coding should feel good.", "1;36")),
            row(f"workspace  {workspace}"),
            row(f"provider   {state}  approval  {approval}"),
            self._style("╰─────────────────────────────────────────────────────────────╯", "36"),
        ])

    def welcome(self, configured: bool, workspace: str = "") -> str:
        if configured:
            return "Commands: /help  /status  /plan  /usage  /checkpoint  /quit"
        return "\n".join([
            "╭─ WELCOME TO FUN ──────────────────────────────────────────╮",
            "│ Your terminal coding workspace.                            │",
            f"│ {workspace[:57]:<57}│",
            "│                                                             │",
            "│  [1] Configure an OpenAI-compatible provider               │",
            "│  [2] Use environment variables                             │",
            "│  [3] Continue in offline mode                              │",
            "│  [q] Exit                                                  │",
            "╰─────────────────────────────────────────────────────────────╯",
        ])

    def setup_complete(self) -> str:
        return "✓ Setup saved · API key stays out of config · restart `fun` to begin"

    def help(self) -> str:
        return "\n".join([
            "┌─ COMMANDS ────────────────────────────────────────────────┐",
            "│ /help       show this help                                │",
            "│ /status     show task, agent and usage state              │",
            "│ /plan       show the current execution plan               │",
            "│ /usage      show token usage                               │",
            "│ /checkpoint save a workspace checkpoint                   │",
            "│ /pause /resume /stop /recover <action> /quit              │",
            "└───────────────────────────────────────────────────────────┘",
        ])

    def prompt(self, configured: bool = True) -> str:
        return "fun ❯ " if configured else "fun/setup ❯ "

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
