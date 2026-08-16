from __future__ import annotations

from dataclasses import dataclass
import unicodedata


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _fit_display(text: str, width: int) -> str:
    result = ""
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width:
            break
        result += char
        used += char_width
    return result + " " * max(0, width - used)


@dataclass
class TerminalRenderer:
    """Minimal single-column renderer for Runtime events."""

    color: bool = True
    locale: str = "en-US"

    @property
    def zh(self) -> bool:
        return self.locale.startswith("zh")

    def _style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def activity(self, text: str) -> str:
        return f"◌ {text}"

    def header(self, workspace: str, configured: bool, approval: str) -> str:
        state = ("就绪" if configured else "需要配置") if self.zh else ("READY" if configured else "SETUP REQUIRED")
        title = "FUN 工作区" if self.zh else "FUN WORKSPACE"
        slogan = "让写代码变得有意思。" if self.zh else "Coding should feel good."
        width = 59
        def row(text: str) -> str:
            return f"│ {_fit_display(text, width)}│"
        top_text = f"╭─ {title} " + "─" * max(0, 57 - _display_width(title)) + "╮"
        bottom_text = "╰" + "─" * 60 + "╯"
        return "\n".join([
            self._style(top_text, "36"),
            row(self._style(slogan, "1;36")),
            row(("工作区  " if self.zh else "workspace  ") + workspace),
            row(("Provider  " if self.zh else "provider   ") + f"{state}  " + ("审批  " if self.zh else "approval  ") + approval),
            self._style(bottom_text, "36"),
        ])

    def welcome(self, configured: bool, workspace: str = "") -> str:
        if configured:
            return "命令：/help  /config  /model  /permissions  /status  /plan  /usage  /logout  /exit" if self.zh else "Commands: /help  /config  /model  /permissions  /status  /plan  /usage  /logout  /exit"
        if self.zh:
            return "\n".join(["╭─ 欢迎使用 Fun ────────────────────────────────────────────╮", "│ 你的终端 Coding 工作区。                                  │", f"│ {_fit_display(workspace, 58)}│", "│                                                            │", "│  [1] OpenAI                                                │", "│  [2] OpenAI-compatible / 自定义 Provider                  │", "│  [3] 使用环境变量                                          │", "│  [4] 先进入离线模式                                        │", "│  [q] 退出                                                  │", "╰─────────────────────────────────────────────────────────────╯"])
        return "\n".join(["╭─ WELCOME TO FUN ──────────────────────────────────────────╮", "│ Your terminal coding workspace.                            │", f"│ {workspace[:57]:<57}│", "│                                                             │", "│  [1] OpenAI                                                │", "│  [2] OpenAI-compatible / custom provider                  │", "│  [3] Use environment variables                             │", "│  [4] Continue in offline mode                              │", "│  [q] Exit                                                  │", "╰─────────────────────────────────────────────────────────────╯"])

    def setup_complete(self) -> str:
        return "✓ 配置已保存 · API Key 已安全保存 · 当前会话已生效" if self.zh else "✓ Setup saved · API key is stored securely · active in this session"

    def help(self) -> str:
        if self.zh:
            return "\n".join(["┌─ 命令 ────────────────────────────────────────────────────┐", "│ /help       显示帮助                                      │", "│ /status     查看任务、Agent 和用量状态                    │", "│ /plan       查看当前执行计划                             │", "│ /usage      查看 Token 用量                              │", "│ /checkpoint 创建工作区检查点                             │", "│ /pause /resume /stop /recover <action> /quit              │", "└───────────────────────────────────────────────────────────┘"])
        return "\n".join(["┌─ COMMANDS ────────────────────────────────────────────────┐", "│ /help       show this help                                │", "│ /status     show task, agent and usage state              │", "│ /plan       show the current execution plan               │", "│ /usage      show token usage                               │", "│ /checkpoint save a workspace checkpoint                   │", "│ /pause /resume /stop /recover <action> /quit              │", "└───────────────────────────────────────────────────────────┘"])

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
