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
from .lock import WorkspaceLock
from .schema import SchemaError, validate_tool_arguments
from .tools import ToolResult, Tools
from .usage import Usage

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
    validation: dict[str, Any] | None = None


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
        self.usage = Usage()
        self.lock = WorkspaceLock(self.workspace, state_dir or str(self.workspace / ".fun"))
        self.task: Task | None = None

    @classmethod
    def recover(cls, workspace: str, state_dir: str, session_id: str, approval: str = "smart", provider: OpenAICompatible | None = None, approve: Callable[[str, Risk], bool] | None = None) -> "Runtime":
        durable = SQLiteEventStore(Path(state_dir) / "events.db")
        store = EventStore(durable)
        store.load(durable.events(session_id))
        runtime = cls(workspace, approval, provider, event_store=store, state_dir=state_dir, approve=approve)
        events = store.replay(session_id)
        task_events = [event for event in events if event.task_id]
        if task_events:
            task_id = task_events[-1].task_id
            created = next((event for event in task_events if event.type == "task.created" and event.task_id == task_id), None)
            if created:
                runtime.task = Task(task_id, str(created.payload.get("goal", "")))
                runtime._replay_task(task_events)
                if runtime.task.status in {"running", "paused"}:
                    if not runtime.lock.adopt_if_owned():
                        runtime.lock.acquire()
        return runtime

    def _replay_task(self, events: list[Event]) -> None:
        if not self.task:
            return
        for event in events:
            if event.task_id != self.task.id:
                continue
            if event.type == "plan.created":
                self.task.plan = list(event.payload.get("steps", []))
            elif event.type == "task.started":
                self.task.status = "running"
            elif event.type == "task.paused":
                self.task.status = "paused"
            elif event.type == "task.resumed":
                self.task.status = "running"
            elif event.type == "task.completed":
                self.task.status = "completed"
            elif event.type == "task.stopped":
                self.task.status = "stopped"
            elif event.type in {"validation.completed", "validation.failed"}:
                self.task.validation = {"ok": bool(event.payload.get("ok")), "command": event.payload.get("command", ""), "text": event.payload.get("text", "")}

    def emit(self, event_type: str, task_id: str | None = None, **payload: object) -> Event:
        event = Event(event_type, self.session_id, task_id, dict(payload))
        stored = self.events.append(event)
        if stored is not event:
            raise RuntimeError("EVENT_APPEND_FAILED")
        return event

    def create_task(self, goal: str) -> Task:
        if self.task and self.task.status == "running":
            raise RuntimeError("TASK_ALREADY_RUNNING")
        if not self.lock.held:
            self.lock.acquire()
        self.task = Task(f"task_{uuid.uuid4().hex[:12]}", goal, "created")
        self.task.plan = self._initial_plan(goal)
        self.task.messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": goal}]
        self.emit("task.created", self.task.id, goal=goal)
        self.emit("plan.created", self.task.id, steps=self.task.plan)
        self._transition("running", "task.started")
        return self.task

    def _transition(self, status: str, event_type: str) -> None:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        allowed = {
            "created": {"running"},
            "running": {"paused", "completed", "stopped"},
            "paused": {"running", "stopped"},
            "completed": set(),
            "stopped": set(),
        }
        if status not in allowed.get(self.task.status, set()):
            raise RuntimeError("INVALID_TASK_TRANSITION")
        self.task.status = status
        self.emit(event_type, self.task.id)

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
        try:
            kwargs = validate_tool_arguments(name, kwargs)
        except SchemaError as exc:
            result = ToolResult(False, str(exc))
            self.emit("tool.failed", self.task.id, name=name, ok=False, text=result.text, changed=[])
            return result
        write_operation = name in {"edit", "exec"}
        risk = self.policy.risk_for(name, write=write_operation)
        if self.policy.requires_approval(risk):
            self.emit("approval.required", self.task.id, name=name, risk=risk.value)
            allowed = self.approve(name, risk) if self.approve else False
            if not allowed:
                result = ToolResult(False, "APPROVAL_REQUIRED", risk)
                self.emit("tool.failed", self.task.id, name=name, ok=False, text=result.text, changed=[])
                return result
        registered: dict[str, Callable[..., ToolResult]] = {
            "explore": self.tools.explore,
            "read": self.tools.read,
            "edit": self.tools.edit,
            "exec": self.tools.exec,
        }
        method = registered.get(name)
        if method is None:
            result = ToolResult(False, "UNSUPPORTED_TOOL")
            self.emit("tool.failed", self.task.id, name=name, ok=False, text=result.text, changed=[])
            return result
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
                if chunk.get("_meta", {}).get("ttft_ms") is not None:
                    self.usage.ttft_ms = int(chunk["_meta"]["ttft_ms"])
                if isinstance(chunk.get("usage"), dict):
                    self.usage.merge_provider(chunk["usage"], self.usage.ttft_ms)
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
                final_text += content
            if not calls:
                if content:
                    self.task.messages.append({"role": "assistant", "content": content})
                self.emit("model.completed", self.task.id, text=content, usage=self.usage.as_dict())
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
        self.task.validation = {"ok": result.ok, "command": command, "text": result.text}
        self.emit("validation.completed" if result.ok else "validation.failed", self.task.id, ok=result.ok, command=command)
        return result

    def checkpoint(self, label: str = "manual") -> dict[str, object]:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        diff = subprocess.run(["git", "diff", "--binary"], cwd=self.workspace, capture_output=True, text=True)
        snapshot = {"label": label, "task_id": self.task.id, "diff": diff.stdout, "event_count": len(self.events.list()), "status": self.task.status, "goal": self.task.goal}
        self.emit("checkpoint.created", self.task.id, label=label, changed=bool(diff.stdout), snapshot=snapshot)
        return snapshot

    def restore_checkpoint(self, snapshot: dict[str, object]) -> None:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        if snapshot.get("task_id") != self.task.id:
            raise RuntimeError("CHECKPOINT_TASK_MISMATCH")
        diff = snapshot.get("diff")
        if not isinstance(diff, str):
            raise RuntimeError("INVALID_CHECKPOINT")
        self.emit("checkpoint.restore_requested", self.task.id, label=snapshot.get("label", "unknown"))
        if diff:
            reset = subprocess.run(["git", "restore", "--worktree", "--source=HEAD", "--", "."], cwd=self.workspace, capture_output=True, text=True)
            if reset.returncode != 0:
                self.emit("checkpoint.restore_failed", self.task.id, error=reset.stderr.strip())
                raise RuntimeError("CHECKPOINT_RESTORE_FAILED")
            patch = subprocess.run(["git", "apply", "--binary", "-"], cwd=self.workspace, input=diff, capture_output=True, text=True)
            if patch.returncode != 0:
                self.emit("checkpoint.restore_failed", self.task.id, error=patch.stderr.strip())
                raise RuntimeError("CHECKPOINT_RESTORE_FAILED")
        self.emit("checkpoint.restored", self.task.id)

    def pause(self) -> None:
        if self.task and self.task.status == "running":
            self._transition("paused", "task.paused")

    def resume(self) -> None:
        if self.task and self.task.status == "paused":
            self._transition("running", "task.resumed")

    def complete(self, result: str = "") -> None:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        self._transition("completed", "task.completed")
        self.emit("task.result", self.task.id, result=result, validation=self.task.validation or {})
        self.lock.release()

    def stop(self) -> None:
        if self.task and self.task.status in {"running", "paused"}:
            self._transition("stopped", "task.stopped")
            self.lock.release()
