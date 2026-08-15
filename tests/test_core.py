import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.events import Event, EventStore
from fun.policy import PolicyError
from fun.runtime import Runtime
from fun.tools import Tools, file_hash


class CoreTests(unittest.TestCase):
    def test_event_store_is_idempotent(self):
        store = EventStore()
        event = Event("task.created", "ses_1", "task_1")
        store.append(event)
        store.append(event)
        self.assertEqual(store.list(), [event])

    def test_workspace_guard_rejects_escape(self):
        with TemporaryDirectory() as directory:
            tools = Tools(directory)
            with self.assertRaisesRegex(PolicyError, "PATH_OUTSIDE_WORKSPACE"):
                tools.read("../outside.txt")

    def test_explore_and_read(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "hello.txt"
            file.write_text("hello\nworld\n", encoding="utf-8")
            tools = Tools(directory)
            self.assertIn("hello.txt", tools.explore().text)
            self.assertIn("world", tools.read("hello.txt").text)

    def test_runtime_emits_tool_events(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "hello.txt").write_text("hello\n", encoding="utf-8")
            runtime = Runtime(directory)
            runtime.create_task("inspect files")
            result = runtime.run_tool("read", path="hello.txt")
            self.assertTrue(result.ok)
            self.assertEqual(
                [event.type for event in runtime.events.list()],
                ["task.created", "task.started", "tool.requested", "tool.completed"],
            )
            runtime.stop()
            self.assertEqual(runtime.task.status, "stopped")

    def test_protected_file_is_not_read(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text("SECRET=x", encoding="utf-8")
            tools = Tools(directory)
            with self.assertRaisesRegex(PolicyError, "PROTECTED_PATH"):
                tools.read(".env")

    def test_exec_runs_inside_workspace(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec("python3 -c \"print('ok')\"")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "ok")

    def test_checkpoint_and_validation_emit_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("validate")
            result = runtime.validate("python3 -c \"print('pass')\"")
            self.assertTrue(result.ok)
            snapshot = runtime.checkpoint("test")
            self.assertEqual(snapshot["label"], "test")
            self.assertEqual([event.type for event in runtime.events.list()][-4:], ["tool.requested", "tool.completed", "validation.completed", "checkpoint.created"])

    def test_edit_applies_hash_checked_patch_in_auto_mode(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.txt"
            path.write_text("one\ntwo\n", encoding="utf-8")
            runtime = Runtime(directory, "auto")
            runtime.create_task("edit a file")
            result = runtime.run_tool(
                "edit",
                path="a.txt",
                expected_hash=file_hash(path),
                patch="@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n",
            )
            self.assertTrue(result.ok, result.text)
            self.assertEqual(path.read_text(), "one\nTWO\n")


if __name__ == "__main__":
    unittest.main()
