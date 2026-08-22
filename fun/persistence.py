from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any

from dataclasses import replace

from .events import Event, advance_event_seq


class SQLiteEventStore:
    """Small durable event store used by a local Fun session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._lock = Lock()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
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
        except Exception:
            self.connection.rollback()
            self.connection.close()
            raise
        row = self.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        advance_event_seq(int(row[0]) + 1)

    def begin(self) -> None:
        self.connection.execute("BEGIN")

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def append(self, event: Event) -> Event:
        self.append_many([event])
        return event

    MAX_SEQ_ATTEMPTS = 6

    def append_many(self, events: list[Event]) -> list[Event]:
        """Append a batch, claiming sequence numbers no other writer holds.

        The whole read-max-then-insert runs inside ``BEGIN IMMEDIATE`` so SQLite
        serialises writers across processes; without it two processes read the
        same maximum and the loser's batch is rolled back on a primary-key
        collision.  The retry covers the remaining case where another process
        wrote between our transaction ending and the next one starting.
        """
        with self._lock:
            last: Exception | None = None
            for _ in range(self.MAX_SEQ_ATTEMPTS):
                try:
                    self.connection.execute("BEGIN IMMEDIATE")
                    try:
                        return self._append_many(events)
                    except sqlite3.IntegrityError as exc:
                        if "events.id" in str(exc):
                            raise
                        last = exc
                        events[:] = self._renumber(events)
                except sqlite3.OperationalError as exc:
                    self.connection.rollback()
                    last = exc
            raise last if last is not None else RuntimeError("EVENT_APPEND_FAILED")

    def _renumber(self, events: list[Event]) -> list[Event]:
        """Re-issue seq numbers from what is actually on disk.

        ``seq`` is this table's primary key, but it is handed out by a
        *process-global* counter that starts at 1.  Two processes writing one
        events.db — which two workspaces sharing a state dir now do — both start
        at 1 and collide, and the loser's whole batch is rolled back.  Reading
        the real maximum inside the write lock and renumbering is what makes the
        allocation authoritative rather than hopeful.
        """
        self.connection.rollback()
        row = self.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        next_seq = int(row[0]) + 1
        advance_event_seq(next_seq + len(events))
        renumbered = []
        for offset, event in enumerate(events):
            renumbered.append(replace(event, seq=next_seq + offset))
        return renumbered

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
        """Read the log.

        Under the same lock as the writer: the connection is shared across
        threads (``check_same_thread=False``) and ``_append_many`` is a
        multi-statement transaction, so an unlocked reader could interleave with
        it and observe rows that were about to be rolled back.
        """
        query = "SELECT seq,id,type,session_id,task_id,timestamp,payload,parent_task_id,run_id,correlation_id,command_key FROM events"
        args: tuple[str, ...] = ()
        if session_id:
            query += " WHERE session_id = ?"
            args = (session_id,)
        query += " ORDER BY seq"
        with self._lock:
            rows = self.connection.execute(query, args).fetchall()
        return [
            {"seq": row[0], "id": row[1], "type": row[2], "session_id": row[3], "task_id": row[4], "timestamp": row[5], "payload": json.loads(row[6]), "parent_task_id": row[7], "run_id": row[8], "correlation_id": row[9], "command_key": row[10]}
            for row in rows
        ]

    def events(self, session_id: str | None = None) -> list[Event]:
        rows = self.list(session_id)
        return [Event(row["type"], row["session_id"], row["task_id"], row["payload"], row["id"], row["seq"], row["timestamp"], row["parent_task_id"], row["run_id"], row["correlation_id"], row["command_key"]) for row in rows]

    def checkpoint(self) -> None:
        with self._lock:
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "SQLiteEventStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
