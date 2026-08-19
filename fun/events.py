"""Append-only event facts and the in-memory projection of an event log.

Sequence numbers are allocated from a single process-wide counter on purpose.
Two ``SQLiteEventStore`` connections can point at the same ``events.db`` inside
one process (recovery opens a second connection while the first is still live),
and ``seq`` is that table's ``INTEGER PRIMARY KEY``.  A per-store counter would
hand both connections the same numbers and turn an ordinary append into a
primary-key collision, so the counter stays global and only ever moves forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Any
from uuid import uuid4


_next_event_seq = 1
_event_seq_lock = Lock()


def next_event_seq() -> int:
    global _next_event_seq
    with _event_seq_lock:
        seq = _next_event_seq
        _next_event_seq += 1
        return seq


def advance_event_seq(minimum: int) -> None:
    global _next_event_seq
    with _event_seq_lock:
        if minimum > _next_event_seq:
            _next_event_seq = minimum


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_payload(value: Any) -> Any:
    """A structural copy of an event payload.

    The log is append-only, so what it recorded must not change afterwards.
    ``task.created`` carried the task's *live* ``messages`` list, so the
    recorded event grew for the rest of the task and replaying it reproduced
    the end state rather than the beginning.
    """
    if isinstance(value, dict):
        return {key: snapshot_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot_payload(item) for item in value]
    if isinstance(value, set):
        return sorted(snapshot_payload(item) for item in value)
    return value


@dataclass(frozen=True)
class Event:
    type: str
    session_id: str
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    seq: int = field(default_factory=next_event_seq)
    timestamp: str = field(default_factory=now_iso)
    parent_task_id: str | None = None
    run_id: str | None = None
    correlation_id: str | None = None
    command_key: str | None = None

    def __post_init__(self) -> None:
        # Every construction site, not only emit(): create_task, the approval
        # failure batch and the completion batch all build Events directly.
        object.__setattr__(self, "payload", snapshot_payload(self.payload))


class EventStore:
    """In-memory projection of the event log, optionally backed by a durable store.

    Every mutation is serialised by ``_lock``.  The Runtime emits from the TUI
    render thread, from model worker threads and from background sub-agent
    threads, so an unguarded duplicate check followed by an unguarded ``extend``
    would let two threads both pass validation and then interleave their writes.
    """

    def __init__(self, durable: object | None = None) -> None:
        self._events: list[Event] = []
        self._durable = durable
        self._lock = RLock()
        self._by_id: dict[str, Event] = {}
        self._by_seq: dict[int, Event] = {}

    def append(self, event: Event) -> Event:
        self.append_many([event])
        return event

    def append_many(self, events: list[Event]) -> list[Event]:
        with self._lock:
            batch_ids: set[str] = set()
            batch_seqs: set[int] = set()
            for event in events:
                if event.id in batch_ids:
                    raise ValueError("DUPLICATE_EVENT_ID")
                if event.seq in batch_seqs:
                    raise ValueError("DUPLICATE_EVENT_SEQ")
                existing_by_id = self._by_id.get(event.id)
                if existing_by_id is not None:
                    if len(events) == 1 and existing_by_id == event:
                        return events
                    raise ValueError("DUPLICATE_EVENT_ID")
                existing_by_seq = self._by_seq.get(event.seq)
                if existing_by_seq is not None:
                    if len(events) == 1 and existing_by_seq == event:
                        return events
                    raise ValueError("DUPLICATE_EVENT_SEQ")
                batch_ids.add(event.id)
                batch_seqs.add(event.seq)
            new_events = list(events)
            if self._durable is not None and new_events:
                self._persist(new_events)
            self._track(new_events)
            if new_events:
                advance_event_seq(max(event.seq for event in new_events) + 1)
            return events

    def _persist(self, events: list[Event]) -> None:
        durable = self._durable
        append_many = getattr(durable, "append_many", None)
        begin = getattr(durable, "begin", None)
        commit = getattr(durable, "commit", None)
        rollback = getattr(durable, "rollback", None)
        transactional = all(callable(method) for method in (begin, commit, rollback))
        if callable(append_many):
            append_many(events)
        elif transactional:
            begin()
            try:
                for event in events:
                    durable.append(event)
                commit()
            except Exception:
                rollback()
                raise
        else:
            for event in events:
                durable.append(event)

    def _track(self, events: list[Event]) -> None:
        self._events.extend(events)
        for event in events:
            self._by_id[event.id] = event
            self._by_seq[event.seq] = event

    def list(self, session_id: str | None = None) -> list[Event]:
        with self._lock:
            if session_id is None:
                return list(self._events)
            return [event for event in self._events if event.session_id == session_id]

    def load(self, events: list[Event]) -> None:
        """Merge recovered history in, rejecting anything that contradicts it.

        Validation runs to completion before a single event is tracked so a
        conflicting batch leaves the projection untouched.
        """
        with self._lock:
            seen_by_id = dict(self._by_id)
            seen_by_seq = dict(self._by_seq)
            additions: list[Event] = []
            for event in sorted(events, key=lambda item: item.seq):
                prior_id = seen_by_id.get(event.id)
                prior_seq = seen_by_seq.get(event.seq)
                if prior_id is not None and prior_id != event:
                    raise ValueError("CONFLICTING_EVENT_ID")
                if prior_seq is not None and prior_seq != event:
                    raise ValueError("CONFLICTING_EVENT_SEQ")
                if prior_id is None and prior_seq is None:
                    seen_by_id[event.id] = event
                    seen_by_seq[event.seq] = event
                    additions.append(event)
            advance_event_seq(max((event.seq for event in events), default=0) + 1)
            self._track(additions)

    def replay(self, session_id: str) -> list[Event]:
        return sorted(self.list(session_id), key=lambda event: event.seq)
