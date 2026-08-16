import threading
import time
import unittest
from tempfile import TemporaryDirectory

from fun.runtime import Runtime


class BackgroundTaskTests(unittest.TestCase):
    def test_spawn_agent_persists_lifecycle_and_result(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("parent")
            done = threading.Event()

            def worker(goal, cancel):
                done.set()
                return goal.upper()

            task = runtime.spawn_agent("inspect", worker)
            self.assertTrue(done.wait(2))
            task.thread.join(2)
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.result, "INSPECT")
            self.assertEqual(task.parent_task_id, runtime.task.id)
            self.assertTrue(task.run_id.startswith("run_"))
            events = runtime.events.list(runtime.session_id)
            types = [event.type for event in events]
            self.assertIn("background.task.created", types)
            self.assertIn("background.task.started", types)
            self.assertIn("background.task.completed", types)
            runtime.stop()

    def test_cancel_background_task_is_cooperative(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("parent")
            started = threading.Event()

            def worker(goal, cancel):
                started.set()
                while not cancel.is_set():
                    time.sleep(0.005)
                return "cancelled"

            task = runtime.spawn_agent("long work", worker)
            self.assertTrue(started.wait(2))
            runtime.cancel_background_task(task.id)
            task.thread.join(2)
            self.assertEqual(task.status, "cancelled")
            self.assertIn("background.task.cancel_requested", [event.type for event in runtime.events.list()])
            runtime.stop()

    def test_close_cancels_background_tasks(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("parent")
            started = threading.Event()

            def worker(goal, cancel):
                started.set()
                while not cancel.is_set():
                    time.sleep(0.005)

            task = runtime.spawn_agent("cleanup", worker)
            self.assertTrue(started.wait(2))
            runtime.close()
            task.thread.join(2)
            self.assertFalse(task.thread.is_alive())
            self.assertEqual(task.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
