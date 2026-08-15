from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .events import Event


class SQLiteEventStore:
    """Small durable event store used by a local Fun session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, type TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT, timestamp TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def append(self, event: Event) -> Event:
        self.connection.execute(
            "INSERT OR IGNORE INTO events(seq,id,type,session_id,task_id,timestamp,payload) VALUES(?,?,?,?,?,?,?)",
            (event.seq, event.id, event.type, event.session_id, event.task_id, event.timestamp, json.dumps(event.payload)),
        )
        self.connection.commit()
        return event

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT seq,id,type,session_id,task_id,timestamp,payload FROM events"
        args: tuple[str, ...] = ()
        if session_id:
            query += " WHERE session_id = ?"
            args = (session_id,)
        query += " ORDER BY seq"
        rows = self.connection.execute(query, args).fetchall()
        return [
            {"seq": row[0], "id": row[1], "type": row[2], "session_id": row[3], "task_id": row[4], "timestamp": row[5], "payload": json.loads(row[6])}
            for row in rows
        ]

    def checkpoint(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
