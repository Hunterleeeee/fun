from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from typing import Any


_seq = count(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    type: str
    session_id: str
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"evt_{next(_seq)}")
    seq: int = field(default_factory=lambda: next(_seq))
    timestamp: str = field(default_factory=now_iso)


class EventStore:
    def __init__(self, durable: object | None = None) -> None:
        self._events: list[Event] = []
        self._durable = durable

    def append(self, event: Event) -> Event:
        if any(item.id == event.id for item in self._events):
            return event
        self._events.append(event)
        if self._durable is not None:
            self._durable.append(event)
        return event

    def list(self, session_id: str | None = None) -> list[Event]:
        if session_id is None:
            return list(self._events)
        return [event for event in self._events if event.session_id == session_id]

    def replay(self, session_id: str) -> list[Event]:
        return sorted(self.list(session_id), key=lambda event: event.seq)
