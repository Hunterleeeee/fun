from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

from .events import Event, EventStore
from .policy import ApprovalMode, Policy
from .tools import ToolResult, Tools


@dataclass
class Task:
    id: str
    goal: str
    status: str = "created"


class Runtime:
    """Small V1 Core orchestration shell.

    The runtime owns task state and events; providers and renderers are adapters.
    The full model-driven planner is intentionally a follow-up implementation.
    """

    def __init__(self, workspace: str, approval: str = "smart") -> None:
        self.session_id = f"ses_{uuid.uuid4().hex[:12]}"
        self.events = EventStore()
        self.policy = Policy(ApprovalMode(approval))
        self.tools = Tools(workspace, self.policy)
        self.task: Task | None = None

    def emit(self, event_type: str, task_id: str | None = None, **payload: object) -> Event:
        return self.events.append(Event(event_type, self.session_id, task_id, dict(payload)))

    def create_task(self, goal: str) -> Task:
        if self.task and self.task.status == "running":
            raise RuntimeError("TASK_ALREADY_RUNNING")
        self.task = Task(f"task_{uuid.uuid4().hex[:12]}", goal, "running")
        self.emit("task.created", self.task.id, goal=goal)
        self.emit("task.started", self.task.id)
        return self.task

    def run_tool(self, name: str, **kwargs: object) -> ToolResult:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        self.emit("tool.requested", self.task.id, name=name, arguments=kwargs)
        method: Callable[..., ToolResult] = getattr(self.tools, name)
        result = method(**kwargs)
        self.emit("tool.completed" if result.ok else "tool.failed", self.task.id, name=name, ok=result.ok, text=result.text)
        return result

    def stop(self) -> None:
        if self.task and self.task.status == "running":
            self.task.status = "stopped"
            self.emit("task.stopped", self.task.id)
