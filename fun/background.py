from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event as CancelEvent, Lock, Thread
from typing import Any, Callable
from uuid import uuid4


Worker = Callable[[str, CancelEvent], Any]
Emit = Callable[[str, str, dict[str, Any]], None]


@dataclass
class BackgroundTask:
    id: str
    goal: str
    kind: str = "subagent"
    status: str = "created"
    result: Any = None
    error: str | None = None
    thread: Thread | None = field(default=None, repr=False)
    cancel_event: CancelEvent = field(default_factory=CancelEvent, repr=False)


class BackgroundTaskManager:
    """Small, Runtime-owned executor for cancellable sub-agent work."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = Lock()

    def spawn(self, goal: str, worker: Worker, kind: str = "subagent") -> BackgroundTask:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("EMPTY_BACKGROUND_GOAL")
        if not callable(worker):
            raise TypeError("BACKGROUND_WORKER_REQUIRED")
        task = BackgroundTask(f"bg_{uuid4().hex[:12]}", goal.strip(), kind=kind)
        with self._lock:
            self._tasks[task.id] = task
        self._emit("background.task.created", task.id, {"goal": task.goal, "kind": task.kind})

        def run() -> None:
            with self._lock:
                task.status = "running"
            self._emit("background.task.started", task.id, {"kind": task.kind})
            try:
                task.result = worker(task.goal, task.cancel_event)
                with self._lock:
                    task.status = "cancelled" if task.cancel_event.is_set() else "completed"
                event = "background.task.cancelled" if task.status == "cancelled" else "background.task.completed"
                self._emit(event, task.id, {"kind": task.kind, "result": str(task.result)[:1000]})
            except Exception as exc:
                task.error = type(exc).__name__
                with self._lock:
                    task.status = "cancelled" if task.cancel_event.is_set() else "failed"
                event = "background.task.cancelled" if task.status == "cancelled" else "background.task.failed"
                self._emit(event, task.id, {"kind": task.kind, "error": task.error})

        task.thread = Thread(target=run, name=f"fun-{task.id}", daemon=True)
        task.thread.start()
        return task

    def cancel(self, task_id: str) -> None:
        task = self.get(task_id)
        if task is None:
            raise RuntimeError("BACKGROUND_TASK_NOT_FOUND")
        task.cancel_event.set()
        self._emit("background.task.cancel_requested", task.id, {"kind": task.kind})

    def get(self, task_id: str) -> BackgroundTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[BackgroundTask]:
        with self._lock:
            return list(self._tasks.values())

    def close(self, join_timeout: float = 2.0) -> None:
        tasks = self.list()
        for task in tasks:
            if task.thread and task.thread.is_alive():
                task.cancel_event.set()
        for task in tasks:
            if task.thread and task.thread.is_alive():
                task.thread.join(join_timeout)
