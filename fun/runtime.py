from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .events import Event, EventStore
from .policy import ApprovalMode, Policy, Risk
from .persistence import SQLiteEventStore
from .provider import OpenAICompatible, tool_schemas
from .tools import ToolResult, Tools

SYSTEM_PROMPT = """You are Fun, a safety-first terminal coding agent.
The Runtime is authoritative for tool results, workspace boundaries, approvals, and task state.
Inspect before editing. Make small reversible changes. Never claim an action succeeded without its tool result.
For substantial tasks, maintain a concise 2-7 step plan and verify edits with a focused command.
Do not reveal hidden chain-of-thought; communicate short, auditable activity updates.
"""


@dataclass
class Task:
    id: str
    goal: str
    status: str = "created"
    plan: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)


class Runtime:
    def __init__(self, workspace: str, approval: str = "smart", provider: OpenAICompatible | None = None, event_store: EventStore | None = None, state_dir: str | None = None, approve: Callable[[str, Risk], bool] | None = None) -> None:
        self.session_id = f"ses_{uuid.uuid4().hex[:12]}"
        if event_store is not None:
            self.events = event_store
        elif state_dir is not None:
            self.events = EventStore(SQLiteEventStore(Path(state_dir) / "events.db"))
        else:
            self.events = EventStore()
        self.workspace = Path(workspace).expanduser().resolve()
        self.policy = Policy(ApprovalMode(approval))
        self.tools = Tools(workspace, self.policy)
        self.approve = approve
        self.provider = provider
        self.task: Task | None = None

    def emit(self, event_type: str, task_id: str | None = None, **payload: object) -> Event:
        return self.events.append(Event(event_type, self.session_id, task_id, dict(payload)))

    def create_task(self, goal: str) -> Task:
        if self.task and self.task.status == "running":
            raise RuntimeError("TASK_ALREADY_RUNNING")
        self.task = Task(f"task_{uuid.uuid4().hex[:12]}", goal, "running")
        self.task.plan = self._initial_plan(goal)
        self.task.messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": goal}]
        self.emit("task.created", self.task.id, goal=goal)
        self.emit("plan.created", self.task.id, steps=self.task.plan)
        self.emit("task.started", self.task.id)
        return self.task

    @staticmethod
    def _initial_plan(goal: str) -> list[str]:
        lower = goal.lower()
        if any(word in lower for word in ("fix", "修复", "改", "implement", "实现")):
            return ["inspect workspace", "locate relevant code", "apply a minimal change", "run focused validation"]
        return ["inspect workspace", "analyze the request", "report verified findings"]

    def run_tool(self, name: str, **kwargs: object) -> ToolResult:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        self.emit("tool.requested", self.task.id, name=name, arguments=kwargs)
        write_operation = name in {"edit", "exec"}
        risk = self.policy.risk_for(name, write=write_operation)
        if self.policy.requires_approval(risk):
            self.emit("approval.required", self.task.id, name=name, risk=risk.value)
            allowed = self.approve(name, risk) if self.approve else False
            if not allowed:
                result = ToolResult(False, "APPROVAL_REQUIRED", risk)
                self.emit("tool.failed", self.task.id, name=name, ok=False, text=result.text, changed=[])
                return result
        method: Callable[..., ToolResult] = getattr(self.tools, name)
        try:
            result = method(**kwargs)
        except Exception as exc:
            self.emit("tool.failed", self.task.id, name=name, error=str(exc))
            raise
        self.emit("tool.completed" if result.ok else "tool.failed", self.task.id, name=name, ok=result.ok, text=result.text, changed=result.changed or [])
        return result

    def run_model_turn(self, on_text: Callable[[str], None] | None = None, max_steps: int = 8) -> str:
        if not self.provider or not self.task:
            raise RuntimeError("PROVIDER_NOT_CONFIGURED")
        final_text = ""
        for _ in range(max_steps):
            chunks = self.provider.stream(self.task.messages, tool_schemas())
            content = ""
            calls: dict[str, dict[str, str]] = {}
            for chunk in chunks:
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content += delta["content"]
                    if on_text:
                        on_text(delta["content"])
                for call in delta.get("tool_calls") or []:
                    entry = calls.setdefault(str(call.get("index", 0)), {"name": "", "arguments": "", "id": ""})
                    entry["id"] += call.get("id", "")
                    function = call.get("function") or {}
                    entry["name"] += function.get("name", "")
                    entry["arguments"] += function.get("arguments", "")
            if content:
                self.task.messages.append({"role": "assistant", "content": content})
                final_text += content
            if not calls:
                self.emit("model.completed", self.task.id, text=content)
                return final_text
            assistant_calls = [{"id": item["id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}} for item in calls.values()]
            self.task.messages.append({"role": "assistant", "content": content or None, "tool_calls": assistant_calls})
            for call in assistant_calls:
                name = call["function"]["name"]
                try:
                    arguments = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    result = ToolResult(False, "INVALID_ARGUMENTS")
                else:
                    self.emit("model.tool_call", self.task.id, call_id=call["id"], name=name)
                    result = self.run_tool(name, **arguments)
                self.task.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result.text})
        self.emit("task.blocked", self.task.id, reason="TOOL_BUDGET_EXCEEDED")
        raise RuntimeError("TASK_BUDGET_EXCEEDED")

    def validate(self, command: str) -> ToolResult:
        """Run a user-selected validation command without changing task state."""
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        self.emit("validation.started", self.task.id, command=command)
        result = self.run_tool("exec", command=command)
        self.emit("validation.completed" if result.ok else "validation.failed", self.task.id, ok=result.ok, command=command)
        return result

    def checkpoint(self, label: str = "manual") -> dict[str, object]:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        diff = subprocess.run(["git", "diff", "--binary"], cwd=self.workspace, capture_output=True, text=True)
        snapshot = {"label": label, "task_id": self.task.id, "diff": diff.stdout, "event_count": len(self.events.list())}
        self.emit("checkpoint.created", self.task.id, label=label, changed=bool(diff.stdout))
        return snapshot

    def stop(self) -> None:
        if self.task and self.task.status == "running":
            self.task.status = "stopped"
            self.emit("task.stopped", self.task.id)
