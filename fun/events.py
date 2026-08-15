from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


_next_event_seq = 1


def next_event_seq() -> int:
    global _next_event_seq
    seq = _next_event_seq
    _next_event_seq += 1
    return seq


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


class EventStore:
    def __init__(self, durable: object | None = None) -> None:
        self._events: list[Event] = []
        self._durable = durable

    def append(self, event: Event) -> Event:
        self.append_many([event])
        return event

    def append_many(self, events: list[Event]) -> list[Event]:
        existing_ids = {item.id for item in self._events}
        batch_ids: set[str] = set()
        for event in events:
            if event.id in batch_ids:
                raise ValueError("DUPLICATE_EVENT_ID")
            if event.id in existing_ids:
                if len(events) == 1:
                    return events
                raise ValueError("DUPLICATE_EVENT_ID")
            batch_ids.add(event.id)
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
        ordered = sorted(events, key=lambda item: item.seq)
        highest_seq = max((event.seq for event in ordered), default=0)
        _next_event_seq = max(_next_event_seq, highest_seq + 1)
        for event in ordered:
            if not any(item.id == event.id for item in self._events):
                self._events.append(event)

    def replay(self, session_id: str) -> list[Event]:
        return sorted(self.list(session_id), key=lambda event: event.seq)
