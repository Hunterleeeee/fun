import tempfile
import unittest
from pathlib import Path

from fun.events import Event
from fun.persistence import SQLiteEventStore


class PersistenceTests(unittest.TestCase):
    def test_sqlite_event_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            event = Event("task.created", "ses_1", "task_1", {"goal": "test"})
            store.append(event)
            store.append(event)
            rows = store.list("ses_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["goal"], "test")
            store.close()


if __name__ == "__main__":
    unittest.main()
