from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from threading import RLock
from pathlib import Path
from typing import Any, Callable, Iterator

from .ui.text import display_width

from .events import Event, EventStore
from .background import BackgroundTask, BackgroundTaskManager
from .policy import ApprovalMode, Policy, Risk
from .persistence import SQLiteEventStore
from .provider import OpenAICompatible, tool_schemas
from .lock import WorkspaceLock
from .schema import SchemaError, validate_tool_arguments
from .tools import ToolResult, Tools, classify_command
from .usage import Usage
from .telemetry import TelemetryClient, event_payload

DEFAULT_SYSTEM_PROMPT = """You are Fun, a safety-first terminal coding agent.
The Runtime is authoritative for tool results, workspace boundaries, approvals, and task state.
Inspect before editing. Make small, reversible changes and verify them with focused commands.
Ask before destructive or ambiguous actions; never claim success without a tool result.
For substantial tasks, keep a concise 2-7 step plan and report only useful, auditable updates.
Do not reveal hidden chain-of-thought; summarize decisions and evidence instead.
"""
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT

# The seed plan for small talk.  It is a real plan with real events — the
# Runtime does not special-case it — but it says nothing a reader did not
# already know, so the UI is told not to draw it as progress.
SMALL_TALK_PLAN = ("understand the request", "respond")

# Latin question words are matched on word boundaries, CJK ones as substrings
# (CJK has no word separators).  Plain ``in`` meant "how" fired inside "show",
# "who" inside "whose" and "where" inside "somewhere" — contradicting the
# docstring below, which already claimed word-boundary matching.
QUESTION_PUNCTUATION = ("?", "？")
QUESTION_MARKERS_CJK = ("怎么", "如何", "为什么", "什么", "是否", "解释", "说明", "介绍")
QUESTION_WORDS_PATTERN = re.compile(r"\b(what|how|why|when|where|which|who|whom|whose|explain|describe)\b")
CHANGE_MARKERS_CJK = ("修复", "修改", "实现", "创建", "新增", "删除", "重构", "重命名", "迁移", "更新", "编写", "优化", "升级", "补上", "加上", "改成", "改为")
CHANGE_VERBS_PATTERN = re.compile(
    r"\b(fix|repair|implement|create|build|add|remove|delete|drop|refactor|rename|migrate|update|write|patch|port|upgrade|install|generate|make|replace|extract|split|merge)\b"
)


# A background sub-agent reads and reports; it never proposes a plan and never
# touches the workspace.  Saying so in the prompt is a courtesy — the capability
# boundary is in Policy.
RESEARCH_PROMPT = (
    "You are a read-only research sub-agent inside a coding session. "
    "Use the explore and read tools to answer the question about this workspace. "
    "You cannot modify files or run commands. "
    "Answer in at most six sentences, citing the paths you read."
)

READ_ONLY_TOOLS = ("explore", "read")


def valid_tool_calls(fragments: Any) -> list[dict[str, Any]]:
    """Assemble streamed tool-call fragments, dropping the unusable ones.

    Fragments arrive split across chunks and are concatenated by index, so a
    provider that interleaves indexes, omits an id, or never sends a name
    produces entries that look like calls and are not.  Passing those on
    produced tool calls with an empty name (dispatched as UNSUPPORTED_TOOL) or a
    minted id the model never saw, which then answered a call it had not made.
    """
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in fragments:
        name = str(item.get("name", "")).strip()
        identifier = str(item.get("id", "")).strip()
        if not name or not identifier or identifier in seen:
            continue
        arguments = item.get("arguments", "")
        if not isinstance(arguments, str):
            continue
        seen.add(identifier)
        calls.append({"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}})
    return calls


def checkpoint_digest(session_id: str, task_id: str, diff: str) -> str:
    """Bind a checkpoint to the session and task that produced it."""
    material = f"{session_id}\x00{task_id}\x00{diff}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _collect_response(chunks: Any, cancel: Any = None) -> tuple[str, list[dict[str, Any]]]:
    """Accumulate one streamed completion into text plus tool calls.

    A small, self-contained copy of the main parser: that one is bound to the
    foreground task's events, plan and agent state, none of which a sub-agent
    is allowed to touch.
    """
    content = ""
    calls: dict[str, dict[str, str]] = {}
    for chunk in chunks:
        if cancel is not None and cancel.is_set():
            # Checked per chunk, not per step: a cancel issued during a stream
            # used to sit unheard until the whole completion had arrived, so
            # /cancel said "requested" and then nothing happened for up to the
            # provider timeout.
            break
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content += delta["content"]
        for call in delta.get("tool_calls") or []:
            entry = calls.setdefault(str(call.get("index", 0)), {"name": "", "arguments": "", "id": ""})
            entry["id"] += call.get("id", "")
            function = call.get("function") or {}
            entry["name"] += function.get("name", "")
            entry["arguments"] += function.get("arguments", "")
    return content, valid_tool_calls(calls.values())


def build_system_prompt(preferences: str = "") -> str:
    custom = preferences.strip()[:12000]
    if not custom:
        return DEFAULT_SYSTEM_PROMPT
    return DEFAULT_SYSTEM_PROMPT + "\n\nAdditional user preferences (follow when they do not conflict with Runtime safety rules):\n" + custom


@dataclass
class Task:
    id: str
    goal: str
    status: str = "created"
    agent_state: str = "idle"
    recovery_reason: str | None = None
    failure_reason: str | None = None
    pending_tool: dict[str, Any] | None = None
    plan: list[str] = field(default_factory=list)
    plan_status: list[str] = field(default_factory=list)
    plan_error: str | None = None
    plan_error_summary: dict[str, Any] | None = None
    result: str | None = None
    response_error: dict[str, Any] | None = None
    model_error: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    repair_attempts: int = 0


class Runtime:
    def __init__(self, workspace: str, approval: str = "smart", provider: OpenAICompatible | None = None, event_store: EventStore | None = None, state_dir: str | None = None, approve: Callable[[str, Risk], bool] | None = None, telemetry: TelemetryClient | None = None, model: str = "", system_prompt: str = "") -> None:
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
        self.system_prompt = build_system_prompt(system_prompt)
        self._tool_calls = 0
        self._telemetry_sent = False
        self._task_started_at: float | None = None
        self._model_step_started: float | None = None
        self._model_step_first_token: float | None = None
        self.last_model_timing: dict[str, int | None] | None = None
        # Told whenever the plan changes.  Without it the UI only learned the
        # plan *after* the whole turn, so during the minutes a long turn takes —
        # exactly when a plan is worth looking at — the rail showed nothing and
        # the step counter never moved.
        self.on_plan: Callable[[list[str], list[str]], None] | None = None
        self._closed = False
        self._durable_closed = False
        self._background_closed = False
        self._close_pending = False
        self._release_pending = False
        self._active_turns = 0
        # Guards the open/closed state of the store against the other threads
        # that emit into it: the model worker, background sub-agents, and the UI
        # thread calling stop() from a Ctrl-C.
        self._store_lock = RLock()
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self.background = BackgroundTaskManager(self._emit_background)

    def _reopen_if_needed(self) -> None:
        """Reopen the store for a new task after the previous one ended."""
        if not self._closed:
            return
        if self._state_dir is not None and isinstance(getattr(self.events, "_durable", None), SQLiteEventStore):
            durable = SQLiteEventStore(self._state_dir / "events.db")
            store = EventStore(durable)
            # Carry the existing history across.  A blank projection made
            # ``events.list()`` report only what happened after the reopen — so
            # a checkpoint claimed four events for a session with hundreds — and
            # emptied the duplicate-detection index at the same time.
            store.load(durable.events(self.session_id))
            self.events = store
        self._closed = False
        self._durable_closed = False

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        """End the session: stop the task, cancel sub-agents, release the store.

        Distinct from ``stop()``, which ends a *task* — ``/stop`` should not
        take a running research sub-agent down with it, and neither should the
        end of an ordinary turn.
        """
        if self.task and self.task.status in {"running", "paused"}:
            self.stop()
        elif self.task and self.task.status == "recovery_required":
            self.acknowledge_recovery("stop")
        self.close(shutdown=True)

    @classmethod
    def recover(cls, workspace: str, state_dir: str, session_id: str, approval: str = "smart", provider: OpenAICompatible | None = None, approve: Callable[[str, Risk], bool] | None = None, telemetry: TelemetryClient | None = None, model: str = "", system_prompt: str = "") -> "Runtime":
        """Rebuild a Runtime from its event log.

        ``model``, ``system_prompt`` and ``telemetry`` are accepted because a
        resumed session is still the user's session: without them the dock
        showed no model name, the saved /prompt preference did not apply to new
        tasks, and telemetry was silently off for every resumed session.
        """
        durable = SQLiteEventStore(Path(state_dir) / "events.db")
        store = EventStore(durable)
        history = durable.events(session_id)
        if not history:
            # A session id with no events is a typo or a wrong state dir.
            # Returning a blank session instead looked like the resume had
            # worked and the user's previous task had vanished.
            durable.close()
            raise RuntimeError(f"UNKNOWN_SESSION: {session_id}")
        store.load(history)
        runtime = cls(workspace, approval, provider, event_store=store, state_dir=state_dir, approve=approve, telemetry=telemetry, model=model, system_prompt=system_prompt)
        runtime.session_id = session_id
        events = store.replay(session_id)
        # ``model.completed`` carries ``Usage.as_dict()``, which is the *session
        # cumulative* total, not that turn's delta.  Merging every one of them
        # through an accumulating merge made the replayed total grow with the
        # square of the turn count.  The last snapshot is the answer already.
        last_usage = next(
            (event.payload["usage"] for event in reversed(events)
             if event.type == "model.completed" and isinstance(event.payload.get("usage"), dict)),
            None,
        )
        if last_usage is not None:
            runtime.usage.restore(last_usage)
        task_events = [event for event in events if event.task_id]
        if task_events:
            task_id = task_events[-1].task_id
            created = next((event for event in task_events if event.type == "task.created" and event.task_id == task_id), None)
            if created:
                runtime.task = Task(task_id, str(created.payload.get("goal", "")))
                messages = created.payload.get("messages")
                if isinstance(messages, list):
                    runtime.task.messages = [dict(item) for item in messages if isinstance(item, dict)]
                runtime._replay_task(task_events)
                # The acknowledgement must be newer than the *current* stall.
                # Comparing against task creation meant one recovery ever made
                # every later crash invisible: the second half-executed edit was
                # silently forgotten instead of being offered for recovery.
                own = [event for event in task_events if event.task_id == task_id]
                stalled_at = max((event.seq for event in own if event.type in {"approval.pending", "tool.executing"}), default=None)
                acknowledged = any(
                    event.type in {"recovery.acknowledged", "recovery.discarded", "recovery.marked_failed"}
                    and event.seq > (stalled_at if stalled_at is not None else created.seq)
                    for event in own
                )
                stalled_states = {"approval.pending", "tool.executing", "model.requested", "model.step_started", "response.parsed"}
                if runtime.task.status in {"running", "paused"} and runtime.task.agent_state in stalled_states and not acknowledged:
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
                reason = str(event.payload.get("reason", "unknown"))
                self.task.agent_state = "failed"
                self.task.failure_reason = reason
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
            elif event.type == "model.failed":
                self.task.model_error = {key: event.payload.get(key) for key in ("error_type", "error_tag") if key in event.payload}
            elif event.type == "model.completed":
                self.task.model_error = None
            elif event.type == "task.message":
                message = event.payload.get("message")
                if isinstance(message, dict):
                    if message.get("role") == "system" and self.task.messages and self.task.messages[0].get("role") == "system":
                        self.task.messages[0] = dict(message)
                    else:
                        self.task.messages.append(dict(message))
            elif event.type == "task.result":
                self.task.result = str(event.payload.get("result", ""))
                self.task.agent_state = "completed"
            elif event.type == "response.failed":
                self.task.response_error = {key: event.payload.get(key) for key in ("error_type", "error_tag", "summary") if key in event.payload}
            elif event.type == "response.parsed":
                self.task.response_error = None
            elif event.type == "approval.pending":
                self.task.agent_state = "approval.pending"
                self.task.pending_tool = dict(event.payload)
            elif event.type == "tool.executing":
                self.task.agent_state = "tool.executing"
                self.task.pending_tool = dict(event.payload)
            elif event.type in {"approval.rejected", "approval.failed", "approval.resolved", "tool.completed", "tool.failed"}:
                pending_call = self.task.pending_tool or {}
                call_id = event.payload.get("call_id")
                matches = not pending_call or pending_call.get("call_id") == call_id
                if matches:
                    self.task.agent_state = "ready"
                    if event.type != "approval.resolved":
                        self.task.pending_tool = None
            elif event.type in {"validation.completed", "validation.failed"}:
                self.task.validation = {"ok": bool(event.payload.get("ok")), "command": event.payload.get("command", ""), "text": event.payload.get("text", "")}
            elif event.type in {"repair.started", "repair.completed", "repair.failed", "repair.blocked"}:
                attempt = event.payload.get("attempts", event.payload.get("attempt", 0))
                if isinstance(attempt, int):
                    self.task.repair_attempts = max(self.task.repair_attempts, attempt)

    def _emit_background(self, event_type: str, task_id: str, payload: dict[str, Any]) -> None:
        # One read, not two.  Evaluating ``self.task`` twice let create_task's
        # rollback null it between the check and the use, and the AttributeError
        # was raised inside the worker's try — recording a task that had just
        # succeeded as failed.
        task = self.task
        self.emit(event_type, task.id if task is not None else None, background_task_id=task_id, **payload)

    def spawn_agent(self, goal: str, worker: Callable[[str, Any], Any]) -> BackgroundTask:
        # A session sits closed between prompts — complete()/fail() close the
        # store — so anything that emits outside a task has to reopen first.
        # /diff, /checkpoint and /agent all failed with EVENT_STORE_CLOSED (and
        # /agent reported it as "configure a provider first") for the entire
        # idle time of every session.
        self._reopen_if_needed()
        return self.background.spawn(goal, worker, kind="subagent", parent_task_id=self.task.id if self.task else None)

    def spawn_research(self, goal: str, max_steps: int = 6) -> BackgroundTask:
        """Answer ``goal`` in the background, read-only.

        Sub-agents are deliberately researchers, not workers.  A background
        agent that could edit or exec would need concurrent approvals arbitrated
        against the foreground task, concurrent writes to one workspace, and a
        second answer to "who owns the plan" — none of which this Runtime has.
        Read-only removes all three questions and keeps the useful case: reading
        around the codebase while the main task continues.

        The restriction is enforced by :class:`Policy`, not by the prompt: the
        sub-agent's own ``Tools`` is built in a read-only agent mode, so ``edit``
        and ``exec`` are refused even if the model asks for them.
        """
        if self.provider is None:
            raise RuntimeError("PROVIDER_NOT_CONFIGURED")
        if not goal.strip():
            raise ValueError("EMPTY_BACKGROUND_GOAL")
        provider = self.provider
        policy = Policy(self.policy.mode, agent_mode="Review", protected_names=self.policy.protected_names)
        tools = Tools(self.tools.guard.root, policy)
        schemas = [schema for schema in tool_schemas() if schema["function"]["name"] in READ_ONLY_TOOLS]

        def worker(text: str, cancel: Any) -> str:
            messages = [{"role": "system", "content": RESEARCH_PROMPT}, {"role": "user", "content": text}]
            answer = ""
            for _ in range(max_steps):
                if cancel.is_set():
                    return answer or "cancelled"
                content, calls = _collect_response(provider.stream(messages, schemas), cancel)
                if content:
                    answer = content
                if not calls:
                    return answer
                messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
                for call in calls:
                    if cancel.is_set():
                        return answer or "cancelled"
                    name = call["function"]["name"]
                    try:
                        arguments = validate_tool_arguments(name, json.loads(call["function"]["arguments"] or "{}"))
                        result = getattr(tools, name)(**arguments) if policy.allows(name) else ToolResult(False, "MODE_FORBIDS_TOOL")
                        payload = result.text
                    except Exception as exc:  # a sub-agent must not kill the session
                        payload = f"{type(exc).__name__}: {exc}"
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": payload[:4000]})
            return answer or "no answer within the step budget"

        return self.spawn_agent(goal, worker)

    def background_tasks(self) -> list[BackgroundTask]:
        return self.background.list()

    def cancel_background_task(self, task_id: str) -> None:
        # A foreground turn closes the durable store, but background research is
        # deliberately allowed to outlive that turn.  Reopen before recording a
        # later /cancel so the in-memory cancellation and append-only history do
        # not diverge with EVENT_STORE_CLOSED.
        self._reopen_if_needed()
        self.background.cancel(task_id)

    def emit(self, event_type: str, task_id: str | None = None, **payload: object) -> Event:
        with self._store_lock:
            if self._durable_closed:
                # A named refusal, not a sqlite ProgrammingError surfacing from
                # whichever thread happened to be writing when the store shut.
                raise RuntimeError("EVENT_STORE_CLOSED")
            # Event itself snapshots the payload, so every construction site is
            # covered rather than only this one.
            event = Event(event_type, self.session_id, task_id, dict(payload))
            stored = self.events.append(event)
            if stored.id != event.id:
                # append() returns its argument, so the old identity check could
                # never fire.  What it hid is the store's idempotent branch,
                # which returns the *existing* event without persisting — a real
                # outcome that emit() used to report as a fresh write.
                raise RuntimeError("EVENT_APPEND_FAILED")
            return stored

    def create_task(self, goal: str) -> Task:
        self._reopen_if_needed()
        if self.task and self.task.status == "running":
            raise RuntimeError("TASK_ALREADY_RUNNING")
        # The same guard set_goal has.  run_goal calls create_task directly, so
        # a typed prompt used to overwrite a paused task (orphaning it in the
        # log) or a task awaiting recovery — silently discarding a half-executed
        # destructive call with no acknowledgement event.
        if self.task and self.task.status == "paused":
            raise RuntimeError("TASK_PAUSED")
        if self.task and self.task.status == "recovery_required":
            raise RuntimeError("RECOVERY_REQUIRED")
        acquired = False
        if not self.lock.held:
            self.lock.acquire()
            acquired = True
        candidate = Task(f"task_{uuid.uuid4().hex[:12]}", goal, "created")
        candidate.plan = self._initial_plan(goal)
        candidate.plan_status = ["pending"] * len(candidate.plan)
        candidate.agent_state = "planning"
        # Carry the conversation across tasks.  The Runtime models a *task*
        # while the UI shows one continuous conversation, and nothing bridged
        # the two: every prompt started from an empty history, so "what did I
        # just ask you?" was unanswerable and a follow-up like "now do the same
        # for the other file" had no referent.  The existing per-request
        # compaction bounds how much of this actually reaches the provider.
        candidate.messages = [{"role": "system", "content": self.system_prompt}]
        candidate.messages.extend(self._carried_history())
        candidate.messages.append({"role": "user", "content": goal})
        try:
            events = [
                Event("task.created", self.session_id, candidate.id, {"goal": goal, "messages": candidate.messages}),
                Event("plan.created", self.session_id, candidate.id, {"steps": candidate.plan}),
                Event("task.started", self.session_id, candidate.id, {}),
                Event("agent.node", self.session_id, candidate.id, {"node": "ready"}),
            ]
            self.events.append_many(events)
            candidate.status = "running"
            candidate.agent_state = "ready"
            # Per task, not per process: _telemetry_sent latched on the first
            # task, so every later task in the session reported nothing.
            self._telemetry_sent = False
            self.task = candidate
            self._task_started_at = time.monotonic()
            return candidate
        except Exception:
            if acquired:
                self.lock.release()
            self.task = None
            raise

    def _carried_history(self, max_messages: int = 24) -> list[dict[str, Any]]:
        """The previous task's conversation, minus its system prompt.

        Trimmed on a turn boundary so an assistant message with ``tool_calls``
        is never separated from the ``tool`` messages answering it — the same
        invariant ``_model_messages`` keeps.
        """
        if not self.task or not self.task.messages:
            return []
        history = [item for item in self.task.messages if item.get("role") != "system"]
        if len(history) > max_messages:
            history = history[-max_messages:]
        while history and history[0].get("role") == "tool":
            history.pop(0)
        return [dict(item) for item in history]

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
        self._reopen_if_needed()
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
        """Pick a starting plan shape from the goal's intent.

        This is only a seed: the model may propose a better plan and the Runtime
        will accept it through ``replace_plan``.  Latin verbs are matched on word
        boundaries so "fixture" is not read as "fix", while CJK markers are
        matched as substrings because CJK text has no word separators.  Size uses
        display width rather than ``len`` so a short Chinese sentence is not
        mistaken for a trivial one-word greeting.
        """
        text = goal.strip().lower()
        asks_question = (
            any(mark in text for mark in QUESTION_PUNCTUATION)
            or any(marker in text for marker in QUESTION_MARKERS_CJK)
            or bool(QUESTION_WORDS_PATTERN.search(text))
        )
        changes_workspace = any(marker in text for marker in CHANGE_MARKERS_CJK) or bool(CHANGE_VERBS_PATTERN.search(text))
        if not asks_question and not changes_workspace and display_width(text) <= 16:
            return list(SMALL_TALK_PLAN)
        if changes_workspace:
            return ["inspect workspace", "locate relevant code", "apply a minimal change", "run focused validation"]
        return ["understand the request", "inspect workspace if needed", "report verified findings"]

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
        self._announce_plan()

    def _announce_plan(self) -> None:
        """Push the current plan to whoever is drawing it, never raising."""
        if self.on_plan is None or not self.task:
            return
        try:
            self.on_plan(list(self.task.plan), list(self.task.plan_status))
        except Exception:
            return

    def update_plan_step(self, index: int, status: str, evidence: str = "") -> None:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        if index < 0 or index >= len(self.task.plan) or status not in {"pending", "active", "done", "blocked"}:
            raise RuntimeError("INVALID_PLAN_STEP")
        self.emit("plan.step_updated", self.task.id, index=index, status=status, evidence=evidence)
        self.task.plan_status[index] = status
        self._announce_plan()

    def _active_plan_index(self) -> int | None:
        if not self.task:
            return None
        for index, status in enumerate(self.task.plan_status):
            if status in {"active", "pending"}:
                return index
        return None

    def _ready_after_tool(self) -> None:
        if not self.task:
            return
        self.emit("agent.node", self.task.id, node="ready")
        self.task.agent_state = "ready"

    def run_tool(self, name: str, on_status: Callable[[str, dict[str, Any]], None] | None = None, call_id: str | None = None, **kwargs: object) -> ToolResult:
        """Execute one tool call.

        ``call_id`` correlates every event of a single call.  Callers that
        already have an identifier — the model's own tool-call id — should pass
        it, so a UI does not see one call as two unrelated ones.
        """
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        plan_index = self._active_plan_index()
        if plan_index is not None and self.task.plan_status[plan_index] == "pending":
            self.update_plan_step(plan_index, "active", f"tool:{name}")
        call_id = call_id or f"tool_{uuid.uuid4().hex[:10]}"
        self.task.pending_tool = {"call_id": call_id, "name": name, "arguments": dict(kwargs)}
        # A copy: the event log is append-only, and this dict is mutated later
        # (``exec`` has a progress callback bolted on), which would otherwise
        # edit an event that has already been recorded.
        self.emit("tool.requested", self.task.id, call_id=call_id, name=name, arguments=dict(kwargs))

        def refuse(result: ToolResult, **extra: Any) -> ToolResult:
            """End the call the same way every path must end it.

            Four early returns used to skip either ``call_id`` or ``on_status``
            or both — and ``UiState.tool_status`` drops an event with no
            ``call_id``, so those calls left a card sitting at "queued" (or, for
            a denied approval, at "running") forever after the call was over.
            """
            payload = {"call_id": call_id, "name": name, "ok": False, "text": result.text, "changed": [], **extra}
            self.emit("tool.failed", self.task.id, **payload)
            if on_status is not None:
                on_status("tool.failed", dict(payload))
            self.task.pending_tool = None
            self._ready_after_tool()
            return result

        if not self.policy.allows(name):
            return refuse(ToolResult(False, f"MODE_FORBIDS_TOOL: {self.policy.agent_mode} is read-only"), error_tag="MODE_FORBIDS_TOOL")
        try:
            kwargs = validate_tool_arguments(name, kwargs)
        except SchemaError as exc:
            return refuse(ToolResult(False, str(exc)), error_tag="INVALID_TOOL_ARGUMENTS")
        write_operation = name in {"edit", "exec"}
        risk = self.policy.risk_for(name, write=write_operation)
        # The gate is set by what the command actually is, not by the tool it
        # arrived through.  A flat "medium" for every exec meant the user was
        # asked to approve `rm -rf` as a medium-risk call, and then the tool
        # refused it anyway after they said yes.
        subject = name
        if name == "exec":
            plan = classify_command(str(kwargs.get("command", "")), self.tools.guard.root)
            if plan.refusal:
                return refuse(ToolResult(False, plan.refusal, plan.risk), error_tag=plan.refusal.split(":")[0])
            risk = plan.risk
            # "Always allow" is remembered against the program, not against the
            # word "exec": approving one awk should not silently approve rm.
            subject = f"exec:{plan.program}"
        approved = False
        if self.policy.requires_approval(risk):
            self.emit("approval.pending", self.task.id, call_id=call_id, name=name, risk=risk.value, arguments=dict(kwargs))
            if on_status is not None:
                on_status("approval.pending", {"call_id": call_id, "name": name, "risk": risk.value, "arguments": dict(kwargs)})
            try:
                allowed = self.approve(subject, risk) if self.approve else False
                if not isinstance(allowed, bool):
                    raise TypeError("approval callback must return bool")
            except Exception as exc:
                failure_type = type(exc).__name__
                failures = [
                    Event("approval.failed", self.session_id, self.task.id, {"call_id": call_id, "name": name, "error_type": failure_type, "error_tag": "APPROVAL_CALLBACK_FAILED"}),
                    Event("tool.failed", self.session_id, self.task.id, {"call_id": call_id, "name": name, "ok": False, "error_type": failure_type, "error_tag": "APPROVAL_CALLBACK_FAILED"}),
                ]
                self.events.append_many(failures)
                pending = self.task.pending_tool
                try:
                    self._ready_after_tool()
                except Exception:
                    self.task.pending_tool = pending
                    raise
                self.task.pending_tool = None
                raise
            self.emit("approval.resolved", self.task.id, call_id=call_id, name=name, allowed=allowed)
            if on_status is not None:
                on_status("approval.resolved", {"call_id": call_id, "name": name, "allowed": allowed})
            if not allowed:
                self.emit("approval.rejected", self.task.id, call_id=call_id, name=name, risk=risk.value, reason="callback_denied")
                return refuse(ToolResult(False, "APPROVAL_REQUIRED", risk), error_tag="APPROVAL_DENIED")
            approved = True
        registered: dict[str, Callable[..., ToolResult]] = {
            "explore": self.tools.explore,
            "read": self.tools.read,
            "edit": self.tools.edit,
            "exec": self.tools.exec,
        }
        method = registered.get(name)
        if method is None:
            return refuse(ToolResult(False, "UNSUPPORTED_TOOL"), error_tag="UNSUPPORTED_TOOL")
        self.emit("tool.executing", self.task.id, call_id=call_id, name=name, arguments=dict(kwargs))
        started = time.monotonic()
        if on_status is not None:
            # With the arguments.  Only approval.pending carried them, so for
            # any tool that does not need approval — read and explore always,
            # everything in auto mode — the card rendered as a bare "read" with
            # no path and "exec" with no command.
            on_status("tool.executing", {"call_id": call_id, "name": name, "arguments": dict(kwargs)})
        try:
            call_kwargs = dict(kwargs)
            if name == "exec":
                # Tell the tool the gate was already passed, so it does not
                # refuse a command the user just allowed.
                call_kwargs["approved"] = approved
                call_kwargs["on_progress"] = lambda elapsed: on_status("tool.progress", {"call_id": call_id, "name": name, "elapsed_ms": int(elapsed * 1000)}) if on_status is not None else None
            result = method(**call_kwargs)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self.emit("tool.failed", self.task.id, call_id=call_id, name=name, error_type=type(exc).__name__, error_tag="TOOL_EXECUTION_FAILED", elapsed_ms=elapsed_ms)
            if on_status is not None:
                on_status("tool.failed", {"call_id": call_id, "name": name, "ok": False, "elapsed_ms": elapsed_ms, "error": type(exc).__name__})
            # A tool that raised is a tool that finished.  Leaving pending_tool
            # set marked the call as unresolved, so the next recovery offered to
            # re-run something that had already failed, and the plan step stayed
            # "active" for the rest of the task.
            self.task.pending_tool = None
            if plan_index is not None and self.task.status == "running":
                try:
                    self.update_plan_step(plan_index, "blocked", type(exc).__name__)
                except RuntimeError:
                    pass
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.emit("tool.completed" if result.ok else "tool.failed", self.task.id, call_id=call_id, name=name, ok=result.ok, text=result.text, changed=result.changed or [], elapsed_ms=elapsed_ms, exit_code=result.exit_code)
        if on_status is not None:
            on_status("tool.completed" if result.ok else "tool.failed", {"call_id": call_id, "name": name, "ok": result.ok, "elapsed_ms": elapsed_ms, "exit_code": result.exit_code, "text": result.text[:500]})
        self.task.pending_tool = None
        if plan_index is not None and self.task.status == "running":
            # Only while the task still is one.  A Ctrl-C landing between the
            # tool finishing and this line moved the task to "stopped", and the
            # plan update then raised NO_ACTIVE_TASK — reporting a clean
            # interrupt as an internal error and marking the turn failed.
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
            self.close()
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

    def _model_messages(self, max_chars: int = 32000, max_item_chars: int = 12000) -> list[dict[str, Any]]:
        """Keep requests bounded while preserving the system prompt and latest turn."""
        if not self.task:
            return []
        messages = self.task.messages
        normalized: list[dict[str, Any]] = []
        item_trimmed = False
        for item in messages:
            content = item.get("content")
            if isinstance(content, str) and len(content) > max_item_chars:
                item = dict(item)
                item["content"] = content[:max_item_chars] + "\n[output truncated for context]"
                item_trimmed = True
            normalized.append(item)
        if sum(len(str(item.get("content", ""))) for item in normalized) <= max_chars and not item_trimmed:
            return normalized
        head = normalized[:2]
        # Cut on turn boundaries, never inside one.  An assistant message with
        # ``tool_calls`` and the ``role: "tool"`` messages answering it are one
        # unit: dropping the assistant half leaves an orphan tool reply, and
        # every OpenAI-compatible endpoint rejects that request with a 400.
        groups: list[list[dict[str, Any]]] = []
        for item in normalized[2:]:
            if item.get("role") == "tool" and groups:
                groups[-1].append(item)
            else:
                groups.append([item])
        tail: list[dict[str, Any]] = []
        size = sum(len(str(item.get("content", ""))) for item in head)
        for group in reversed(groups):
            group_size = sum(len(str(item.get("content", ""))) for item in group)
            if tail and size + group_size > max_chars:
                break
            tail[:0] = group
            size += group_size
        # A tail that still opens with an orphan reply means one group alone
        # exceeded the budget; drop the orphans rather than send a 400.
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
        self.emit("context.compacted", self.task.id, original_messages=len(messages), retained_messages=len(head) + len(tail), max_chars=max_chars, max_item_chars=max_item_chars, item_trimmed=item_trimmed)
        return head + tail

    def request_model(self) -> Any:
        if not self.provider or not self.task:
            raise RuntimeError("PROVIDER_NOT_CONFIGURED")
        self._ensure_running()
        self._model_step_started = time.monotonic()
        self._model_step_first_token = None
        self._node("model.requested")
        self.emit("model.step_started", self.task.id)
        try:
            return self.provider.stream(self._model_messages(), tool_schemas())
        except Exception as exc:
            try:
                failure = {"error_type": type(exc).__name__, "error_tag": getattr(exc, "error_tag", "MODEL_REQUEST_FAILED")}
                self.emit("model.failed", self.task.id, **failure)
                self.task.model_error = failure
                self.emit("agent.node", self.task.id, node="ready")
                self.task.agent_state = "ready"
            except Exception:
                pass
            raise

    def parse_model_response(self, chunks: Any, on_text: Callable[[str], None] | None = None) -> tuple[str, list[dict[str, Any]]]:
        stats: dict[str, Any] = {"content_length": 0, "tool_calls": 0, "chunk_types": []}
        try:
            return self._parse_model_chunks(chunks, on_text, stats)
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc) in {"TASK_NOT_RUNNING", "EVENT_STORE_CLOSED"}:
                # The user stopped the task.  Blaming the provider for a clean
                # cancellation is a lie that then shows up in the transcript.
                raise
            if self.task:
                try:
                    # provider.stream is a generator function, so its body does
                    # not run until iteration — every provider failure surfaces
                    # here, not at the call site, and hard-coding
                    # MALFORMED_RESPONSE made the durable log blame the parser
                    # for auth, network and timeout failures alike.
                    tag = getattr(exc, "error_tag", "") or "MALFORMED_RESPONSE"
                    if tag != "MALFORMED_RESPONSE":
                        self.emit("model.failed", self.task.id, error_type=type(exc).__name__, error_tag=tag)
                        self.task.model_error = {"error_type": type(exc).__name__, "error_tag": tag}
                    failure = {"error_type": type(exc).__name__, "error_tag": tag, "summary": stats}
                    self.emit("response.failed", self.task.id, **failure)
                    self.task.response_error = failure
                    self.emit("agent.node", self.task.id, node="ready")
                    self.task.agent_state = "ready"
                except Exception:
                    pass
            raise

    def _parse_model_chunks(self, chunks: Any, on_text: Callable[[str], None] | None = None, stats: dict[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]]:
        content = ""
        calls: dict[str, dict[str, str]] = {}
        proposed_plan: list[str] | None = None
        for chunk in chunks:
            self._ensure_running()
            if stats is not None:
                chunk_type = type(chunk).__name__
                if chunk_type not in stats["chunk_types"] and len(stats["chunk_types"]) < 4:
                    stats["chunk_types"].append(chunk_type)
            if chunk.get("_meta", {}).get("ttft_ms") is not None:
                self.usage.ttft_ms = int(chunk["_meta"]["ttft_ms"])
                if self._model_step_first_token is None and self._model_step_started is not None:
                    self._model_step_first_token = time.monotonic()
                    self.emit("model.first_token", self.task.id, ttft_ms=self.usage.ttft_ms)
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
                if stats is not None:
                    stats["content_length"] = len(content)
                if on_text:
                    on_text(delta["content"])
            for call in delta.get("tool_calls") or []:
                entry = calls.setdefault(str(call.get("index", 0)), {"name": "", "arguments": "", "id": ""})
                if stats is not None:
                    stats["tool_calls"] = len(calls)
                entry["id"] += call.get("id", "")
                function = call.get("function") or {}
                entry["name"] += function.get("name", "")
                entry["arguments"] += function.get("arguments", "")
        parsed = valid_tool_calls(calls.values())
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
        self.emit("response.parsed", self.task.id, content_length=len(content), tool_calls=len(parsed), plan_updated=plan_updated, summary={"content_length": len(content), "tool_calls": len(parsed)})
        self._node("response.parsed", content_length=len(content), tool_calls=len(parsed), plan_updated=plan_updated)
        self.task.response_error = None
        self.emit("agent.node", self.task.id, node="ready")
        self.task.agent_state = "ready"
        return content, parsed

    def execute_tool_calls(self, calls: list[dict[str, Any]], on_status: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        """Run every call the model asked for, and reply to every one of them.

        The reply is an invariant, not a happy-path step.  An exception anywhere
        in here — an approval callback that raises, a tool that raises, a Ctrl-C
        arriving between calls — used to leave the assistant's ``tool_calls``
        message with fewer ``role: "tool"`` replies than it declared, and every
        OpenAI-compatible endpoint rejects that conversation with a 400 from
        then on.  So the ``finally`` fills in whatever is missing.
        """
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        self._node("tools.executing", count=len(calls))
        answered: set[str] = set()

        def reply(call_id: str, text: str) -> None:
            if call_id in answered:
                return
            answered.add(call_id)
            message = {"role": "tool", "tool_call_id": call_id, "content": text}
            self.task.messages.append(message)
            try:
                self.emit("task.message", self.task.id, message=message)
            except Exception:
                pass

        try:
            for call in calls:
                self._ensure_running()
                name = call["function"]["name"]
                try:
                    arguments = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    self.emit("tool.failed", self.task.id, call_id=call["id"], name=name, ok=False, error_tag="INVALID_TOOL_ARGUMENTS")
                    self.task.pending_tool = None
                    result = ToolResult(False, "INVALID_ARGUMENTS")
                else:
                    self.emit("model.tool_call", self.task.id, call_id=call["id"], name=name)
                    self._tool_calls += 1
                    if on_status is not None:
                        on_status("tool.started", {"name": name, "call_id": call["id"], "arguments": dict(arguments)})
                    result = self.run_tool(name, on_status=on_status, call_id=call["id"], **arguments)
                reply(call["id"], result.text)
        finally:
            for call in calls:
                reply(call["id"], "NOT_EXECUTED: the session interrupted this call")
            try:
                self.emit("agent.node", self.task.id, node="ready")
                self.task.agent_state = "ready"
            except Exception:
                pass

    def run_model_turn(self, on_text: Callable[[str], None] | None = None, on_status: Callable[[str, dict[str, Any]], None] | None = None, max_steps: int = 8) -> str:
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        self._enter_turn()
        try:
            return self._run_model_turn(on_text, on_status, max_steps)
        finally:
            self._leave_turn()

    def _run_model_turn(self, on_text: Callable[[str], None] | None = None, on_status: Callable[[str, dict[str, Any]], None] | None = None, max_steps: int = 8) -> str:
        final_text = ""
        for _ in range(max_steps):
            self._ensure_running()
            content, calls = self.parse_model_response(self.request_model(), on_text)
            if content:
                final_text += content
            if not calls:
                if content:
                    message = {"role": "assistant", "content": content}
                    self.task.messages.append(message)
                    self.emit("task.message", self.task.id, message=message)
                timing = {
                    "step_ms": None if self._model_step_started is None else int((time.monotonic() - self._model_step_started) * 1000),
                    "ttft_ms": self.usage.ttft_ms,
                }
                self.last_model_timing = timing
                self.emit("model.completed", self.task.id, text=content, usage=self.usage.as_dict(), timing=timing)
                self.task.model_error = None
                return final_text
            message = {"role": "assistant", "content": content or None, "tool_calls": calls}
            self.task.messages.append(message)
            self.emit("task.message", self.task.id, message=message)
            self.execute_tool_calls(calls, on_status=on_status)
        self.emit("task.blocked", self.task.id, reason="TOOL_BUDGET_EXCEEDED")
        raise RuntimeError("TASK_BUDGET_EXCEEDED")

    def _node(self, node: str, **payload: object) -> None:
        if self.task:
            self.emit("agent.node", self.task.id, node=node, **payload)
            self.task.agent_state = node

    def validate(self, command: str) -> ToolResult:
        """Run validation and record evidence for a bounded repair loop."""
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        self.emit("validation.started", self.task.id, command=command)
        self.task.agent_state = "validation.started"
        result = self.run_tool("exec", command=command)
        validation = {"ok": result.ok, "command": command, "text": result.text}
        self.emit("validation.completed" if result.ok else "validation.failed", self.task.id, **validation)
        self.task.validation = validation
        if self.task.plan_status:
            index = len(self.task.plan_status) - 1
            self.update_plan_step(index, "done" if result.ok else "blocked", result.text[:500])
        self.emit("agent.node", self.task.id, node="ready")
        self.task.agent_state = "ready"
        return result

    def repair(self, command: str, max_attempts: int = 2) -> ToolResult:
        if not self.task or self.task.status != "running":
            raise RuntimeError("NO_ACTIVE_TASK")
        if self.task.repair_attempts >= max_attempts:
            self.emit("repair.blocked", self.task.id, reason="REPAIR_BUDGET_EXCEEDED", attempts=self.task.repair_attempts)
            return ToolResult(False, "REPAIR_BUDGET_EXCEEDED")
        attempt = self.task.repair_attempts + 1
        self.emit("repair.started", self.task.id, attempt=attempt)
        self.task.repair_attempts = attempt
        self.emit("agent.node", self.task.id, node="repair.started", attempt=attempt)
        self.task.agent_state = "repair.started"
        try:
            result = self.validate(command)
        except Exception as exc:
            try:
                self.emit("repair.failed", self.task.id, attempt=attempt, ok=False, error=str(exc))
                self.emit("agent.node", self.task.id, node="ready")
                self.task.agent_state = "ready"
            except Exception:
                pass
            raise
        try:
            self.emit("repair.completed" if result.ok else "repair.failed", self.task.id, attempt=attempt, ok=result.ok)
            self.emit("agent.node", self.task.id, node="ready")
            self.task.agent_state = "ready"
        except Exception:
            pass
        return result

    def checkpoint(self, label: str = "manual") -> dict[str, object]:
        self._reopen_if_needed()
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        diff = subprocess.run(["git", "diff", "--binary"], cwd=self.workspace, capture_output=True, text=True)
        snapshot = {
            "label": label, "task_id": self.task.id, "diff": diff.stdout,
            "event_count": len(self.events.list()), "status": self.task.status, "goal": self.task.goal,
            "session_id": self.session_id,
            # Restoring runs `git apply` on this text, so a snapshot has to be
            # provably ours before it is trusted with the worktree.
            "digest": checkpoint_digest(self.session_id, self.task.id, diff.stdout),
        }
        self.emit("checkpoint.created", self.task.id, label=label, changed=bool(diff.stdout), snapshot=snapshot)
        return snapshot

    def restore_checkpoint(self, snapshot: dict[str, object], discard_other_changes: bool = False) -> None:
        """Restore while this Runtime exclusively owns an idle workspace."""
        with self._store_lock:
            if self._active_turns > 0:
                raise RuntimeError("CHECKPOINT_TURN_ACTIVE")
        acquired = False
        if not self.lock.held:
            self.lock.acquire()
            acquired = True
        try:
            self._restore_checkpoint_owned(snapshot, discard_other_changes)
        finally:
            if acquired:
                self.lock.release()

    def _restore_checkpoint_owned(self, snapshot: dict[str, object], discard_other_changes: bool = False) -> None:
        """Put the worktree back to a checkpoint this Runtime took.

        Two things are checked that were not.  The snapshot must carry our own
        digest — restoring runs ``git apply`` on whatever text it holds, so an
        unverified snapshot is an arbitrary patch applied to the user's tree.
        And restoring discards *every* uncommitted change, not only the ones the
        checkpoint knew about, so work done by hand since would vanish without
        being mentioned; that now requires saying so.
        """
        if not self.task:
            raise RuntimeError("NO_ACTIVE_TASK")
        if snapshot.get("task_id") != self.task.id:
            raise RuntimeError("CHECKPOINT_TASK_MISMATCH")
        if snapshot.get("session_id") not in (None, self.session_id):
            raise RuntimeError("CHECKPOINT_SESSION_MISMATCH")
        diff = snapshot.get("diff")
        if not isinstance(diff, str):
            raise RuntimeError("INVALID_CHECKPOINT")
        expected = checkpoint_digest(self.session_id, self.task.id, diff)
        if snapshot.get("digest") != expected:
            raise RuntimeError("CHECKPOINT_NOT_TRUSTED")
        current = subprocess.run(["git", "diff", "--binary"], cwd=self.workspace, capture_output=True, text=True).stdout
        if current and current != diff and not discard_other_changes:
            raise RuntimeError("CHECKPOINT_WOULD_DISCARD_CHANGES")
        self.emit("checkpoint.restore_requested", self.task.id, label=snapshot.get("label", "unknown"))
        if current != diff:
            reset_command = ["git", "restore", "--worktree", "--source=HEAD", "--", "."]
            reset = subprocess.run(reset_command, cwd=self.workspace, capture_output=True, text=True)
            if reset.returncode != 0:
                self.emit("checkpoint.restore_failed", self.task.id, error=reset.stderr.strip())
                raise RuntimeError("CHECKPOINT_RESTORE_FAILED")
            if diff:
                patch = subprocess.run(["git", "apply", "--binary", "-"], cwd=self.workspace, input=diff, capture_output=True, text=True)
                if patch.returncode != 0:
                    # The destructive reset already happened.  Put back the
                    # exact tracked worktree diff we observed before attempting
                    # the checkpoint, so a malformed/inapplicable patch does
                    # not turn a failed restore into data loss.
                    subprocess.run(reset_command, cwd=self.workspace, capture_output=True, text=True)
                    rollback = subprocess.run(["git", "apply", "--binary", "-"], cwd=self.workspace, input=current, capture_output=True, text=True) if current else None
                    rollback_error = rollback.stderr.strip() if rollback is not None and rollback.returncode != 0 else ""
                    error = patch.stderr.strip()
                    if rollback_error:
                        error = f"{error}; rollback failed: {rollback_error}"
                    self.emit("checkpoint.restore_failed", self.task.id, error=error)
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
        self.task.result = result
        try:
            self._send_telemetry("completed")
        finally:
            self.lock.release()
            self.close()

    def fail(self, reason: str) -> None:
        if not self.task or self.task.status not in {"running", "paused"}:
            raise RuntimeError("NO_ACTIVE_TASK")
        failed = Event("task.failed", self.session_id, self.task.id, {"reason": reason})
        stopped = Event("task.stopped", self.session_id, self.task.id, {})
        self.events.append_many([failed, stopped])
        self.task.agent_state = "failed"
        self.task.failure_reason = reason
        self.task.status = "stopped"
        try:
            self._send_telemetry("failed")
        finally:
            self.lock.release()
            self.close()

    def _send_telemetry(self, status: str) -> None:
        if not self.telemetry or self._telemetry_sent:
            return
        usage = self.usage.as_dict()
        duration_ms = None if self._task_started_at is None else int(max(0.0, time.monotonic() - self._task_started_at) * 1000)
        payload = event_payload(event="task.finished", install=self.telemetry.install, model=self.model, status=status, input_tokens=usage.get("input_tokens") or 0, output_tokens=usage.get("output_tokens") or 0, total_tokens=usage.get("total_tokens") or 0, tool_calls=self._tool_calls, duration_ms=duration_ms)
        try:
            self.telemetry.send(payload)
        except Exception:
            return
        self._telemetry_sent = True

    def close(self, shutdown: bool = False) -> None:
        """Release the store, but never out from under a turn in flight.

        ``stop()`` runs on the UI thread while the model worker may be between
        two emits.  Closing the shared SQLite connection there raised
        ``ProgrammingError`` inside the worker and lost the tool result it was
        recording, so a close requested mid-turn is deferred to whoever finishes
        the turn.
        """
        if shutdown and not self._background_closed:
            # Always, even if the store is already closed: stop() closes it, and
            # shutdown() then found nothing left to do and left the sub-agent
            # threads running.
            self._background_closed = True
            self.background.close()
        with self._store_lock:
            if self._closed:
                return
            if self._active_turns > 0:
                # Deferred even for a shutdown.  Closing the store under a live
                # turn is exactly the race the counter exists to prevent, and
                # the sub-agents have already been cancelled above.
                self._close_pending = True
                return
            self._closed = True
            self._close_pending = False
        durable = getattr(self.events, "_durable", None)
        close = getattr(durable, "close", None)
        if callable(close):
            with self._store_lock:
                self._durable_closed = True
            close()

    def _enter_turn(self) -> None:
        with self._store_lock:
            self._active_turns += 1

    def _leave_turn(self) -> None:
        with self._store_lock:
            self._active_turns = max(0, self._active_turns - 1)
            idle = self._active_turns == 0
            pending = self._close_pending and idle
            release = self._release_pending and idle
        if release:
            self._release_pending = False
            self.lock.release()
        if pending:
            self.close(shutdown=self._background_closed)

    def stop(self) -> None:
        """End the task from any state that can still be ended.

        ``recovery_required`` used to fall through every branch here: no
        transition, no lock release — but ``close()`` ran anyway.  That left the
        lock file on disk owned by a live PID, the task still awaiting recovery,
        and the store closed, so the next ``/recover`` raised
        ``Cannot operate on a closed database``.
        """
        try:
            if self.task and self.task.status == "recovery_required":
                self.acknowledge_recovery("stop")
            elif self.task and self.task.status in {"running", "paused"}:
                self._transition("stopped", "task.stopped")
                self._send_telemetry("stopped")
        finally:
            # The lock is not released while a turn is still running.  It used
            # to be, so a Ctrl-C during a tool call handed the workspace to
            # another process while this one was still writing to it.
            with self._store_lock:
                busy = self._active_turns > 0
            if not busy and (self.task is None or self.task.status in {"stopped", "failed", "completed"}):
                self.lock.release()
            else:
                self._release_pending = True
            self.close()
