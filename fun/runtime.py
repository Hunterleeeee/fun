from __future__ import annotations

import json
import subprocess
import time
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
from .telemetry import TelemetryClient, event_payload

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
    agent_state: str = "idle"
    recovery_reason: str | None = None
    pending_tool: dict[str, Any] | None = None
    plan: list[str] = field(default_factory=list)
    plan_status: list[str] = field(default_factory=list)
    plan_error: str | None = None
    plan_error_summary: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    repair_attempts: int = 0


class Runtime:
    def __init__(self, workspace: str, approval: str = "smart", provider: OpenAICompatible | None = None, event_store: EventStore | None = None, state_dir: str | None = None, approve: Callable[[str, Risk], bool] | None = None, telemetry: TelemetryClient | None = None, model: str = "") -> None:
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
        self.telemetry = telemetry
        self.model = model
        self._tool_calls = 0
        self._telemetry_sent = False
        self._task_started_at: float | None = None

    @classmethod
    def recover(cls, workspace: str, state_dir: str, session_id: str, approval: str = "smart", provider: OpenAICompatible | None = None, approve: Callable[[str, Risk], bool] | None = None) -> "Runtime":
        durable = SQLiteEventStore(Path(state_dir) / "events.db")
        store = EventStore(durable)
        store.load(durable.events(session_id))
        runtime = cls(workspace, approval, provider, event_store=store, state_dir=state_dir, approve=approve)
        runtime.session_id = session_id
        events = store.replay(session_id)
        for event in events:
            if event.type == "model.completed":
                usage = event.payload.get("usage")
                if isinstance(usage, dict):
                    runtime.usage.merge_provider({
                        "prompt_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                        "completion_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
                        "total_tokens": usage.get("total_tokens"),
                    })
        task_events = [event for event in events if event.task_id]
        if task_events:
            task_id = task_events[-1].task_id
            created = next((event for event in task_events if event.type == "task.created" and event.task_id == task_id), None)
            if created:
                runtime.task = Task(task_id, str(created.payload.get("goal", "")))
                runtime._replay_task(task_events)
                if runtime.task.status in {"running", "paused"} and runtime.task.agent_state in {"approval.pending", "tool.executing"} and not any(event.type in {"recovery.acknowledged", "recovery.discarded", "recovery.marked_failed"} for event in task_events if event.task_id == task_id and event.seq > created.seq):
                    runtime.task.status = "recovery_required"
                    runtime.task.recovery_reason = runtime.task.agent_state
                    runtime.emit("recovery.required", runtime.task.id, reason=runtime.task.recovery_reason)
                if runtime.task.status in {"running", "paused", "recovery_required"}:
                    if not runtime.lock.adopt_if_owned():
                        runtime.lock.acquire()
        return runtime

    def _replay_task(self, events: list[Event]) -> None:
        if not self.task:
            return
        for event in events:
            if event.task_id != self.task.id:
                continue
            if event.type in {"plan.created", "plan.replaced"}:
                self.task.plan = list(event.payload.get("steps", []))
                self.task.plan_status = list(event.payload.get("statuses", ["pending"] * len(self.task.plan)))
                if event.type == "plan.replaced":
                    self.task.plan_error = None
                    self.task.plan_error_summary = None
            elif event.type == "plan.replaced":
                self.task.plan_error = None
                self.task.plan_error_summary = None
            elif event.type == "plan.rejected":
                self.task.plan_error = str(event.payload.get("reason", "INVALID_PLAN"))
                summary = event.payload.get("summary")
                self.task.plan_error_summary = dict(summary) if isinstance(summary, dict) else None
            elif event.type == "plan.step_updated":
                index = event.payload.get("index")
                if isinstance(index, int) and 0 <= index < len(self.task.plan_status):
                    self.task.plan_status[index] = str(event.payload.get("status", "pending"))
            elif event.type == "task.started":
                self.task.status = "running"
            elif event.type == "agent.node":
                self.task.agent_state = str(event.payload.get("node", "idle"))
            elif event.type == "task.paused":
                self.task.status = "paused"
            elif event.type == "task.resumed":
                self.task.status = "running"
            elif event.type == "task.completed":
                self.task.status = "completed"
            elif event.type == "task.stopped":
                self.task.status = "stopped"
            elif event.type == "task.failed":
                self.task.agent_state = "failed"
                self.task.recovery_reason = str(event.payload.get("reason", "unknown"))
            elif event.type == "recovery.required":
                self.task.status = "recovery_required"
                self.task.recovery_reason = str(event.payload.get("reason", "unknown"))
            elif event.type in {"recovery.discarded", "recovery.marked_failed"}:
                self.task.status = "running"
                self.task.pending_tool = None
                self.task.recovery_reason = None
                self.task.agent_state = "ready"
            elif event.type == "recovery.acknowledged":
                action = str(event.payload.get("action", "resume"))
                if action in {"resume", "discard", "mark_failed"}:
                    self.task.status = "running"
                    self.task.recovery_reason = None
                elif action == "stop":
                    self.task.status = "stopped"
                    self.task.recovery_reason = None
            elif event.type == "task.result":
                self.task.agent_state = "completed"
            elif event.type == "approval.pending":
                self.task.agent_state = "approval.pending"
                self.task.pending_tool = dict(event.payload)
            elif event.type == "tool.executing":
                self.task.agent_state = "tool.executing"
                self.task.pending_tool = dict(event.payload)
            elif event.type == "approval.rejected":
                self.task.agent_state = "ready"
                self.task.pending_tool = None
            elif event.type in {"tool.completed", "tool.failed", "approval.resolved"}:
                self.task.agent_state = "ready"
                if event.type != "approval.resolved":
                    self.task.pending_tool = None
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
        acquired = False
        if not self.lock.held:
            self.lock.acquire()
            acquired = True
        candidate = Task(f"task_{uuid.uuid4().hex[:12]}", goal, "created")
        candidate.plan = self._initial_plan(goal)
        candidate.plan_status = ["pending"] * len(candidate.plan)
        candidate.agent_state = "planning"
        candidate.messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": goal}]
        try:
            events = [
                Event("task.created", self.session_id, candidate.id, {"goal": goal}),
                Event("plan.created", self.session_id, candidate.id, {"steps": candidate.plan}),
                Event("task.started", self.session_id, candidate.id, {}),
                Event("agent.node", self.session_id, candidate.id, {"node": "ready"}),
            ]
            self.events.append_many(events)
            candidate.status = "running"
            candidate.agent_state = "ready"
            self.task = candidate
            self._task_started_at = time.monotonic()
            return candidate
        except Exception:
            if acquired:
                self.lock.release()
            self.task = None
            raise

    def _transition(self, status: str, event_type: str) -> None:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        allowed = {
            "created": {"running"},
            "running": {"paused", "completed", "stopped"},
            "paused": {"running", "stopped"},
            "recovery_required": {"running", "stopped"},
            "completed": set(),
            "stopped": set(),
        }
        if status not in allowed.get(self.task.status, set()):
            raise RuntimeError("INVALID_TASK_TRANSITION")
        self.emit(event_type, self.task.id)
        self.task.status = status

    def set_goal(self, goal: str) -> Task:
        if not goal.strip():
            raise RuntimeError("EMPTY_GOAL")
        if self.task and self.task.status in {"running", "paused", "recovery_required"}:
            raise RuntimeError("TASK_ALREADY_ACTIVE")
        if self.task and self.task.status in {"completed", "stopped"}:
            self.emit("goal.replaced", self.task.id, previous=self.task.goal, goal=goal)
        return self.create_task(goal.strip())

    def goal(self) -> str | None:
        return self.task.goal if self.task else None

    @staticmethod
    def _initial_plan(goal: str) -> list[str]:
        lower = goal.lower()
        if any(word in lower for word in ("fix", "修复", "改", "implement", "实现")):
            return ["inspect workspace", "locate relevant code", "apply a minimal change", "run focused validation"]
        return ["inspect workspace", "analyze the request", "report verified findings"]

    def replace_plan(self, steps: list[str]) -> None:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        if not isinstance(steps, list) or any(not isinstance(step, str) or not step.strip() for step in steps):
            raise RuntimeError("INVALID_PLAN")
        normalized = [step.strip() for step in steps]
        if not normalized or len(normalized) > 7:
            raise RuntimeError("INVALID_PLAN")
        previous = list(self.task.plan)
        statuses = ["pending"] * len(normalized)
        self.emit("plan.replaced", self.task.id, previous=previous, steps=normalized, statuses=statuses)
        self.task.plan = normalized
        self.task.plan_status = statuses

    def update_plan_step(self, index: int, status: str, evidence: str = "") -> None:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        if index < 0 or index >= len(self.task.plan) or status not in {"pending", "active", "done", "blocked"}:
            raise RuntimeError("INVALID_PLAN_STEP")
        self.emit("plan.step_updated", self.task.id, index=index, status=status, evidence=evidence)
        self.task.plan_status[index] = status

    def _active_plan_index(self) -> int | None:
        if not self.task:
            return None
        for index, status in enumerate(self.task.plan_status):
            if status in {"active", "pending"}:
                return index
        return None

    def run_tool(self, name: str, **kwargs: object) -> ToolResult:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        plan_index = self._active_plan_index()
        if plan_index is not None and self.task.plan_status[plan_index] == "pending":
            self.update_plan_step(plan_index, "active", f"tool:{name}")
        call_id = f"tool_{uuid.uuid4().hex[:10]}"
        self.task.pending_tool = {"call_id": call_id, "name": name, "arguments": dict(kwargs)}
        self.emit("tool.requested", self.task.id, call_id=call_id, name=name, arguments=kwargs)
        try:
            kwargs = validate_tool_arguments(name, kwargs)
        except SchemaError as exc:
            result = ToolResult(False, str(exc))
            self.emit("tool.failed", self.task.id, name=name, ok=False, text=result.text, changed=[])
            return result
        write_operation = name in {"edit", "exec"}
        risk = self.policy.risk_for(name, write=write_operation)
        if self.policy.requires_approval(risk):
            self.emit("approval.pending", self.task.id, call_id=call_id, name=name, risk=risk.value, arguments=dict(kwargs))
            allowed = self.approve(name, risk) if self.approve else False
            self.emit("approval.resolved", self.task.id, call_id=call_id, name=name, allowed=allowed)
            if not allowed:
                self.emit("approval.rejected", self.task.id, call_id=call_id, name=name, risk=risk.value, reason="callback_denied")
                self.task.pending_tool = None
                result = ToolResult(False, "APPROVAL_REQUIRED", risk)
                self.emit("tool.failed", self.task.id, call_id=call_id, name=name, ok=False, text=result.text, changed=[])
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
        self.emit("tool.executing", self.task.id, call_id=call_id, name=name)
        try:
            result = method(**kwargs)
        except Exception as exc:
            self.emit("tool.failed", self.task.id, name=name, error=str(exc))
            raise
        self.emit("tool.completed" if result.ok else "tool.failed", self.task.id, call_id=call_id, name=name, ok=result.ok, text=result.text, changed=result.changed or [])
        if plan_index is not None:
            self.update_plan_step(plan_index, "done" if result.ok else "blocked", result.text[:500])
        return result

    def recovery_summary(self) -> dict[str, Any] | None:
        if not self.task or self.task.status != "recovery_required":
            return None
        pending = self.task.pending_tool or {}
        return {"reason": self.task.recovery_reason or "unknown", "call_id": pending.get("call_id"), "name": pending.get("name"), "arguments": pending.get("arguments", {})}

    def acknowledge_recovery(self, action: str = "resume") -> None:
        if not self.task or self.task.status != "recovery_required":
            raise RuntimeError("RECOVERY_NOT_REQUIRED")
        if action not in {"resume", "stop", "discard", "mark_failed"}:
            raise RuntimeError("INVALID_RECOVERY_ACTION")
        reason = self.task.recovery_reason or "unknown"
        pending = self.task.pending_tool or {}
        self.emit("recovery.acknowledged", self.task.id, action=action, reason=reason, call_id=pending.get("call_id"), name=pending.get("name"))
        self.task.recovery_reason = None
        if action == "stop":
            self._transition("stopped", "task.stopped")
            self.lock.release()
        elif action == "discard":
            self.task.pending_tool = None
            self.task.agent_state = "ready"
            self._transition("running", "task.resumed")
            self.emit("recovery.discarded", self.task.id)
        elif action == "mark_failed":
            self.task.pending_tool = None
            self.task.agent_state = "ready"
            self._transition("running", "task.resumed")
            self.emit("recovery.marked_failed", self.task.id)
        else:
            self.task.agent_state = "ready"
            self._transition("running", "task.resumed")

    def _ensure_running(self) -> None:
        if not self.task or self.task.status != "running":
            raise RuntimeError("TASK_NOT_RUNNING")

    def request_model(self) -> Any:
        if not self.provider or not self.task:
            raise RuntimeError("PROVIDER_NOT_CONFIGURED")
        self._ensure_running()
        self._node("model.requested")
        return self.provider.stream(self.task.messages, tool_schemas())

    def parse_model_response(self, chunks: Any, on_text: Callable[[str], None] | None = None) -> tuple[str, list[dict[str, Any]]]:
        content = ""
        calls: dict[str, dict[str, str]] = {}
        proposed_plan: list[str] | None = None
        for chunk in chunks:
            self._ensure_running()
            if chunk.get("_meta", {}).get("ttft_ms") is not None:
                self.usage.ttft_ms = int(chunk["_meta"]["ttft_ms"])
            if isinstance(chunk.get("usage"), dict):
                self.usage.merge_provider(chunk["usage"], self.usage.ttft_ms)
            if isinstance(chunk.get("plan"), list):
                proposed_plan = chunk["plan"]
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("plan"), list):
                proposed_plan = delta["plan"]
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
        parsed = [{"id": item["id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}} for item in calls.values()]
        plan_updated = False
        if proposed_plan is not None:
            try:
                self.replace_plan(proposed_plan)
                plan_updated = True
            except RuntimeError as exc:
                if self.task:
                    self.task.plan_error = str(exc)
                summary = {"count": len(proposed_plan), "types": [type(step).__name__ for step in proposed_plan[:7]], "lengths": [len(step) if isinstance(step, str) else None for step in proposed_plan[:7]]}
                if self.task:
                    self.task.plan_error_summary = summary
                self.emit("plan.rejected", self.task.id if self.task else None, reason=str(exc), summary=summary)
        self._node("response.parsed", content_length=len(content), tool_calls=len(parsed), plan_updated=plan_updated)
        return content, parsed

    def execute_tool_calls(self, calls: list[dict[str, Any]]) -> None:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        self._node("tools.executing", count=len(calls))
        for call in calls:
            self._ensure_running()
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                result = ToolResult(False, "INVALID_ARGUMENTS")
            else:
                self.emit("model.tool_call", self.task.id, call_id=call["id"], name=name)
                self._tool_calls += 1
                result = self.run_tool(name, **arguments)
            self.task.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result.text})

    def run_model_turn(self, on_text: Callable[[str], None] | None = None, max_steps: int = 8) -> str:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        final_text = ""
        for _ in range(max_steps):
            self._ensure_running()
            content, calls = self.parse_model_response(self.request_model(), on_text)
            if content:
                final_text += content
            if not calls:
                if content:
                    self.task.messages.append({"role": "assistant", "content": content})
                self.emit("model.completed", self.task.id, text=content, usage=self.usage.as_dict())
                return final_text
            self.task.messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
            self.execute_tool_calls(calls)
        self.emit("task.blocked", self.task.id, reason="TOOL_BUDGET_EXCEEDED")
        raise RuntimeError("TASK_BUDGET_EXCEEDED")

    def _node(self, node: str, **payload: object) -> None:
        if self.task:
            self.task.agent_state = node
            self.emit("agent.node", self.task.id, node=node, **payload)

    def validate(self, command: str) -> ToolResult:
        """Run validation and record evidence for a bounded repair loop."""
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        self._node("validation.started", command=command)
        self.emit("validation.started", self.task.id, command=command)
        result = self.run_tool("exec", command=command)
        if self.task.plan_status:
            index = len(self.task.plan_status) - 1
            self.update_plan_step(index, "done" if result.ok else "blocked", result.text[:500])
        self.task.validation = {"ok": result.ok, "command": command, "text": result.text}
        self.emit("validation.completed" if result.ok else "validation.failed", self.task.id, ok=result.ok, command=command, text=result.text)
        return result

    def repair(self, command: str, max_attempts: int = 2) -> ToolResult:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        if self.task.repair_attempts >= max_attempts:
            self.emit("repair.blocked", self.task.id, reason="REPAIR_BUDGET_EXCEEDED", attempts=self.task.repair_attempts)
            return ToolResult(False, "REPAIR_BUDGET_EXCEEDED")
        self.task.repair_attempts += 1
        self._node("repair.started", attempt=self.task.repair_attempts)
        result = self.validate(command)
        self.emit("repair.completed" if result.ok else "repair.failed", self.task.id, attempt=self.task.repair_attempts, ok=result.ok)
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
        completed = Event("task.completed", self.session_id, self.task.id, {})
        task_result = Event("task.result", self.session_id, self.task.id, {"result": result, "validation": self.task.validation or {}})
        self.events.append_many([completed, task_result])
        self.task.status = "completed"
        try:
            self._send_telemetry("completed")
        finally:
            self.lock.release()

    def fail(self, reason: str) -> None:
        if not self.task or self.task.status not in {"running", "paused"}:
            raise RuntimeError("NO_ACTIVE_TASK")
        self.emit("task.failed", self.task.id, reason=reason)
        self.task.agent_state = "failed"
        self._transition("stopped", "task.stopped")
        try:
            self._send_telemetry("failed")
        finally:
            self.lock.release()

    def _send_telemetry(self, status: str) -> None:
        if not self.telemetry or self._telemetry_sent:
            return
        self._telemetry_sent = True
        usage = self.usage.as_dict()
        duration_ms = None if self._task_started_at is None else int(max(0.0, time.monotonic() - self._task_started_at) * 1000)
        payload = event_payload(event="task.finished", install=self.telemetry.install, model=self.model, status=status, input_tokens=usage.get("input_tokens") or 0, output_tokens=usage.get("output_tokens") or 0, total_tokens=usage.get("total_tokens") or 0, tool_calls=self._tool_calls, duration_ms=duration_ms)
        try:
            self.telemetry.send(payload)
        except Exception:
            return

    def stop(self) -> None:
        if self.task and self.task.status in {"running", "paused"}:
            self._transition("stopped", "task.stopped")
            try:
                self._send_telemetry("stopped")
            finally:
                self.lock.release()
