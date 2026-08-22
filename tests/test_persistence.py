import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path

from fun.events import Event
from fun.persistence import SQLiteEventStore


def _process_writer(path: str, index: int, ready, result) -> None:
    store = SQLiteEventStore(path)
    ready.wait()
    try:
        store.append(Event("process", "ses_process", id=f"evt_process_{index}", seq=index + 1))
        result.put(None)
    except Exception as exc:
        result.put(type(exc).__name__ + ": " + str(exc))
    finally:
        store.close()


class PersistenceTests(unittest.TestCase):
    def test_sqlite_concurrent_processes_persist_all_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.db")
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            result = context.Queue()
            processes = [context.Process(target=_process_writer, args=(path, index, ready, result)) for index in range(8)]
            for process in processes:
                process.start()
            ready.set()
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([result.get(timeout=2) for _ in processes], [None] * len(processes))
            store = SQLiteEventStore(path)
            self.assertEqual(len(store.list("ses_process")), len(processes))
            store.close()

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

    def test_sqlite_multiple_connections_preserve_batch_atomicity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            first = SQLiteEventStore(path)
            second = SQLiteEventStore(path)
            first.append(Event("existing", "ses_1", id="evt_existing", seq=1))
            with self.assertRaisesRegex(Exception, "UNIQUE"):
                second.append_many([
                    Event("first", "ses_1", id="evt_first", seq=2),
                    Event("conflict", "ses_1", id="evt_existing", seq=3),
                ])
            self.assertEqual([row["id"] for row in first.list("ses_1")], ["evt_existing"])
            first.close()
            second.close()

    def test_sqlite_concurrent_connections_persist_all_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            errors = []

            def writer(index: int) -> None:
                store = SQLiteEventStore(path)
                try:
                    store.append(Event("concurrent", "ses_concurrent", id=f"evt_{index}", seq=index + 1))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    store.close()

            threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            store = SQLiteEventStore(path)
            self.assertEqual(len(store.list("ses_concurrent")), 12)
            store.close()

    def test_sqlite_event_store_supports_multiple_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            first = SQLiteEventStore(path)
            second = SQLiteEventStore(path)
            first.append(Event("first", "ses_1", id="evt_first", seq=1))
            second.append(Event("second", "ses_1", id="evt_second", seq=2))
            self.assertEqual(len(first.list("ses_1")), 2)
            self.assertEqual(len(second.list("ses_1")), 2)
            first.close()
            second.close()

    def test_sqlite_event_store_configures_busy_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            value = store.connection.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(value, 30000)
            store.close()

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

    def test_a_seq_conflict_is_renumbered_rather_than_losing_the_event(self):
        """seq is allocated by a process-global counter starting at 1, so two
        processes on one events.db collide.  Losing the batch was the wrong
        answer: the events are valid, only their numbering was hopeful."""
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            store.append(Event("task.created", "ses_1", "task_1", id="evt_one", seq=7))
            store.append(Event("plan.created", "ses_1", "task_1", id="evt_two", seq=7))
            rows = store.list("ses_1")
            self.assertEqual([row["id"] for row in rows], ["evt_one", "evt_two"])
            self.assertEqual(len({row["seq"] for row in rows}), 2)
            store.close()

    def test_a_duplicate_id_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            store.append(Event("task.created", "ses_1", "task_1", {"a": 1}, id="evt_one", seq=7))
            with self.assertRaisesRegex(Exception, "UNIQUE"):
                store.append(Event("plan.created", "ses_1", "task_1", {"b": 2}, id="evt_one", seq=8))
            self.assertEqual(len(store.list("ses_1")), 1)
            store.close()

    def test_concurrent_writers_do_not_lose_events(self):
        import subprocess
        import sys

        program = (
            "import sys; sys.path.insert(0, %r)\n"
            "from fun.persistence import SQLiteEventStore\n"
            "from fun.events import Event, EventStore\n"
            "store = EventStore(SQLiteEventStore(sys.argv[1] + '/events.db'))\n"
            "[store.append(Event('t', 's' + sys.argv[2], None, {'i': i})) for i in range(30)]\n"
        ) % str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as directory:
            processes = [subprocess.Popen([sys.executable, "-c", program, directory, str(index)], stderr=subprocess.PIPE, text=True) for index in range(4)]
            errors = [process.communicate()[1] for process in processes]
            self.assertEqual([error for error in errors if error.strip()], [])
            store = SQLiteEventStore(Path(directory) / "events.db")
            rows = store.list()
            self.assertEqual(len(rows), 120)
            self.assertEqual(len({row["seq"] for row in rows}), 120)
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


class ConcurrencyRegressionTests(unittest.TestCase):
    def test_concurrent_appends_do_not_lose_or_duplicate_events(self):
        """The Runtime emits from worker and background threads at once."""
        from fun.events import EventStore

        store = EventStore()
        errors: list[Exception] = []

        def emit(count: int) -> None:
            try:
                for _ in range(count):
                    store.append(Event("threaded", "ses_1"))
            except Exception as exc:  # pragma: no cover - only on regression
                errors.append(exc)

        threads = [threading.Thread(target=emit, args=(40,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        events = store.list("ses_1")
        self.assertEqual(len(events), 320)
        self.assertEqual(len({event.id for event in events}), 320)
        self.assertEqual(len({event.seq for event in events}), 320)

    def test_concurrent_appends_stay_consistent_with_a_durable_store(self):
        from fun.events import EventStore

        with tempfile.TemporaryDirectory() as directory:
            durable = SQLiteEventStore(Path(directory) / "events.db")
            store = EventStore(durable)
            errors: list[Exception] = []

            def emit() -> None:
                try:
                    for _ in range(25):
                        store.append(Event("threaded", "ses_1"))
                except Exception as exc:  # pragma: no cover - only on regression
                    errors.append(exc)

            threads = [threading.Thread(target=emit) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(store.list("ses_1")), 150)
            self.assertEqual(len(durable.events("ses_1")), 150)
            durable.close()

    def test_load_rejecting_a_batch_leaves_the_projection_usable(self):
        from fun.events import EventStore

        store = EventStore()
        store.load([Event("original", "ses_1", id="evt_a", seq=5)])
        with self.assertRaises(ValueError):
            store.load([Event("changed", "ses_1", id="evt_a", seq=6)])
        self.assertEqual(len(store.list()), 1)
        store.append(Event("after", "ses_1"))
        self.assertEqual(len(store.list()), 2)
