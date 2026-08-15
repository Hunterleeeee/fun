import tempfile
import unittest
from pathlib import Path

from fun.dashboard import DashboardData
from fun.events import Event
from fun.persistence import SQLiteEventStore


class DashboardTests(unittest.TestCase):
    def test_empty_dashboard_has_zero_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = DashboardData(Path(directory) / "events.db").snapshot()
            self.assertEqual(snapshot["sessions"], 0)
            self.assertEqual(snapshot["total_tokens"], 0)

    def test_dashboard_aggregates_local_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / "events.db")
            store.append(Event("task.created", "s1", "t1", {"goal": "test"}))
            store.append(Event("model.completed", "s1", "t1", {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}))
            store.append(Event("model.tool_call", "s1", "t1", {"name": "read"}))
            store.append(Event("task.completed", "s1", "t1", {}))
            snapshot = DashboardData(Path(directory) / "events.db").snapshot()
            self.assertEqual(snapshot["sessions"], 1)
            self.assertEqual(snapshot["tasks"], 1)
            self.assertEqual(snapshot["total_tokens"], 15)
            self.assertEqual(snapshot["tool_calls"], 1)
            self.assertEqual(snapshot["completed"], 1)


if __name__ == "__main__":
    unittest.main()
