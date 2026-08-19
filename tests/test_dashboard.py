import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
            with SQLiteEventStore(Path(directory) / "events.db") as store:
                store.append(Event("task.created", "s1", "t1", {"goal": "test"}))
                store.append(Event("model.completed", "s1", "t1", {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}))
                store.append(Event("task.created", "s2", "t2", {"goal": "second"}))
                store.append(Event("model.completed", "s2", "t2", {"usage": {"prompt_tokens": 20, "completion_tokens": 7}}))
                store.append(Event("model.tool_call", "s1", "t1", {"name": "read"}))
                store.append(Event("task.completed", "s1", "t1", {}))
                store.append(Event("task.failed", "s2", "t2", {"reason": "test"}))
            snapshot = DashboardData(Path(directory) / "events.db").snapshot()
            self.assertEqual(snapshot["sessions"], 2)
            self.assertEqual(snapshot["tasks"], 2)
            self.assertEqual(snapshot["input_tokens"], 30)
            self.assertEqual(snapshot["output_tokens"], 12)
            self.assertEqual(snapshot["total_tokens"], 42)
            self.assertEqual(snapshot["failed"], 1)
            self.assertEqual(snapshot["stopped"], 0)
            self.assertEqual(snapshot["recent"][0]["type"], "task.failed")
            self.assertEqual(snapshot["session_usage"][0]["session_id"], "s2")
            self.assertEqual(snapshot["session_usage"][0]["total_tokens"], 27)
            self.assertEqual(snapshot["session_usage"][1]["tool_calls"], 1)
            self.assertEqual(snapshot["tool_calls"], 1)
            self.assertEqual(snapshot["completed"], 1)
            store.close()

    def test_dashboard_uses_background_task_id_from_runtime_events(self):
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteEventStore(Path(directory) / "events.db") as store:
                store.append(Event("background.task.created", "s1", "parent_1", {"background_task_id": "bg_1", "goal": "scan", "kind": "subagent", "parent_task_id": "parent_1", "run_id": "run_1"}))
                store.append(Event("background.task.started", "s1", "parent_1", {"background_task_id": "bg_1", "kind": "subagent", "parent_task_id": "parent_1", "run_id": "run_1"}))
                store.append(Event("background.task.completed", "s1", "parent_1", {"background_task_id": "bg_1", "kind": "subagent", "result": "done"}))
            tasks = DashboardData(Path(directory) / "events.db").snapshot()["background_tasks"]
            self.assertEqual(tasks[0]["task_id"], "bg_1")
            self.assertEqual(tasks[0]["status"], "completed")

    def test_token_totals_are_snapshots_not_a_running_sum(self):
        """model.completed carries the session cumulative total, so adding them
        up grew the numbers triangularly: ten turns of 100 reported 5 500."""
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteEventStore(Path(directory) / "events.db") as store:
                for turn in range(1, 4):
                    store.append(Event("model.completed", "s1", "t1", {"usage": {"input_tokens": 100 * turn, "output_tokens": 50 * turn, "total_tokens": 150 * turn}}))
            snapshot = DashboardData(Path(directory) / "events.db").snapshot()
            self.assertEqual(snapshot["input_tokens"], 300)
            self.assertEqual(snapshot["output_tokens"], 150)
            self.assertEqual(snapshot["total_tokens"], 450)

    def test_the_dashboard_agrees_with_the_runtime_it_reports_on(self):
        from fun.runtime import Runtime

        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("usage")
            for _ in range(4):
                runtime.usage.merge_provider({"prompt_tokens": 100, "completion_tokens": 50})
                runtime.emit("model.completed", runtime.task.id, usage=runtime.usage.as_dict())
            live = runtime.usage.total_tokens
            runtime.stop()
            snapshot = DashboardData(Path(directory) / "events.db").snapshot()
            self.assertEqual(snapshot["total_tokens"], live)

    def test_a_malformed_row_does_not_take_the_endpoint_down(self):
        """Each of these has been seen in a real events.db; one used to 500 the
        whole page until the row was deleted by hand."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            with SQLiteEventStore(path) as store:
                store.append(Event("model.completed", "s1", "t1", {"usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}))
            connection = sqlite3.connect(path)
            # NOT NULL on payload, so a NULL row cannot be written here; the
            # other three shapes are what a provider or an older writer produces.
            for seq, payload in ((900, '{"usage": null}'), (901, "[1,2]"), (903, '"just a string"'), (905, "not json at all")):
                connection.execute(
                    "INSERT INTO events(seq,id,type,session_id,task_id,timestamp,payload) VALUES(?,?,?,?,?,?,?)",
                    (seq, f"bad-{seq}", "model.completed", "s1", "t1", "2026-01-01T00:00:00Z", payload),
                )
            connection.commit()
            connection.close()
            snapshot = DashboardData(path).snapshot()
            self.assertEqual(snapshot["sessions"], 1)

    def test_a_zero_byte_database_is_a_first_run_not_a_500(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            path.write_bytes(b"")
            self.assertEqual(DashboardData(path).snapshot()["sessions"], 0)

    def test_the_page_reports_a_key_that_failed_to_store_as_not_configured(self):
        """api_key_env is written exactly when durable storage FAILED, so
        treating it as evidence showed "ready to run" with no credential."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            (Path(directory) / "config.json").write_text(
                json.dumps({"base_url": "https://x/v1", "model": "m", "api_key_env": "FUN_API_KEY"}), encoding="utf-8"
            )
            with patch.dict("os.environ", {"FUN_API_KEY": ""}, clear=False), patch("fun.config._keychain_get", return_value=""):
                setup = DashboardData(path).snapshot()["setup"]
            self.assertFalse(setup["configured"])
            self.assertTrue(setup["needs_env"])

    def test_an_unreadable_keychain_is_not_reported_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            (Path(directory) / "config.json").write_text(
                json.dumps({"base_url": "https://x/v1", "model": "m", "api_key_store": "macos-keychain"}), encoding="utf-8"
            )
            with patch.dict("os.environ", {"FUN_API_KEY": ""}, clear=False), patch("fun.config._keychain_get", return_value=""):
                setup = DashboardData(path).snapshot()["setup"]
            self.assertFalse(setup["configured"])
            self.assertTrue(setup["needs_env"])
            self.assertTrue(setup["keychain_unreadable"])

    def test_a_readable_keychain_is_reported_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            (Path(directory) / "config.json").write_text(
                json.dumps({"base_url": "https://x/v1", "model": "m", "api_key_store": "macos-keychain"}), encoding="utf-8"
            )
            with patch.dict("os.environ", {"FUN_API_KEY": ""}, clear=False), patch("fun.config._keychain_get", return_value="sk-live"):
                setup = DashboardData(path).snapshot()["setup"]
            self.assertTrue(setup["configured"])
            self.assertFalse(setup["keychain_unreadable"])

    def test_a_request_that_did_not_address_localhost_is_refused(self):
        """The DNS-rebinding guard had no test: deleting it broke nothing while
        any page the user visits could read this origin."""
        import threading
        import urllib.error
        import urllib.request

        from fun.dashboard import serve

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            with SQLiteEventStore(path) as store:
                store.append(Event("task.created", "s1", "t1", {"goal": "hi"}))
            port = 8899
            thread = threading.Thread(target=serve, args=(path, port), daemon=True)
            thread.start()
            time.sleep(0.4)
            allowed = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary", timeout=3)
            self.assertEqual(allowed.status, 200)
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/summary", headers={"Host": "evil.example.com"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
