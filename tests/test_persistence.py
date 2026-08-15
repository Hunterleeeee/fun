import tempfile
import unittest
from pathlib import Path

from fun.events import Event
from fun.persistence import SQLiteEventStore


class PersistenceTests(unittest.TestCase):
    def test_sqlite_event_store_batch_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            events = [Event("task.created", "ses_1", "task_1"), Event("plan.created", "ses_1", "task_1")]
            store.append_many(events)
            self.assertEqual(len(store.list("ses_1")), 2)
            store.close()

    def test_sqlite_event_store_rejects_id_conflict_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            first = Event("task.created", "ses_1", "task_1", id="evt_same", seq=1)
            store.append(first)
            conflicting = Event("plan.created", "ses_1", "task_1", id="evt_same", seq=2)
            with self.assertRaisesRegex(Exception, "UNIQUE"):
                store.append(conflicting)
            rows = store.list("ses_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["type"], "task.created")
            store.close()

    def test_sqlite_event_store_rejects_seq_conflict_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            first = Event("task.created", "ses_1", "task_1", id="evt_one", seq=7)
            store.append(first)
            conflicting = Event("plan.created", "ses_1", "task_1", id="evt_two", seq=7)
            with self.assertRaisesRegex(Exception, "UNIQUE"):
                store.append(conflicting)
            rows = store.list("ses_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "evt_one")
            store.close()

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
