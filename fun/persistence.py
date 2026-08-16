from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any

from .events import Event


class SQLiteEventStore:
    """Small durable event store used by a local Fun session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._lock = Lock()
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, type TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT, timestamp TEXT NOT NULL, payload TEXT NOT NULL, parent_task_id TEXT, run_id TEXT, correlation_id TEXT, command_key TEXT)"
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(events)")}
        for name in ("parent_task_id", "run_id", "correlation_id", "command_key"):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_events_command ON events(command_key)")
        self.connection.commit()

    def begin(self) -> None:
        self.connection.execute("BEGIN")

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def append(self, event: Event) -> Event:
        self.append_many([event])
        return event

    def append_many(self, events: list[Event]) -> list[Event]:
        with self._lock:
            return self._append_many(events)

    def _append_many(self, events: list[Event]) -> list[Event]:
        try:
            for event in events:
                payload = json.dumps(event.payload)
                existing = self.connection.execute(
                    "SELECT seq,id,type,session_id,task_id,timestamp,payload,parent_task_id,run_id,correlation_id,command_key FROM events WHERE id = ?",
                    (event.id,),
                ).fetchone()
                if existing is not None:
                    if existing == (event.seq, event.id, event.type, event.session_id, event.task_id, event.timestamp, payload, event.parent_task_id, event.run_id, event.correlation_id, event.command_key):
                        continue
                    raise sqlite3.IntegrityError("UNIQUE constraint failed: events.id")
                self.connection.execute(
                    "INSERT INTO events(seq,id,type,session_id,task_id,timestamp,payload,parent_task_id,run_id,correlation_id,command_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event.seq, event.id, event.type, event.session_id, event.task_id, event.timestamp, payload, event.parent_task_id, event.run_id, event.correlation_id, event.command_key),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return events

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT seq,id,type,session_id,task_id,timestamp,payload,parent_task_id,run_id,correlation_id,command_key FROM events"
        args: tuple[str, ...] = ()
        if session_id:
            query += " WHERE session_id = ?"
            args = (session_id,)
        query += " ORDER BY seq"
        rows = self.connection.execute(query, args).fetchall()
        return [
            {"seq": row[0], "id": row[1], "type": row[2], "session_id": row[3], "task_id": row[4], "timestamp": row[5], "payload": json.loads(row[6]), "parent_task_id": row[7], "run_id": row[8], "correlation_id": row[9], "command_key": row[10]}
            for row in rows
        ]

    def events(self, session_id: str | None = None) -> list[Event]:
        rows = self.list(session_id)
        return [Event(row["type"], row["session_id"], row["task_id"], row["payload"], row["id"], row["seq"], row["timestamp"], row["parent_task_id"], row["run_id"], row["correlation_id"], row["command_key"]) for row in rows]

    def checkpoint(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
