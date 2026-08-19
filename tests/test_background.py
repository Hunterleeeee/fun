import json
from pathlib import Path
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

    def test_background_status_has_safe_owned_fields(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("parent")
            task = runtime.spawn_agent("status check", lambda goal, cancel: "done")
            task.thread.join(2)
            status = [(item.id, item.status, item.goal, item.result, item.error) for item in runtime.background.list()]
            self.assertEqual(status[0][1:], ("completed", "status check", "done", None))
            runtime.stop()

    def test_cancel_unknown_background_task_is_safe(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("parent")
            with self.assertRaisesRegex(RuntimeError, "BACKGROUND_TASK_NOT_FOUND"):
                runtime.cancel_background_task("bg_missing")
            runtime.stop()

    def test_failed_background_task_exposes_safe_error_name(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("parent")
            def worker(goal, cancel):
                raise RuntimeError("private secret must not be shown")
            task = runtime.spawn_agent("failed work", worker)
            task.thread.join(2)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.error, "RuntimeError")
            self.assertNotIn("private secret", str(task.error))
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

    def test_background_task_can_be_cancelled_after_foreground_turn_closed_store(self):
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
            runtime.complete("foreground done")
            runtime.cancel_background_task(task.id)
            task.thread.join(2)
            self.assertEqual(task.status, "cancelled")
            self.assertIn("background.task.cancel_requested", [event.type for event in runtime.events.list()])
            runtime.shutdown()

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
            # shutdown(), not close(): a turn ending is not the session ending,
            # and close() no longer takes live sub-agents down with it.
            runtime.shutdown()
            task.thread.join(2)
            self.assertFalse(task.thread.is_alive())
            self.assertEqual(task.status, "cancelled")


if __name__ == "__main__":
    unittest.main()


class ResearchSubAgentTests(unittest.TestCase):
    """Sub-agents were unreachable: no caller, no tool schema, no command —
    while the rail, /cancel and the dashboard all reported on them."""

    def _workspace(self):
        directory = TemporaryDirectory()
        Path(directory.name, "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        return directory

    def _provider(self, script):
        class Scripted:
            def __init__(self):
                self.calls = 0
                self.tools_offered = []

            def stream(self, messages, tools=None):
                self.tools_offered.append([item["function"]["name"] for item in (tools or [])])
                self.calls += 1
                yield from script(self.calls)

        return Scripted()

    def test_a_sub_agent_reads_the_workspace_and_answers(self):
        def script(call):
            if call == 1:
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}}]}}]}
            else:
                yield {"choices": [{"delta": {"content": "a.py defines f()"}}]}

        with self._workspace() as directory:
            provider = self._provider(script)
            runtime = Runtime(directory, "auto", provider=provider, state_dir=directory)
            runtime.create_task("main")
            task = runtime.spawn_research("what is in a.py")
            task.thread.join(5)
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.result, "a.py defines f()")
            self.assertEqual(provider.tools_offered[0], ["explore", "read"])
            types = [event.type for event in runtime.events.list() if event.type.startswith("background")]
            self.assertEqual(types, ["background.task.created", "background.task.started", "background.task.completed"])
            runtime.stop()

    def test_a_sub_agent_cannot_write_even_when_the_model_asks(self):
        """The boundary is Policy, not the prompt or the tool list."""
        def script(call):
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "edit", "arguments": json.dumps({"path": "a.py", "expected_hash": "x", "patch": "y"})}}]}}]}
            yield {"choices": [{"delta": {"content": "done"}}]}

        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", provider=self._provider(script), state_dir=directory)
            runtime.create_task("main")
            task = runtime.spawn_research("change something")
            task.thread.join(5)
            self.assertEqual(Path(directory, "a.py").read_text(encoding="utf-8"), "def f():\n    return 1\n")
            runtime.stop()

    def test_a_sub_agent_cannot_exec_even_when_the_model_asks(self):
        marker = "pwned.txt"

        def script(call):
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "exec", "arguments": json.dumps({"command": f"touch {marker}"})}}]}}]}
            yield {"choices": [{"delta": {"content": "done"}}]}

        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", provider=self._provider(script), state_dir=directory)
            runtime.create_task("main")
            task = runtime.spawn_research("run something")
            task.thread.join(5)
            self.assertFalse(Path(directory, marker).exists())
            runtime.stop()

    def test_a_sub_agent_failure_does_not_reach_the_session(self):
        def script(call):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", provider=self._provider(script), state_dir=directory)
            runtime.create_task("main")
            task = runtime.spawn_research("anything")
            task.thread.join(5)
            self.assertEqual(task.status, "failed")
            self.assertEqual(runtime.task.status, "running")
            runtime.stop()

    def test_cancelling_stops_the_loop(self):
        def script(call):
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"c{call}", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}}]}}]}

        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", provider=self._provider(script), state_dir=directory)
            runtime.create_task("main")
            task = runtime.spawn_research("loop forever")
            runtime.cancel_background_task(task.id)
            task.thread.join(5)
            self.assertIn(task.status, {"cancelled", "completed"})
            runtime.stop()

    def test_spawning_without_a_provider_is_refused(self):
        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("main")
            with self.assertRaises(RuntimeError):
                runtime.spawn_research("anything")
            runtime.stop()

    def test_the_command_reaches_the_runtime(self):
        from fun.commands import Session, dispatch
        from fun.config import FunConfig

        def script(call):
            yield {"choices": [{"delta": {"content": "the answer"}}]}

        class Frontend:
            locale = "en-US"

            def __init__(self):
                self.said: list[str] = []
                self.notified: list[str] = []

            def say(self, text): self.said.append(text)
            def notify(self, text): self.notified.append(text)
            def status(self, text): pass
            def clear(self): pass

        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", provider=self._provider(script), state_dir=directory)
            runtime.create_task("main")
            session = Session(runtime, FunConfig(), f"{directory}/config.json")
            frontend = Frontend()
            self.assertTrue(dispatch("/agent what is in a.py", session, frontend))
            self.assertTrue(frontend.notified)
            for task in runtime.background.list():
                task.thread.join(5)
            self.assertEqual([task.result for task in runtime.background.list()], ["the answer"])
            dispatch("/agent", session, frontend)
            self.assertIn("Usage: /agent", frontend.said[-1])
            runtime.stop()

    def test_a_turn_ending_does_not_kill_a_running_sub_agent(self):
        """close() runs after every turn; taking sub-agents with it meant
        /agent had no window in which it could ever finish."""
        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", provider=self._provider(lambda call: iter([{"choices": [{"delta": {"content": "answer"}}]}])), state_dir=directory)
            runtime.create_task("main")
            slow = threading.Event()

            def worker(goal, cancel):
                slow.wait(1.0)
                return "answer"

            task = runtime.spawn_agent("question", worker)
            runtime.complete("turn over")
            slow.set()
            task.thread.join(5)
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.result, "answer")
            runtime.shutdown()

    def test_shutdown_does_cancel_sub_agents(self):
        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("main")
            started = threading.Event()

            def worker(goal, cancel):
                started.set()
                while not cancel.is_set():
                    time.sleep(0.005)

            task = runtime.spawn_agent("forever", worker)
            self.assertTrue(started.wait(2))
            runtime.shutdown()
            task.thread.join(3)
            self.assertFalse(task.thread.is_alive())

    def test_exiting_waits_a_bounded_time_no_matter_how_many_are_stuck(self):
        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("main")

            def stuck(goal, cancel):
                time.sleep(30)

            for _ in range(4):
                runtime.spawn_agent("stuck", stuck)
            started = time.monotonic()
            runtime.shutdown()
            self.assertLess(time.monotonic() - started, 4.0)

    def test_the_number_of_live_sub_agents_is_capped(self):
        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("main")

            def stuck(goal, cancel):
                while not cancel.is_set():
                    time.sleep(0.01)

            for _ in range(4):
                runtime.spawn_agent("stuck", stuck)
            with self.assertRaises(RuntimeError):
                runtime.spawn_agent("one too many", stuck)
            runtime.shutdown()

    def test_a_store_closed_mid_flight_does_not_mark_a_finished_task_failed(self):
        with self._workspace() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("main")
            release = threading.Event()

            def worker(goal, cancel):
                release.wait(2.0)
                return "THE ANSWER"

            task = runtime.spawn_agent("question", worker)
            runtime.close(shutdown=False)
            runtime._durable_closed = True     # the store is gone
            release.set()
            task.thread.join(5)
            self.assertEqual(task.result, "THE ANSWER")
            self.assertIsNone(task.error)

    def test_cancellation_is_heard_during_a_stream(self):
        """The cancel flag was only checked between steps, so /cancel said
        'requested' and nothing happened until the whole reply had arrived."""
        from fun.runtime import _collect_response

        cancel = threading.Event()
        seen = []

        def chunks():
            for index in range(20):
                seen.append(index)
                if index == 2:
                    cancel.set()
                yield {"choices": [{"delta": {"content": "x"}}]}

        content, calls = _collect_response(chunks(), cancel)
        self.assertLess(len(seen), 20)
