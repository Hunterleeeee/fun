import tempfile
import unittest
from pathlib import Path

from fun.events import Event
from fun.persistence import SQLiteEventStore


class PersistenceTests(unittest.TestCase):
    def test_event_store_load_rejects_batch_seq_conflict_without_mutation(self):
        from fun.events import EventStore

        store = EventStore()
        original = Event("original", "ses_1", id="evt_original", seq=12)
        store.load([original])
        batch = [
            Event("first", "ses_1", id="evt_first", seq=20),
            Event("second", "ses_1", id="evt_second", seq=20),
        ]
        with self.assertRaisesRegex(ValueError, "CONFLICTING_EVENT_SEQ"):
            store.load(batch)
        self.assertEqual(store.list(), [original])
        fresh = Event("fresh", "ses_1")
        store.append(fresh)
        self.assertGreater(fresh.seq, 20)

    def test_event_store_load_rejects_conflicts_without_mutation(self):
        from fun.events import EventStore

        store = EventStore()
        original = Event("original", "ses_1", id="evt_original", seq=12)
        store.load([original])
        conflict = Event("changed", "ses_1", id="evt_original", seq=13)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_EVENT_ID"):
            store.load([conflict])
        self.assertEqual(store.list(), [original])

    def test_event_store_load_is_idempotent_for_duplicate_history(self):
        from fun.events import EventStore

        historical = Event("task.created", "ses_1", "task_1", id="evt_history", seq=9)
        store = EventStore()
        store.load([historical, historical])
        self.assertEqual(store.list(), [historical])
        fresh = Event("task.started", "ses_1", "task_1")
        store.append(fresh)
        self.assertGreater(fresh.seq, 9)

    def test_event_store_load_does_not_rewind_sequence(self):
        from fun.events import EventStore

        store = EventStore()
        current = Event("current", "ses_1", seq=500)
        store.append(current)
        store.load([Event("old", "ses_1", id="evt_old", seq=3)])
        fresh = Event("fresh", "ses_1")
        store.append(fresh)
        self.assertGreater(fresh.seq, 500)

    def test_event_store_load_advances_sequence_after_recovery(self):
        from fun.events import EventStore

        historical = [
            Event("task.created", "ses_1", "task_1", id="evt_old_1", seq=41),
            Event("plan.created", "ses_1", "task_1", id="evt_old_2", seq=42),
        ]
        store = EventStore()
        store.load(historical)
        fresh = Event("task.started", "ses_1", "task_1")
        store.append(fresh)
        self.assertGreater(fresh.seq, 42)
        self.assertEqual([event.seq for event in store.replay("ses_1")], [41, 42, fresh.seq])

    def test_sqlite_event_store_batch_conflict_rolls_back_all_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            existing = Event("existing", "ses_1", id="evt_existing", seq=1)
            store.append(existing)
            first = Event("first", "ses_1", id="evt_first", seq=2)
            conflicting = Event("conflict", "ses_1", id="evt_existing", seq=3)
            with self.assertRaisesRegex(Exception, "UNIQUE"):
                store.append_many([first, conflicting])
            rows = store.list("ses_1")
            self.assertEqual([row["id"] for row in rows], ["evt_existing"])
            store.close()

    def test_sqlite_event_store_batch_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            events = [Event("task.created", "ses_1", "task_1"), Event("plan.created", "ses_1", "task_1")]
            store.append_many(events)
            self.assertEqual(len(store.list("ses_1")), 2)
            store.close()

    def test_sqlite_event_store_duplicate_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            event = Event("task.created", "ses_1", "task_1", {"goal": "same"}, id="evt_same", seq=1)
            store.append(event)
            store.append(event)
            self.assertEqual(len(store.list("ses_1")), 1)
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
