from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event as CancelEvent, Lock, Thread
from time import monotonic
from typing import Any, Callable
from uuid import uuid4


Worker = Callable[[str, CancelEvent], Any]
Emit = Callable[[str, str, dict[str, Any]], None]


@dataclass
class BackgroundTask:
    id: str
    goal: str
    kind: str = "subagent"
    parent_task_id: str | None = None
    run_id: str = field(default_factory=lambda: f"run_{uuid4().hex[:12]}")
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

    MAX_LIVE = 4
    MAX_RETAINED = 20

    def spawn(self, goal: str, worker: Worker, kind: str = "subagent", parent_task_id: str | None = None) -> BackgroundTask:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("EMPTY_BACKGROUND_GOAL")
        if not callable(worker):
            raise TypeError("BACKGROUND_WORKER_REQUIRED")
        task = BackgroundTask(f"bg_{uuid4().hex[:12]}", goal.strip(), kind=kind, parent_task_id=parent_task_id)
        with self._lock:
            # Check and reserve in one critical section.  Checking in one and
            # inserting in another let two callers both see room and both take
            # the last slot.  Each live sub-agent is an OS thread and an
            # outbound connection, and each costs up to MAX_JOIN at exit.
            live = sum(1 for item in self._tasks.values() if item.status in {"created", "running"})
            if live >= self.MAX_LIVE:
                raise RuntimeError("TOO_MANY_BACKGROUND_TASKS")
            self._prune()
            self._tasks[task.id] = task
        self._emit("background.task.created", task.id, {"goal": task.goal, "kind": task.kind, "parent_task_id": task.parent_task_id, "run_id": task.run_id})

        def run() -> None:
            with self._lock:
                task.status = "running"
            self._emit("background.task.started", task.id, {"kind": task.kind, "parent_task_id": task.parent_task_id, "run_id": task.run_id})
            try:
                task.result = worker(task.goal, task.cancel_event)
                with self._lock:
                    task.status = "cancelled" if task.cancel_event.is_set() else "completed"
                payload = {"kind": task.kind, "result": str(task.result)[:1000]}
            except Exception as exc:
                task.error = type(exc).__name__
                with self._lock:
                    task.status = "cancelled" if task.cancel_event.is_set() else "failed"
                payload = {"kind": task.kind, "error": task.error}
            # Emitting is a separate step, outside the try.  It used to be
            # inside it, so a store closed while this task was finishing turned
            # a *completed* task into a failed one — and then the handler's own
            # emit raised again and escaped the thread, printing a traceback
            # into a terminal that is in raw mode with an alternate screen up.
            event = {
                "cancelled": "background.task.cancelled",
                "completed": "background.task.completed",
                "failed": "background.task.failed",
            }[task.status]
            try:
                self._emit(event, task.id, payload)
            except Exception:
                # The session is going away; the result is still on the task.
                return

        task.thread = Thread(target=run, name=f"fun-{task.id}", daemon=True)
        task.thread.start()
        return task

    def cancel(self, task_id: str) -> None:
        task = self.get(task_id)
        if task is None:
            raise RuntimeError("BACKGROUND_TASK_NOT_FOUND")
        with self._lock:
            if task.cancel_event.is_set() or task.status in {"cancelled", "completed", "failed"}:
                return
            task.cancel_event.set()
        self._emit("background.task.cancel_requested", task.id, {"kind": task.kind, "run_id": task.run_id})

    def _prune(self) -> None:
        """Forget the oldest finished tasks.  Caller holds the lock."""
        finished = [item for item in self._tasks.values() if item.status in {"completed", "failed", "cancelled"}]
        for item in finished[: max(0, len(finished) - self.MAX_RETAINED)]:
            self._tasks.pop(item.id, None)

    def get(self, task_id: str) -> BackgroundTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[BackgroundTask]:
        with self._lock:
            return list(self._tasks.values())

    MAX_JOIN = 2.0

    def close(self, join_timeout: float = MAX_JOIN) -> None:
        """Cancel every live task and wait, but only ``join_timeout`` in total.

        The wait used to be per task and sequential, so quitting with five stuck
        sub-agents blocked the UI thread for ten seconds; the threads are daemons
        and their results are already on the task objects, so a shared deadline
        is enough.
        """
        tasks = self.list()
        for task in tasks:
            if task.thread and task.thread.is_alive():
                task.cancel_event.set()
        deadline = monotonic() + join_timeout
        for task in tasks:
            if task.thread and task.thread.is_alive():
                task.thread.join(max(0.0, deadline - monotonic()))
