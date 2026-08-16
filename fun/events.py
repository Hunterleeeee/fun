from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
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


class EventStore:
    def __init__(self, durable: object | None = None) -> None:
        self._events: list[Event] = []
        self._durable = durable

    def append(self, event: Event) -> Event:
        self.append_many([event])
        return event

    def append_many(self, events: list[Event]) -> list[Event]:
        existing_ids = {item.id: item for item in self._events}
        existing_seqs = {item.seq: item for item in self._events}
        batch_ids: set[str] = set()
        batch_seqs: set[int] = set()
        for event in events:
            if event.id in batch_ids:
                raise ValueError("DUPLICATE_EVENT_ID")
            if event.seq in batch_seqs:
                raise ValueError("DUPLICATE_EVENT_SEQ")
            if event.id in existing_ids:
                if len(events) == 1 and existing_ids[event.id] == event:
                    return events
                raise ValueError("DUPLICATE_EVENT_ID")
            if event.seq in existing_seqs:
                if len(events) == 1 and existing_seqs[event.seq] == event:
                    return events
                raise ValueError("DUPLICATE_EVENT_SEQ")
            batch_ids.add(event.id)
            batch_seqs.add(event.seq)
        new_events = list(events)
        if self._durable is not None and new_events:
            durable = self._durable
            append_many = getattr(durable, "append_many", None)
            begin = getattr(durable, "begin", None)
            commit = getattr(durable, "commit", None)
            rollback = getattr(durable, "rollback", None)
            transactional = all(callable(method) for method in (begin, commit, rollback))
            if callable(append_many):
                append_many(new_events)
            elif transactional:
                begin()
                try:
                    for event in new_events:
                        durable.append(event)
                    commit()
                except Exception:
                    rollback()
                    raise
            else:
                for event in new_events:
                    durable.append(event)
        self._events.extend(new_events)
        return events

    def list(self, session_id: str | None = None) -> list[Event]:
        if session_id is None:
            return list(self._events)
        return [event for event in self._events if event.session_id == session_id]

    def load(self, events: list[Event]) -> None:
        global _next_event_seq
        existing_by_id = {item.id: item for item in self._events}
        existing_by_seq = {item.seq: item for item in self._events}
        additions: list[Event] = []
        for event in sorted(events, key=lambda item: item.seq):
            prior_id = existing_by_id.get(event.id)
            prior_seq = existing_by_seq.get(event.seq)
            if prior_id is not None and prior_id != event:
                raise ValueError("CONFLICTING_EVENT_ID")
            if prior_seq is not None and prior_seq != event:
                raise ValueError("CONFLICTING_EVENT_SEQ")
            if prior_id is None and prior_seq is None:
                existing_by_id[event.id] = event
                existing_by_seq[event.seq] = event
                additions.append(event)
        highest_seq = max((event.seq for event in events), default=0)
        with _event_seq_lock:
            _next_event_seq = max(_next_event_seq, highest_seq + 1)
        self._events.extend(additions)

    def replay(self, session_id: str) -> list[Event]:
        return sorted(self.list(session_id), key=lambda event: event.seq)
