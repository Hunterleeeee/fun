import json
import tempfile
import os
import time
import shlex
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory

from fun.events import Event, EventStore
from fun.lock import WorkspaceLock, WorkspaceLockError
from fun.policy import ApprovalMode, Policy, PolicyError, WorkspaceGuard
from fun.ui.text import display_width as _display_width
from fun.terminal_ui import TerminalUiState, TranscriptItem
from fun.tui import TerminalUI
from fun.ui.theme import Theme
from fun.runtime import Runtime
from fun.tools import Tools, file_hash
from fun.usage import Usage


PYTHON_BIN = Path(sys.executable).name
# Validation commands built from programs the tool considers benign, so these
# tests exercise the subprocess machinery rather than an interpreter escape
# hatch.  Earlier versions used `python -c`, which the tool now refuses — and
# which meant every "validation failed" they asserted came from the refusal
# rather than from a command that actually failed.
PYTHON_OK = "true"
PYTHON_FAIL = "false"


def slow_command(seconds: int = 2) -> str:
    return f"sleep {seconds}"


def loud_command(directory, size: int = 300000, name: str = "loud.txt") -> str:
    """A command that really writes ``size`` bytes to stdout."""
    Path(directory, name).write_text("x" * size, encoding="utf-8")
    return f"cat {name}"


def runtime_usage_summary():
    return Usage().summary()


class CoreTests(unittest.TestCase):
    def test_short_conversational_goal_uses_lightweight_plan(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            task = runtime.create_task("你好啊")
            self.assertEqual(task.plan, ["understand the request", "respond"])

    def test_cross_process_recovery_persists_followup_tool_events(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, "auto", state_dir=directory)
            first.create_task("cross process recovery")
            session_id = first.session_id
            first.lock.release()
            first.events._durable.close()
            child_env = os.environ.copy()
            root = str(Path(__file__).resolve().parents[1])
            child_env["PYTHONPATH"] = root + os.pathsep + child_env.get("PYTHONPATH", "")
            child = subprocess.run([
                os.environ.get("PYTHON", sys.executable), "-c",
                "from fun.runtime import Runtime; import sys; r=Runtime.recover(sys.argv[1], sys.argv[1], sys.argv[2], approval='auto'); r.run_tool('explore', path='.'); r.stop()",
                directory, session_id,
            ], check=False, capture_output=True, text=True, env=child_env)
            self.assertEqual(child.returncode, 0, child.stderr)
            recovered = Runtime.recover(directory, directory, session_id, approval="auto")
            event_types = [event.type for event in recovered.events.list(session_id)]
            self.assertEqual(event_types.count("tool.requested"), 1)
            self.assertEqual(event_types.count("tool.executing"), 1)
            self.assertEqual(event_types.count("tool.completed"), 1)
            self.assertGreaterEqual(len(event_types), 8)
            recovered.stop()

    def test_event_sequence_generation_is_thread_safe(self):
        events = []
        threads = [threading.Thread(target=lambda: events.append(Event("threaded", "session"))) for _ in range(100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({event.seq for event in events}), 100)

    def test_event_store_rejects_duplicate_sequences_in_batch(self):
        store = EventStore()
        first = Event("one", "session", id="one", seq=10)
        second = Event("two", "session", id="two", seq=10)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_EVENT_SEQ"):
            store.append_many([first, second])
        self.assertEqual(store.list(), [])

    def test_event_store_rejects_duplicate_ids_in_batch(self):
        store = EventStore()
        first = Event("one", "session", id="same")
        second = Event("two", "session", id="same")
        with self.assertRaisesRegex(ValueError, "DUPLICATE_EVENT_ID"):
            store.append_many([first, second])
        self.assertEqual(store.list(), [])

    def test_recovered_store_continues_persisting_new_events(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, "auto", state_dir=directory)
            first.create_task("continue after recovery")
            session_id = first.session_id
            first.close()
            recovered = Runtime.recover(directory, directory, session_id, approval="auto")
            recovered.run_tool("explore", path=".")
            event_types = [event.type for event in recovered.events.list(session_id)]
            self.assertIn("tool.requested", event_types)
            self.assertIn("tool.executing", event_types)
            self.assertIn("tool.completed", event_types)
            self.assertIn("agent.node", event_types)
            self.assertGreaterEqual(len(event_types), 8)
            recovered.stop()

    def test_usage_accumulates_multiple_provider_responses(self):
        usage = Usage()
        usage.merge_provider({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})
        usage.merge_provider({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        self.assertEqual(usage.input_tokens, 17)
        self.assertEqual(usage.output_tokens, 7)
        self.assertEqual(usage.total_tokens, 24)

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

    def test_public_tools_reject_invalid_pagination_ranges(self):
        with TemporaryDirectory() as directory:
            Path(directory, "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
            tools = Tools(directory)
            for result in (
                tools.explore(".", -1),
                tools.explore(".", 0),
                tools.read("a.txt", -1),
                tools.read("a.txt", 1, 0),
                tools.read("a.txt", 3, 2),
            ):
                self.assertFalse(result.ok)
                self.assertEqual(result.text, "INVALID_ARGUMENTS")

    def test_runtime_recovers_agent_state_from_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("recover state")
            runtime._node("tools.executing")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "tools.executing")
            recovered.stop()

    def test_recovery_required_blocks_until_acknowledged(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("recover side effect")
            runtime.emit("tool.executing", runtime.task.id, call_id="call_1", name="exec", arguments={"command": "echo hi"})
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.status, "recovery_required")
            self.assertEqual(recovered.task.recovery_reason, "tool.executing")
            self.assertEqual(recovered.task.pending_tool["name"], "exec")
            self.assertEqual(recovered.task.pending_tool["arguments"]["command"], "echo hi")
            self.assertEqual(recovered.recovery_summary()["call_id"], "call_1")
            with self.assertRaisesRegex(RuntimeError, "TASK_NOT_RUNNING"):
                recovered.run_model_turn()
            recovered.acknowledge_recovery("mark_failed")
            self.assertEqual(recovered.task.status, "running")
            self.assertIsNone(recovered.task.pending_tool)
            self.assertIn("recovery.marked_failed", [event.type for event in recovered.events.list()])
            recovered.stop()
            replayed = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(replayed.task.status, "stopped")
            self.assertIsNone(replayed.task.pending_tool)
            replayed.close()

    def test_approval_pending_replays_arguments(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("approval")
            runtime.emit("approval.pending", runtime.task.id, call_id="call_2", name="edit", risk="medium", arguments={"path": "a.txt"})
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.status, "recovery_required")
            self.assertEqual(recovered.recovery_summary()["arguments"]["path"], "a.txt")
            recovered.acknowledge_recovery("discard")
            self.assertIsNone(recovered.task.pending_tool)
            self.assertIn("recovery.discarded", [event.type for event in recovered.events.list()])
            recovered.close()
            replayed = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(replayed.task.status, "running")
            self.assertIsNone(replayed.task.pending_tool)
            replayed.stop()

    def test_create_task_failure_does_not_leave_task_or_lock(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", event_store=EventStore(FailingStore()))
            with self.assertRaises(OSError):
                runtime.create_task("cannot persist")
            self.assertIsNone(runtime.task)
            self.assertFalse(runtime.lock.held)

    def test_runtime_recovers_cumulative_usage_from_events(self):
        """Emitted the way run_model_turn emits it: cumulative, not per-turn.

        The previous version of this test hand-wrote per-turn deltas, which no
        code path produces, and then asserted the sum — so it passed while the
        real replay counted every snapshot again and grew with the square of the
        turn count.
        """
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("usage")
            for report in ({"prompt_tokens": 10, "completion_tokens": 4}, {"prompt_tokens": 7, "completion_tokens": 3}):
                runtime.usage.merge_provider(report)
                runtime.emit("model.completed", runtime.task.id, usage=runtime.usage.as_dict())
            self.assertEqual((runtime.usage.input_tokens, runtime.usage.output_tokens, runtime.usage.total_tokens), (17, 7, 24))
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.usage.input_tokens, 17)
            self.assertEqual(recovered.usage.output_tokens, 7)
            self.assertEqual(recovered.usage.total_tokens, 24)
            recovered.stop()

    def test_replayed_usage_does_not_grow_with_the_turn_count(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("usage")
            for _ in range(6):
                runtime.usage.merge_provider({"prompt_tokens": 100, "completion_tokens": 50})
                runtime.emit("model.completed", runtime.task.id, usage=runtime.usage.as_dict())
            live = runtime.usage.total_tokens
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.usage.total_tokens, live)
            recovered.stop()

    def test_runtime_recovers_task_from_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            task = runtime.create_task("recover me")
            runtime.pause()
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.id, task.id)
            self.assertEqual(recovered.task.status, "paused")
            recovered.stop()

    def test_failed_task_reason_replays_after_restart(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("failure replay")
            runtime.fail("provider unavailable")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.status, "stopped")
            self.assertEqual(recovered.task.failure_reason, "provider unavailable")
            self.assertIsNone(recovered.task.recovery_reason)
            recovered.stop()

    def test_failed_task_preserves_failure_fact(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("will fail")
            runtime.fail("provider unavailable")
            self.assertEqual(runtime.task.agent_state, "failed")
            self.assertIn("task.failed", [event.type for event in runtime.events.list()])
            self.assertEqual(runtime.events.list()[-2].payload["reason"], "provider unavailable")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.status, "stopped")
            self.assertEqual(recovered.task.agent_state, "failed")
            self.assertEqual(recovered.task.failure_reason, "provider unavailable")
            self.assertIsNone(recovered.task.recovery_reason)
            recovered.stop()

    def test_terminal_runtime_stop_is_idempotent_after_complete_or_fail(self):
        with TemporaryDirectory() as directory:
            completed = Runtime(directory, state_dir=directory)
            completed.create_task("complete then stop")
            completed.complete("done")
            completed.stop()
            self.assertFalse(completed.lock.held)

            failed = Runtime(directory, state_dir=directory)
            failed.create_task("fail then stop")
            failed.fail("broken")
            failed.stop()
            self.assertFalse(failed.lock.held)

    def test_terminal_runtime_paths_close_durable_store(self):
        with TemporaryDirectory() as directory:
            completed = Runtime(directory, state_dir=directory)
            completed.create_task("complete close")
            durable = completed.events._durable
            completed.complete("done")
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()

            failed = Runtime(directory, state_dir=directory)
            failed.create_task("fail close")
            durable = failed.events._durable
            failed.fail("nope")
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()

    def test_recovery_resume_keeps_store_open_for_execution(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, state_dir=directory)
            first.create_task("recovery resume")
            session_id = first.session_id
            first.events.append(Event("tool.executing", first.session_id, first.task.id, {"call_id": "call_resume", "name": "explore"}))
            first.lock.release()
            first.close()
            recovered = Runtime.recover(directory, directory, session_id, approval="auto")
            recovered.acknowledge_recovery("resume")
            self.assertEqual(recovered.task.status, "running")
            recovered.run_tool("explore", path=".")
            self.assertFalse(recovered._closed)
            recovered.stop()

    def test_recovery_mark_failed_keeps_store_open_for_resume(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, state_dir=directory)
            first.create_task("recovery mark failed")
            session_id = first.session_id
            first.events.append(Event("tool.executing", first.session_id, first.task.id, {"call_id": "call_failed", "name": "explore"}))
            first.lock.release()
            first.close()
            recovered = Runtime.recover(directory, directory, session_id, approval="auto")
            recovered.acknowledge_recovery("mark_failed")
            self.assertEqual(recovered.task.status, "running")
            recovered.run_tool("explore", path=".")
            self.assertFalse(recovered._closed)
            recovered.stop()

    def test_recovery_discard_keeps_store_open_for_resume(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, state_dir=directory)
            first.create_task("recovery discard")
            session_id = first.session_id
            first.events.append(Event("tool.executing", first.session_id, first.task.id, {"call_id": "call_discard", "name": "explore"}))
            first.lock.release()
            first.close()
            recovered = Runtime.recover(directory, directory, session_id, approval="auto")
            recovered.acknowledge_recovery("discard")
            self.assertEqual(recovered.task.status, "running")
            recovered.run_tool("explore", path=".")
            self.assertFalse(recovered._closed)
            recovered.stop()

    def test_explicit_recovery_stop_closes_store(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, state_dir=directory)
            first.create_task("recovery explicit stop")
            session_id = first.session_id
            first.events.append(Event("tool.executing", first.session_id, first.task.id, {"call_id": "call_explicit", "name": "explore"}))
            first.lock.release()
            first.close()
            recovered = Runtime.recover(directory, directory, session_id)
            durable = recovered.events._durable
            recovered.acknowledge_recovery("stop")
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()

    def test_recovery_context_stop_replays_as_stopped(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, state_dir=directory)
            first.create_task("recovery replay")
            session_id = first.session_id
            first.events.append(Event("tool.executing", first.session_id, first.task.id, {"call_id": "call_replay", "name": "explore"}))
            first.lock.release()
            first.close()
            with Runtime.recover(directory, directory, session_id):
                pass
            replayed = Runtime.recover(directory, directory, session_id)
            self.assertEqual(replayed.task.status, "stopped")
            self.assertNotEqual(replayed.task.status, "recovery_required")
            replayed.close()

    def test_runtime_context_manager_releases_recovery_lock(self):
        with TemporaryDirectory() as directory:
            first = Runtime(directory, state_dir=directory)
            first.create_task("recovery context")
            session_id = first.session_id
            first.events.append(Event("tool.executing", first.session_id, first.task.id, {"call_id": "call_context", "name": "explore"}))
            first.lock.release()
            first.close()
            with Runtime.recover(directory, directory, session_id) as recovered:
                self.assertEqual(recovered.task.status, "recovery_required")
                lock = recovered.lock
            self.assertFalse(lock.held)
            self.assertFalse(lock.path.exists())
            self.assertEqual(recovered.task.status, "stopped")
            self.assertIn("task.stopped", [event.type for event in recovered.events.list(session_id)])

    def test_runtime_context_manager_releases_active_lock(self):
        with TemporaryDirectory() as directory:
            with Runtime(directory, state_dir=directory) as runtime:
                runtime.create_task("active context")
                lock = runtime.lock
            self.assertFalse(lock.held)
            self.assertFalse(lock.path.exists())

    def test_runtime_context_exception_stops_task_and_releases_resources(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "context failure"):
                with Runtime(directory, state_dir=directory) as runtime:
                    runtime.create_task("exception context")
                    session_id = runtime.session_id
                    lock = runtime.lock
                    durable = runtime.events._durable
                    raise ValueError("context failure")
            self.assertFalse(lock.held)
            self.assertFalse(lock.path.exists())
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()
            recovered = Runtime.recover(directory, directory, session_id)
            self.assertEqual(recovered.task.status, "stopped")
            self.assertIn("task.stopped", [event.type for event in recovered.events.list(session_id)])
            recovered.close()

    def test_runtime_context_manager_closes_store_on_success_and_error(self):
        with TemporaryDirectory() as directory:
            with Runtime(directory, state_dir=directory) as runtime:
                runtime.create_task("context success")
                durable = runtime.events._durable
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()

            with self.assertRaisesRegex(ValueError, "boom"):
                with Runtime(directory, state_dir=directory) as runtime:
                    durable = runtime.events._durable
                    raise ValueError("boom")
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()

    def test_runtime_close_is_idempotent_after_context_cleanup(self):
        with TemporaryDirectory() as directory:
            with Runtime(directory, state_dir=directory) as runtime:
                runtime.create_task("close idempotent")
                durable = runtime.events._durable
            runtime.close()
            runtime.close()
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()

    def test_memory_runtime_close_is_idempotent(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.close()
            runtime.close()
            self.assertTrue(runtime._closed)

    def test_runtime_stop_closes_durable_store_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("close lifecycle")
            durable = runtime.events._durable
            runtime.stop()
            self.assertTrue(runtime._closed)
            with self.assertRaisesRegex(Exception, "closed"):
                durable.list()
            runtime.stop()
            self.assertTrue(runtime._closed)

    def test_goal_creation_matches_normal_task_lifecycle(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            task = runtime.set_goal("run the focused tests")
            self.assertEqual(task.status, "running")
            self.assertEqual(runtime.events.list()[-2].type, "task.started")
            runtime.stop()

    def test_goal_can_be_read_and_replaced_after_stop(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.set_goal("first goal")
            self.assertEqual(runtime.goal(), "first goal")
            with self.assertRaisesRegex(RuntimeError, "TASK_ALREADY_ACTIVE"):
                runtime.set_goal("blocked goal")
            runtime.stop()
            runtime.set_goal("second goal")
            self.assertEqual(runtime.goal(), "second goal")
            self.assertIn("goal.replaced", [event.type for event in runtime.events.list()])
            runtime.stop()

    def test_completed_result_replays_after_restart(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("result replay")
            runtime.complete("verified result")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.status, "completed")
            self.assertEqual(recovered.task.result, "verified result")
            recovered.stop()

    def test_sqlite_runtime_batch_events_replay(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("sqlite batch")
            runtime.replace_plan(["one", "two"])
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.plan, ["one", "two"])
            recovered.stop()

    def test_runtime_can_persist_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("persist")
            self.assertGreaterEqual(len(runtime.events.list()), 2)
            self.assertTrue((Path(directory) / "events.db").exists())
            runtime.close()

    def test_repl_control_methods_emit_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.create_task("controls")
            runtime.pause()
            runtime.resume()
            runtime.checkpoint()
            runtime.stop()
            self.assertEqual([event.type for event in runtime.events.list()][-4:], ["task.paused", "task.resumed", "checkpoint.created", "task.stopped"])

    def test_complete_does_not_mutate_when_result_persistence_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("complete")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.complete("done")
            self.assertEqual(runtime.task.status, "running")
            self.assertTrue(runtime.lock.held)

    def test_fail_is_atomic_when_stopped_event_persistence_fails(self):
        class FailingBatchStore:
            def append_many(self, events):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("fail atomic")
            runtime.events._durable = FailingBatchStore()
            with self.assertRaises(OSError):
                runtime.fail("broken")
            self.assertEqual(runtime.task.status, "running")
            self.assertEqual(runtime.task.agent_state, "ready")

    def test_fail_does_not_mutate_agent_state_when_event_persistence_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("fail")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.fail("broken")
            self.assertEqual(runtime.task.status, "running")
            self.assertEqual(runtime.task.agent_state, "ready")

    def test_pause_does_not_mutate_when_event_persistence_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("pause")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.pause()
            self.assertEqual(runtime.task.status, "running")

    def test_stop_is_idempotent_after_terminal_state(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("stop twice")
            runtime.stop()
            events = len(runtime.events.list())
            runtime.stop()
            self.assertEqual(runtime.task.status, "stopped")
            self.assertEqual(len(runtime.events.list()), events)
            self.assertFalse(runtime.lock.held)

    def test_task_pause_resume_and_complete(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.create_task("state")
            runtime.pause()
            self.assertEqual(runtime.task.status, "paused")
            runtime.resume()
            runtime.complete("done")
            self.assertEqual(runtime.task.status, "completed")
            self.assertEqual(runtime.events.list()[-2].type, "task.completed")
            self.assertEqual(runtime.events.list()[-1].type, "task.result")

    def test_unknown_tool_is_rejected(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("tool")
            result = runtime.run_tool("emit", event_type="task.completed")
            self.assertFalse(result.ok)
            self.assertEqual(result.text, "UNSUPPORTED_TOOL")

    def test_runtime_provider_can_be_cleared_after_logout(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, provider=object())
            runtime.model = "model-x"
            runtime.provider = None
            runtime.model = ""
            self.assertIsNone(runtime.provider)
            self.assertEqual(runtime.model, "")

    def test_tui_queues_approval_without_direct_render_thread_access(self):
        import threading
        ui = TerminalUI(commands=["/help", "/status"])
        result = []
        waiter = threading.Thread(target=lambda: result.append(ui.request_approval("exec", "medium", {"command": "echo hi"})))
        waiter.start()
        kind, request = ui.events.get(timeout=1)
        self.assertEqual(kind, "approval")
        request.answer = "n"
        request.done.set()
        waiter.join(1)
        self.assertEqual(result, ["no"])

    def test_ui_state_renders_transcript_tools_and_dock(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.add_user("inspect the project")
        ui.tool_status("tool.executing", {"call_id": "c1", "name": "read", "arguments": {"path": "README.md"}})
        ui.tool_status("tool.completed", {"call_id": "c1", "name": "read", "elapsed_ms": 12, "text": "hello"})
        ui.add_assistant("Do")
        ui.add_assistant("ne")
        rendered = ui.render()
        self.assertIn("inspect the project", rendered)
        self.assertIn("read", rendered)
        self.assertIn("12ms", rendered)
        self.assertIn("README.md", rendered)
        self.assertIn("Done", rendered)
        self.assertIn("smart", rendered)
        # Count the role node, not the bare character: the composer placeholder
        # also contains 你, which is not a second speaker.
        role_lines = [line for line in rendered.splitlines() if line.strip().startswith(("●", "@")) and "你" in line]
        self.assertEqual(len(role_lines), 1)

    def test_ui_state_marks_slash_commands_separately_from_prose(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.add_command("/prompt")
        ui.add_user("real message")
        rendered = ui.render()
        self.assertIn("/prompt", rendered)
        self.assertIn("real message", rendered)
        # Commands belong in history too: they are the lines most worth
        # repeating, and ↑ used to recall prompts but never commands.
        self.assertEqual(ui.composer_history, ["/prompt", "real message"])

    def test_ui_state_render_never_exceeds_width_with_or_without_color(self):
        for mode in ("none", "truecolor"):
            ui = TerminalUiState(theme=Theme(mode=mode, locale="zh-CN"))
            ui.add_user("a very long user message that should wrap instead of overflowing the terminal")
            ui.add_assistant("这是一段很长的中文回答用来验证宽字符不会撑破终端边界限制")
            ui.tool_status("tool.completed", {"call_id": "c1", "name": "exec", "arguments": {"command": "pytest -q"}, "text": "x" * 300, "elapsed_ms": 9})
            for width in (32, 40, 72, 120):
                for line in ui.render(width).splitlines():
                    self.assertLessEqual(_display_width(line), width, f"mode={mode} width={width} line={line!r}")

    def test_ui_state_respects_height_budget(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        for index in range(20):
            ui.add_user(f"message-{index}")
        self.assertLessEqual(len(ui.render(40, 12).splitlines()), 12)

    def test_ui_state_flushes_settled_items_once(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.add_user("hi")
        ui.mode = "working"
        ui.add_assistant("partial answer")
        first = ui.flush(72)
        self.assertTrue(any("hi" in line for line in first))
        self.assertFalse(any("partial answer" in line for line in first))
        ui.mode = "ready"
        second = ui.flush(72)
        self.assertTrue(any("partial answer" in line for line in second))
        self.assertEqual(ui.flush(72), [])

    def test_ui_state_holds_back_running_tools_until_they_settle(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.tool_status("tool.executing", {"call_id": "c1", "name": "exec"})
        self.assertEqual(ui.flush(72), [])
        ui.tool_status("tool.completed", {"call_id": "c1", "name": "exec", "text": "done"})
        self.assertTrue(ui.flush(72))

    def test_ui_state_modes_and_toast_surface_in_the_dock(self):
        working = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"), mode="working", task_state="working")
        self.assertIn("working", working.render())
        approval = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"), mode="approval")
        approval.add_user("x")
        self.assertIn("Build", approval.render())
        toasted = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        toasted.toast = "System prompt updated"
        self.assertIn("System prompt updated", toasted.render())
        for _ in range(20):
            toasted.tick()
        self.assertNotIn("System prompt updated", toasted.render())

    def test_ui_state_scroll_and_history_navigation(self):
        """Scrolling back must reveal *older* content.

        The previous version asserted the old model — that scroll(3) dropped the
        first three items — which is why it stayed green while PgUp did nothing
        at all: dropping from the front cannot change a view pinned to the tail.
        """
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        for index in range(40):
            ui.transcript.append(TranscriptItem("system", f"line-{index}"))
        bottom = "\n".join(ui.compose(70, 16))
        self.assertIn("line-39", bottom)
        self.assertNotIn("line-0", bottom)
        ui.scroll(-999)                       # PgUp to the very top
        top = "\n".join(ui.compose(70, 16))
        self.assertIn("line-0", top)
        self.assertNotIn("line-39", top)
        ui.scroll(999)                        # PgDn back to the newest
        self.assertIn("line-39", "\n".join(ui.compose(70, 16)))
        self.assertEqual(ui.scroll_offset, 0)
        ui.add_user("first")
        ui.add_user("second")
        self.assertEqual(ui.history(-1), "second")
        self.assertEqual(ui.history(-1), "first")
        self.assertEqual(ui.history(1), "second")

    def test_ui_state_collapses_and_expands_tool_output(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.tool_status("tool.completed", {"call_id": "c1", "name": "read", "text": "line\n" * 40})
        self.assertIn("还有", ui.render())
        ui.toggle_output()
        self.assertIn("隐藏", ui.render())
        ui.toggle_output()
        self.assertIn("line", ui.render())

    def test_ui_state_shows_approval_and_recovery_prompts(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.tool_status("approval.pending", {"call_id": "a1", "name": "exec", "risk": "medium", "arguments": {"command": "echo hi"}})
        approval = ui.render()
        self.assertIn("需要授权", approval)
        self.assertIn("medium", approval)
        self.assertIn("允许一次", approval)
        ui.set_recovery({"name": "exec", "call_id": "c9", "arguments": "command='echo hi'"})
        recovery = ui.render()
        self.assertIn("需要恢复处置", recovery)
        self.assertIn("继续执行", recovery)
        self.assertIn("command='echo hi'", recovery)

    def test_ui_state_restores_a_recovered_transcript(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.restore_messages([
            {"role": "user", "content": "old goal"},
            {"role": "assistant", "content": "old answer"},
            {"role": "tool", "tool_call_id": "t1", "content": "old output"},
        ])
        rendered = ui.render()
        self.assertIn("old goal", rendered)
        self.assertIn("old answer", rendered)
        self.assertIn("old output", rendered)

    def test_ui_state_lists_background_tasks(self):
        ui = TerminalUiState(theme=Theme(mode="none", locale="zh-CN"))
        ui.set_background([{"id": "bg_1", "status": "running", "goal": "search files", "result": "", "error": ""}])
        self.assertIn("bg_1", ui.render())
        self.assertIn("running", ui.render())

    def test_app_modals_mask_secrets_and_track_selection(self):
        from io import StringIO
        app = TerminalUI(output=StringIO(), theme=Theme(mode="none", locale="zh-CN"))
        app.open_form("Provider configuration", ["base_url", ("api_key", True)], lambda values: None)
        self.assertIsNotNone(app.modal)
        self.assertIn("Provider configuration", "\n".join(app.modal.lines(app.state.theme, 60)))
        app.modal.handle("x")
        app.modal.handle("enter")
        app.modal.value = "secret-key"
        rendered = "\n".join(app.modal.lines(app.state.theme, 60))
        self.assertNotIn("secret-key", rendered)
        self.assertIn("\u2022", rendered)
        picked = []
        app.open_select("Choose model", ["old", "new"], picked.append)
        app.modal.handle("down")
        self.assertEqual(app.modal.index, 1)
        app.modal.handle("enter")
        self.assertEqual(picked, ["new"])

    def test_app_queues_approval_without_touching_the_render_thread(self):
        import threading
        app = TerminalUI(commands=["/help", "/status"], theme=Theme(mode="none", locale="zh-CN"))
        result = []
        waiter = threading.Thread(target=lambda: result.append(app.request_approval("exec", "medium", {"command": "echo hi"})))
        waiter.start()
        kind, request = app.events.get(timeout=1)
        self.assertEqual(kind, "approval")
        request.answer = "n"
        request.done.set()
        waiter.join(1)
        self.assertEqual(result, ["no"])


    def test_validation_start_failure_does_not_change_agent_state(self):
        class FailingStore:
            def append(self, event):
                if event.type == "validation.started":
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("validation node atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.validate(PYTHON_OK)
            self.assertEqual(runtime.task.agent_state, "ready")

    def test_validation_failure_event_does_not_update_plan_step(self):
        class FailingStore:
            def append(self, event):
                if event.type in {"validation.completed", "validation.failed"}:
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("validation plan atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.validate(PYTHON_FAIL)
            self.assertIsNone(runtime.task.validation)

    def test_validation_does_not_mutate_when_validation_event_fails(self):
        class FailingStore:
            def append(self, event):
                if event.type in {"validation.completed", "validation.failed"}:
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("validation atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.validate(PYTHON_OK)
            self.assertIsNone(runtime.task.validation)

    def test_repair_node_failure_does_not_change_agent_state(self):
        class FailingStore:
            def append(self, event):
                if event.type == "agent.node":
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair node atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.repair(PYTHON_OK)
            self.assertEqual(runtime.task.agent_state, "ready")
            self.assertEqual(runtime.task.repair_attempts, 1)

    def test_repair_result_survives_terminal_fact_persistence_failure(self):
        class FailingAfterValidation:
            def append(self, event):
                if event.type in {"repair.completed", "repair.failed"}:
                    raise OSError("terminal fact unavailable")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair terminal")
            runtime.events._durable = FailingAfterValidation()
            result = runtime.repair(PYTHON_FAIL)
            self.assertFalse(result.ok)
            self.assertEqual(runtime.task.repair_attempts, 1)
            self.assertIsNotNone(runtime.task.validation)

    def test_repair_preserves_original_error_if_failure_fact_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair original error")
            runtime.events._durable = FailingStore()
            with self.assertRaisesRegex(OSError, "disk full"):
                runtime.repair(PYTHON_FAIL)
            self.assertEqual(runtime.task.repair_attempts, 0)

    def test_repair_failure_event_is_recorded_before_error_propagates(self):
        class FailingStore:
            def __init__(self):
                self.failed_after_start = False
            def append(self, event):
                if event.type == "tool.executing":
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair failure")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.repair(PYTHON_FAIL)
            types = [event.type for event in runtime.events.list()]
            self.assertIn("repair.started", types)
            self.assertIn("repair.failed", types)
            self.assertEqual(runtime.task.repair_attempts, 1)

    def test_repair_start_does_not_consume_budget_when_event_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.repair(PYTHON_FAIL)
            self.assertEqual(runtime.task.repair_attempts, 0)

    def test_repair_failure_recovery_returns_agent_to_ready(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("repair ready")
            runtime.repair(PYTHON_FAIL)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id, approval="auto")
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_validation_recovery_returns_agent_to_ready(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("validation ready")
            runtime.validate(PYTHON_OK)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id, approval="auto")
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_repair_attempts_replay_and_remain_bounded(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("repair replay")
            runtime.repair(PYTHON_FAIL, max_attempts=1)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id, approval="auto")
            self.assertEqual(recovered.task.repair_attempts, 1)
            blocked = recovered.repair(PYTHON_OK, max_attempts=1)
            self.assertFalse(blocked.ok)
            self.assertEqual(blocked.text, "REPAIR_BUDGET_EXCEEDED")
            recovered.stop()

    def test_rejected_plan_error_replays(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("bad plan")
            runtime.task.plan_error = "INVALID_PLAN"
            runtime.emit("plan.rejected", runtime.task.id, reason="INVALID_PLAN", summary={"count": 2, "types": ["str", "int"], "lengths": [3, None]})
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id, approval="auto")
            self.assertEqual(recovered.task.plan_error, "INVALID_PLAN")
            self.assertEqual(recovered.task.plan_error_summary["count"], 2)
            recovered.stop()

    def test_replay_response_parsed_clears_previous_failure(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("response ordering")
            runtime.emit("response.failed", runtime.task.id, error_type="ValueError", error_tag="MALFORMED_RESPONSE", summary={"content_length": 0})
            runtime.emit("response.parsed", runtime.task.id, content_length=2, tool_calls=0, plan_updated=False, summary={"content_length": 2, "tool_calls": 0})
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertIsNone(recovered.task.response_error)
            recovered.stop()

    def test_replayed_plan_replacement_clears_rejection(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("replace error")
            runtime.emit("plan.rejected", runtime.task.id, reason="INVALID_PLAN", summary={"count": 1})
            runtime.replace_plan(["valid step"])
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id, approval="auto")
            self.assertIsNone(recovered.task.plan_error)
            self.assertIsNone(recovered.task.plan_error_summary)
            self.assertEqual(recovered.task.plan, ["valid step"])
            recovered.stop()

    def test_transactional_durable_adapter_rolls_back_batch(self):
        class TransactionalStore:
            def __init__(self):
                self.events = []
                self.pending = []

            def begin(self):
                self.pending = []

            def append(self, event):
                if len(self.pending) == 1:
                    raise OSError("disk full")
                self.pending.append(event)

            def commit(self):
                self.events.extend(self.pending)
                self.pending = []

            def rollback(self):
                self.pending = []

        durable = TransactionalStore()
        store = EventStore(durable)
        with self.assertRaises(OSError):
            store.append_many([Event("a", "s"), Event("b", "s")])
        self.assertEqual(durable.events, [])
        self.assertEqual(store.list(), [])

    def test_plan_replace_does_not_mutate_when_event_persistence_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("plan")
            runtime.events._durable = FailingStore()
            original = list(runtime.task.plan)
            with self.assertRaises(OSError):
                runtime.replace_plan(["new step"])
            self.assertEqual(runtime.task.plan, original)

    def test_plan_step_does_not_mutate_when_event_persistence_fails(self):
        class FailingStore:
            def append(self, event):
                raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("plan step")
            runtime.events._durable = FailingStore()
            original = list(runtime.task.plan_status)
            with self.assertRaises(OSError):
                runtime.update_plan_step(0, "active", "start")
            self.assertEqual(runtime.task.plan_status, original)

    def test_dynamic_plan_replaces_and_replays_latest_steps(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("dynamic plan")
            runtime.replace_plan(["inspect code", "edit safely", "run tests"])
            runtime.update_plan_step(0, "done", "inspected")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id, approval="auto")
            self.assertEqual(recovered.task.plan, ["inspect code", "edit safely", "run tests"])
            self.assertEqual(recovered.task.plan_status, ["done", "pending", "pending"])
            self.assertEqual([event.type for event in runtime.events.list()][-2], "plan.replaced")
            recovered.stop()

    def test_plan_step_updates_are_event_sourced(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("plan")
            runtime.update_plan_step(0, "active", "started inspection")
            runtime.update_plan_step(0, "done", "files inspected")
            self.assertEqual(runtime.task.plan_status[0], "done")
            self.assertEqual(runtime.events.list()[-1].type, "plan.step_updated")

    def test_tool_execution_advances_plan_step(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "hello.txt").write_text("hello\n", encoding="utf-8")
            runtime = Runtime(directory, "auto")
            runtime.create_task("inspect")
            runtime.run_tool("read", path="hello.txt")
            self.assertEqual(runtime.task.plan_status[0], "done")
            self.assertTrue(any(event.type == "plan.step_updated" for event in runtime.events.list()))

    def test_plan_step_status_replays(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("inspect")
            runtime.update_plan_step(0, "active", "reading")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.plan_status[0], "active")
            recovered.stop()

    def test_tool_lifecycle_events_are_explicit(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "hello.txt").write_text("hello\n", encoding="utf-8")
            runtime = Runtime(directory, "auto")
            runtime.create_task("inspect")
            runtime.run_tool("read", path="hello.txt")
            types = [event.type for event in runtime.events.list()]
            self.assertIn("tool.requested", types)
            self.assertIn("tool.executing", types)
            self.assertIn("tool.completed", types)
            call_ids = [event.payload.get("call_id") for event in runtime.events.list() if event.type.startswith("tool.")]
            self.assertEqual(len(set(call_ids)), 1)

    def test_runtime_emits_tool_events(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "hello.txt").write_text("hello\n", encoding="utf-8")
            runtime = Runtime(directory)
            runtime.create_task("inspect files")
            result = runtime.run_tool("read", path="hello.txt")
            self.assertTrue(result.ok)
            self.assertEqual(
                [event.type for event in runtime.events.list()],
                ["task.created", "plan.created", "task.started", "agent.node", "plan.step_updated", "tool.requested", "tool.executing", "tool.completed", "plan.step_updated"],
            )
            runtime.stop()
            self.assertEqual(runtime.task.status, "stopped")

    def test_protected_file_is_not_read(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text("SECRET=x", encoding="utf-8")
            tools = Tools(directory)
            with self.assertRaisesRegex(PolicyError, "PROTECTED_PATH"):
                tools.read(".env")

    def test_approval_rejection_is_a_terminal_tool_fact(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            runtime = Runtime(directory, "smart")
            runtime.create_task("reject edit")
            result = runtime.run_tool("edit", path="a.txt", expected_hash=file_hash(path), patch="@@ -1 +1 @@\n-one\n+two\n")
            self.assertEqual(result.text, "APPROVAL_REQUIRED")
            types = [event.type for event in runtime.events.list()]
            self.assertIn("approval.rejected", types)
            self.assertIsNone(runtime.task.pending_tool)

    def test_approval_boundary_blocks_medium_write_without_callback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            runtime = Runtime(directory, "smart")
            runtime.create_task("edit")
            result = runtime.run_tool("edit", path="a.txt", expected_hash=file_hash(path), patch="@@ -1 +1 @@\n-one\n+two\n")
            self.assertFalse(result.ok)
            self.assertEqual(result.text, "APPROVAL_REQUIRED")

    def test_exec_uses_platform_process_group_controls(self):
        with TemporaryDirectory() as directory:
            with patch("fun.tools.sys.platform", "win32"), patch("fun.tools.subprocess.CREATE_NEW_PROCESS_GROUP", 512, create=True), patch("fun.tools.subprocess.Popen") as popen:
                process = popen.return_value
                process.communicate.return_value = ("ok", "")
                process.returncode = 0
                self.assertTrue(Tools(directory).exec("echo 1").ok)
                self.assertEqual(popen.call_args.kwargs["creationflags"], 512)
                self.assertNotIn("start_new_session", popen.call_args.kwargs)

    def test_exec_windows_timeout_kills_process_without_killpg(self):
        with TemporaryDirectory() as directory:
            with patch("fun.tools.sys.platform", "win32"), patch("fun.tools.subprocess.Popen") as popen, patch("fun.tools.os.killpg", create=True) as killpg:
                process = popen.return_value
                process.communicate.side_effect = [subprocess.TimeoutExpired("python", 0.01), ("", "")]
                result = Tools(directory).exec("echo 1", timeout=0.01)
                self.assertFalse(result.ok)
                self.assertIn("COMMAND_TIMEOUT", result.text)
                process.kill.assert_called_once_with()
                killpg.assert_not_called()

    def test_exec_timeout_and_output_limits_use_portable_python(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec(loud_command(directory))
            self.assertTrue(result.ok)
            self.assertIn("OUTPUT_TRUNCATED", result.text)
            timeout = Tools(directory).exec(slow_command(), timeout=0.05)
            self.assertFalse(timeout.ok)
            self.assertIn("COMMAND_TIMEOUT", timeout.text)

    def test_exec_limits_output_and_timeout(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec(loud_command(directory))
            self.assertTrue(result.ok)
            self.assertIn("OUTPUT_TRUNCATED", result.text)
            timeout = Tools(directory).exec(slow_command(), timeout=0.05)
            self.assertFalse(timeout.ok)
            self.assertIn("COMMAND_TIMEOUT", timeout.text)

    def test_exec_blocks_indirect_script_wrappers(self):
        with TemporaryDirectory() as directory:
            for command in ("npm run build", "pnpm run test", "yarn test", "make all", "node -e 'process.exit(0)'", "ruby -e 'puts 1'", "perl -e 'print 1'"):
                result = Tools(directory, Policy(ApprovalMode.ASK)).exec(command)
                self.assertFalse(result.ok)
                self.assertEqual(result.text, "CRITICAL_WRAPPER_BLOCKED")

    def test_exec_blocks_critical_argv_forms(self):
        with TemporaryDirectory() as directory:
            for command in ("rm -rf build", "rm --recursive --force build", "git reset --hard HEAD", "git clean -fd", "sudo echo nope", "curl https://example.com", "env FOO=bar rm -rf build", "command rm --recursive --force build"):
                result = Tools(directory, Policy(ApprovalMode.ASK)).exec(command)
                self.assertFalse(result.ok)
                self.assertEqual(result.text, "APPROVAL_REQUIRED")

    def test_exec_does_not_invoke_a_shell(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "injected"
            command = f'echo "safe; touch {marker}"'
            result = Tools(directory).exec(command)
            self.assertTrue(result.ok)
            self.assertFalse(marker.exists())
            self.assertEqual(result.text, "safe; touch " + str(marker))

    def test_exec_rejects_invalid_command_syntax(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec(f"{PYTHON_BIN} -c 'unterminated")
            self.assertFalse(result.ok)
            self.assertIn("INVALID_COMMAND", result.text)

    def test_exec_runs_inside_workspace(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec("echo ok")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "ok")

    def test_usage_summary_reports_nothing_before_it_measures_anything(self):
        self.assertEqual(runtime_usage_summary(), "")  # nothing measured yet
        measured = Usage()
        measured.merge_provider({"prompt_tokens": 10, "completion_tokens": 4}, ttft_ms=210)
        self.assertIn("ttft", measured.summary())
        self.assertIn("in 10", measured.summary())

    def test_checkpoint_restore_reapplies_git_diff(self):
        with TemporaryDirectory() as directory:
            git_env = os.environ.copy()
            git_env["GIT_CONFIG_NOSYSTEM"] = "1"
            git_env["GIT_CONFIG_GLOBAL"] = os.devnull
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True, env=git_env)
            path = Path(directory) / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=directory, check=True, env=git_env)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], cwd=directory, check=True, env=git_env)
            runtime = Runtime(directory, "auto")
            runtime.create_task("restore")
            path.write_text("two\n", encoding="utf-8")
            snapshot = runtime.checkpoint("before")
            path.write_text("three\n", encoding="utf-8")
            # Restoring discards every uncommitted change, not only the ones the
            # checkpoint knew about, so it has to be asked for explicitly.
            with self.assertRaises(RuntimeError) as caught:
                runtime.restore_checkpoint(snapshot)
            self.assertIn("DISCARD", str(caught.exception))
            runtime.restore_checkpoint(snapshot, discard_other_changes=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "two\n")

    def test_an_unverified_checkpoint_is_not_applied_to_the_worktree(self):
        """Restoring runs `git apply` on whatever text the snapshot holds."""
        with TemporaryDirectory() as directory:
            git_env = os.environ.copy()
            git_env["GIT_CONFIG_NOSYSTEM"] = "1"
            git_env["GIT_CONFIG_GLOBAL"] = os.devnull
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True, env=git_env)
            path = Path(directory) / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=directory, check=True, env=git_env)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], cwd=directory, check=True, env=git_env)
            runtime = Runtime(directory, "auto")
            runtime.create_task("restore")
            path.write_text("two\n", encoding="utf-8")
            snapshot = runtime.checkpoint("before")
            forged = dict(snapshot)
            forged["diff"] = str(snapshot["diff"]).replace("two", "evil")
            with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_NOT_TRUSTED"):
                runtime.restore_checkpoint(forged, discard_other_changes=True)
            without_digest = {key: value for key, value in snapshot.items() if key != "digest"}
            with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_NOT_TRUSTED"):
                runtime.restore_checkpoint(without_digest, discard_other_changes=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "two\n")

    def test_checkpoint_restore_is_refused_during_an_active_turn(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("restore")
            snapshot = runtime.checkpoint("before")
            runtime._enter_turn()
            try:
                with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_TURN_ACTIVE"):
                    runtime.restore_checkpoint(snapshot)
            finally:
                runtime._leave_turn()
                runtime.stop()

    def test_failed_checkpoint_apply_restores_the_preexisting_worktree(self):
        from fun.runtime import checkpoint_digest

        with TemporaryDirectory() as directory:
            git_env = os.environ.copy()
            git_env["GIT_CONFIG_NOSYSTEM"] = "1"
            git_env["GIT_CONFIG_GLOBAL"] = os.devnull
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True, env=git_env)
            path = Path(directory) / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=directory, check=True, env=git_env)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], cwd=directory, check=True, env=git_env)
            runtime = Runtime(directory, "auto")
            runtime.create_task("restore")
            path.write_text("work that must survive\n", encoding="utf-8")
            malformed = "this is not a patch\n"
            snapshot = {
                "task_id": runtime.task.id,
                "session_id": runtime.session_id,
                "diff": malformed,
                "digest": checkpoint_digest(runtime.session_id, runtime.task.id, malformed),
            }
            with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_RESTORE_FAILED"):
                runtime.restore_checkpoint(snapshot, discard_other_changes=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "work that must survive\n")

    def test_validation_repair_is_bounded_and_evented(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair")
            first = runtime.repair(f"{PYTHON_BIN} -c \"raise SystemExit(1)\"")
            second = runtime.repair(f"{PYTHON_BIN} -c \"raise SystemExit(1)\"")
            blocked = runtime.repair(PYTHON_OK)
            self.assertFalse(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(blocked.text, "REPAIR_BUDGET_EXCEEDED")
            self.assertIn("repair.failed", [event.type for event in runtime.events.list()])
            self.assertIn("repair.blocked", [event.type for event in runtime.events.list()])

    def test_checkpoint_and_validation_emit_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("validate")
            result = runtime.validate("echo pass")
            self.assertTrue(result.ok)
            snapshot = runtime.checkpoint("test")
            self.assertEqual(snapshot["label"], "test")
            self.assertEqual([event.type for event in runtime.events.list()][-6:], ["tool.completed", "plan.step_updated", "validation.completed", "plan.step_updated", "agent.node", "checkpoint.created"])

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


class ProviderErrorIdentityTests(unittest.TestCase):
    def test_a_masked_key_identifies_without_disclosing(self):
        from fun.provider import mask_key

        masked = mask_key("sk-proj-AbCdEf1234567890")
        self.assertNotIn("AbCdEf", masked)
        self.assertTrue(masked.startswith("sk-p"))
        self.assertIn("7890", masked)
        self.assertEqual(mask_key("short"), "?????")

    def test_an_auth_failure_names_the_endpoint_it_was_rejected_by(self):
        from fun.frontends import friendly_error
        from fun.provider import ProviderError, mask_key

        error = ProviderError("PROVIDER_AUTH_FAILED", endpoint="https://api.example.com/v1", key_hint=mask_key("sk-proj-AbCdEf1234567890"))
        message = friendly_error(error, "zh-CN")
        self.assertIn("https://api.example.com/v1", message)
        self.assertNotIn("AbCdEf", message)

    def test_other_failures_are_unchanged(self):
        from fun.frontends import friendly_error
        from fun.provider import ProviderError

        self.assertNotIn("·", friendly_error(ProviderError("PROVIDER_TIMEOUT"), "zh-CN"))


class SmallTalkPlanTests(unittest.TestCase):
    def test_small_talk_still_gets_a_real_plan_in_the_runtime(self):
        from fun.runtime import SMALL_TALK_PLAN, Runtime

        self.assertEqual(tuple(Runtime._initial_plan("你好")), SMALL_TALK_PLAN)

    def test_a_real_request_does_not_get_the_small_talk_plan(self):
        from fun.runtime import SMALL_TALK_PLAN, Runtime

        for goal in ("fix the login redirect", "把 completion 的排序改一下", "what does score() do?"):
            self.assertNotEqual(tuple(Runtime._initial_plan(goal)), SMALL_TALK_PLAN, goal)


class ExecCommandResolutionTests(unittest.TestCase):
    """The wrapper block used to reason about argv[1]; these are the escapes."""

    def _tools(self, directory, mode=None):
        return Tools(directory, Policy(mode or ApprovalMode.AUTO))

    def test_every_spelling_of_a_shell_is_refused(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in (
                "bash -c 'echo pwned'",
                "bash -lc 'echo pwned'",          # the original bypass
                "sh -lc 'echo pwned'",
                "zsh -ic 'echo pwned'",
                "bash --login -c 'echo pwned'",
                "/bin/bash -c 'echo pwned'",
                "BASH -c 'echo pwned'",
                "bash",                           # an interactive shell is no better
                "dash -c 'echo pwned'",
                "pwsh -Command 'echo pwned'",
            ):
                result = tools.exec(command)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.text, "CRITICAL_WRAPPER_BLOCKED", command)

    def test_a_shell_behind_a_transparent_wrapper_is_refused(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in (
                "env bash -c 'echo pwned'",       # the second original bypass
                "env A=1 B=2 bash -c 'echo pwned'",
                "command sh -c 'echo pwned'",
                "nohup bash -c 'echo pwned'",
                "nice -n 5 sh -c 'echo pwned'",
                "env -u PATH bash -c 'echo pwned'",
                "timeout 5 bash -c 'echo pwned'",
                "nohup nice env sh -c 'echo pwned'",
            ):
                result = tools.exec(command)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.text, "CRITICAL_WRAPPER_BLOCKED", command)

    def test_critical_commands_are_seen_through_every_wrapper_layer(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ("env sudo id", "nohup nice env sudo rm -rf /", "env FOO=bar rm -rf build", "command curl https://example.com"):
                result = tools.exec(command)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.text, "CRITICAL_OPERATION_BLOCKED", command)

    def test_programs_that_run_other_programs_are_refused(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ("xargs -a list.txt rm -rf", "npm run build", "node -e 'process.exit(0)'", "ssh host 'rm -rf /'", "docker run alpine sh"):
                result = tools.exec(command)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.text, "CRITICAL_WRAPPER_BLOCKED", command)

    def test_find_exec_and_forced_git_are_critical(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ("find . -exec rm {} ;", "find . -delete", "git push --force origin main", "git reset --hard"):
                self.assertEqual(tools.exec(command).text, "CRITICAL_OPERATION_BLOCKED", command)

    def test_python_inline_code_is_refused_rather_than_keyword_scanned(self):
        """The old guard grepped the code string, which `getattr` defeats."""
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ('python3 -c "import os"', 'python3 -c "getattr(os, chr(114)+\'emove\')(x)"', "python3 -m http.server"):
                self.assertEqual(tools.exec(command).text, "CRITICAL_SCRIPT_BLOCKED", command)

    def test_an_argument_pointing_outside_the_workspace_needs_approval(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(self._tools(directory).exec("cat ../../etc/passwd").text, "CRITICAL_OPERATION_BLOCKED")
            self.assertEqual(self._tools(directory).exec("cat /etc/passwd").text, "CRITICAL_OPERATION_BLOCKED")
            self.assertEqual(self._tools(directory, ApprovalMode.ASK).exec("cat ~/.ssh/id_rsa").text, "APPROVAL_REQUIRED")

    def test_ordinary_commands_still_run(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            self.assertEqual(tools.exec("echo hello").text, "hello")
            self.assertTrue(tools.exec("ls -la").ok)
            Path(directory, "notes.txt").write_text("hi", encoding="utf-8")
            self.assertTrue(tools.exec("cat notes.txt").ok)
            self.assertTrue(tools.exec("env true").ok)

    def test_a_wrapper_with_nothing_after_it_is_refused_not_guessed(self):
        with TemporaryDirectory() as directory:
            for command in ("env", "env -u PATH", "nice -n"):
                self.assertFalse(self._tools(directory).exec(command).ok, command)


class ProtectedNameCaseTests(unittest.TestCase):
    """macOS volumes are case-insensitive; the pattern match was not."""

    def test_a_change_of_case_does_not_walk_past_a_protected_name(self):
        with TemporaryDirectory() as directory:
            guard = WorkspaceGuard(directory)
            for name in (".env", ".ENV", ".Env", "SERVER.PEM", "ID_RSA", ".NPMRC"):
                with self.assertRaises(PolicyError, msg=name):
                    guard.check_name(Path(directory) / name)

    def test_a_protected_directory_component_is_caught_at_any_case(self):
        with TemporaryDirectory() as directory:
            guard = WorkspaceGuard(directory)
            with self.assertRaises(PolicyError):
                guard.check_name(Path(directory) / ".GIT" / "config")

    def test_ordinary_files_are_still_allowed(self):
        with TemporaryDirectory() as directory:
            guard = WorkspaceGuard(directory)
            for name in ("main.py", "README.md", "environment.md"):
                guard.check_name(Path(directory) / name)

    def test_a_protected_symlink_alias_is_checked_before_resolution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public.txt").write_text("secret", encoding="utf-8")
            (root / ".env").symlink_to(root / "public.txt")
            with self.assertRaisesRegex(PolicyError, "PROTECTED_PATH"):
                Tools(directory).read(".env")


class ProviderStreamTests(unittest.TestCase):
    """The SSE reader, driven with the byte splits a real transport produces."""

    class _Response:
        def __init__(self, body: bytes, content_type: str = "text/event-stream", chunk: int = 7):
            self.body, self.headers, self.chunk = body, {"Content-Type": content_type}, chunk
            self.status = 200

        def getcode(self):
            return 200

        def __iter__(self):
            for index in range(0, len(self.body) or 1, self.chunk):
                piece = self.body[index:index + self.chunk]
                if piece:
                    yield piece

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _stream(self, body: bytes, content_type: str = "text/event-stream", chunk: int = 7):
        from fun.provider import ModelConfig, OpenAICompatible

        provider = OpenAICompatible(ModelConfig("https://x.test/v1", "sk-abcd1234", "m"))
        with patch("fun.provider.urllib.request.urlopen", return_value=self._Response(body, content_type, chunk)):
            return list(provider.stream([{"role": "user", "content": "hi"}]))

    def test_nothing_after_done_is_yielded(self):
        """`continue` only ended the line, so a proxy could append events."""
        items = self._stream(b'data: {"a":1}\n\ndata: [DONE]\n\ndata: {"evil":1}\n\n', chunk=4096)
        self.assertEqual([item["a"] for item in items], [1])

    def test_a_multibyte_character_split_across_reads_survives(self):
        payload = 'data: {"t":"你好世界"}\n\ndata: [DONE]\n\n'.encode()
        for chunk in (1, 2, 3, 5, 7):
            items = self._stream(payload, chunk=chunk)
            self.assertEqual(items[0]["t"], "你好世界", chunk)

    def test_a_200_with_a_json_error_body_is_not_an_empty_reply(self):
        from fun.provider import ProviderError

        with self.assertRaises(ProviderError) as caught:
            self._stream(b'{"error":{"message":"Invalid API key"}}', content_type="")
        self.assertEqual(caught.exception.error_tag, "PROVIDER_AUTH_FAILED")

    def test_a_stream_that_says_nothing_at_all_is_reported(self):
        from fun.provider import ProviderError

        with self.assertRaises(ProviderError) as caught:
            self._stream(b"", content_type="")
        self.assertEqual(caught.exception.error_tag, "PROVIDER_EMPTY_STREAM")

    def test_a_json_content_type_is_refused_before_parsing(self):
        from fun.provider import ProviderError

        with self.assertRaises(ProviderError):
            self._stream(b'{"error":"nope"}', content_type="application/json")

    def test_ordinary_streaming_still_works(self):
        body = b'data: {"choices":[{"delta":{"content":"he"}}]}\n\ndata: {"choices":[{"delta":{"content":"llo"}}]}\n\ndata: [DONE]\n\n'
        items = self._stream(body, chunk=9)
        self.assertEqual(len(items), 2)
        self.assertIn("_meta", items[0])

    def test_invalid_utf8_is_a_malformed_provider_event(self):
        from fun.provider import ProviderError

        with self.assertRaises(ProviderError) as caught:
            self._stream(b'data: {"text":"\xff"}\n\n')
        self.assertEqual(caught.exception.error_tag, "PROVIDER_MALFORMED_EVENT")

    def test_payload_limit_covers_the_exact_request_body(self):
        from fun.provider import ModelConfig, OpenAICompatible, ProviderError

        provider = OpenAICompatible(ModelConfig("https://x.test/v1", "sk-test", "m" * 100, max_payload_bytes=80))
        with patch("fun.provider.urllib.request.urlopen") as opened:
            with self.assertRaises(ProviderError) as caught:
                list(provider.stream([{"role": "user", "content": "x"}]))
        self.assertEqual(caught.exception.error_tag, "PROVIDER_PAYLOAD_TOO_LARGE")
        opened.assert_not_called()


class ExploreListingTests(unittest.TestCase):
    def _workspace(self):
        directory = TemporaryDirectory()
        root = Path(directory.name)
        for name in (".env", "id_rsa", "server.pem", "main.py", "README.md"):
            (root / name).write_text("x", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("x", encoding="utf-8")
        (root / "src" / ".env").write_text("x", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
        return directory, root

    def test_protected_files_are_not_named_in_a_listing(self):
        """`read` refused them; `explore` listed them, and a filename is itself
        information — which keys exist, which environments are configured."""
        directory, root = self._workspace()
        with directory:
            listing = Tools(str(root)).explore().text
            for hidden in (".env", "id_rsa", "server.pem"):
                self.assertNotIn(hidden, listing)
            self.assertIn("main.py", listing)
            self.assertIn("src/app.py", listing)

    def test_the_limit_is_reported_rather_than_silently_applied(self):
        directory, root = self._workspace()
        with directory:
            result = Tools(str(root)).explore(limit=3)
            rows = result.text.splitlines()
            self.assertEqual(len(rows), 4)
            self.assertIn("LISTING_TRUNCATED", rows[-1])

    def test_a_small_listing_of_a_large_tree_does_not_walk_the_whole_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2000):
                (root / f"file{index:05d}.txt").write_text("", encoding="utf-8")
            started = time.monotonic()
            rows = Tools(directory).explore(limit=10).text.splitlines()
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(len(rows), 11)

    def test_skipped_directories_are_pruned_not_filtered_afterwards(self):
        directory, root = self._workspace()
        with directory:
            self.assertNotIn("node_modules", Tools(str(root)).explore().text)


class WorkspaceLockContentionTests(unittest.TestCase):
    def test_two_workspaces_can_share_one_state_directory(self):
        """The lock was one file per state dir, and the state dir defaults to
        ~/.fun — so a second project in a second terminal was refused with a
        message naming the first project."""
        with TemporaryDirectory() as state, TemporaryDirectory() as first, TemporaryDirectory() as second:
            one = WorkspaceLock(first, state)
            two = WorkspaceLock(second, state)
            one.acquire()
            two.acquire()
            self.assertTrue(one.held and two.held)
            self.assertNotEqual(one.path, two.path)
            one.release()
            two.release()

    def test_one_workspace_is_still_exclusive_across_state_dirs(self):
        with TemporaryDirectory() as state, TemporaryDirectory() as workspace:
            one = WorkspaceLock(workspace, state)
            one.acquire()
            with self.assertRaises(WorkspaceLockError):
                WorkspaceLock(workspace, state).acquire()
            one.release()

    def test_losing_a_stale_lock_race_raises_the_declared_error_type(self):
        """A replacement race must not leak a bare FileExistsError."""
        with TemporaryDirectory() as directory:
            lock = WorkspaceLock(directory, directory)
            lock.path.write_text('{"pid": 999999999}', encoding="utf-8")
            with patch("fun.lock.os.link", side_effect=FileExistsError(lock.path)):
                with self.assertRaises(WorkspaceLockError):
                    lock.acquire()

    def test_a_stale_lock_is_still_reclaimed(self):
        with TemporaryDirectory() as directory:
            lock = WorkspaceLock(directory, directory)
            lock.path.write_text('{"pid": 999999999}', encoding="utf-8")
            lock.acquire()
            self.assertTrue(lock.held)
            lock.release()

    def test_a_live_lock_is_still_refused(self):
        with TemporaryDirectory() as directory:
            first = WorkspaceLock(directory, directory)
            first.acquire()
            with self.assertRaises(WorkspaceLockError):
                WorkspaceLock(directory, directory).acquire()
            first.release()

    def test_a_second_runtime_in_the_same_process_cannot_adopt_by_pid(self):
        with TemporaryDirectory() as directory:
            first = WorkspaceLock(directory, directory)
            first.acquire()
            second = WorkspaceLock(directory, directory)
            self.assertFalse(second.adopt_if_owned())
            with self.assertRaises(WorkspaceLockError):
                second.acquire()
            first.release()

    def test_invalid_or_partial_lock_metadata_fails_closed(self):
        with TemporaryDirectory() as directory:
            lock = WorkspaceLock(directory, directory)
            lock.path.write_text("", encoding="utf-8")
            with self.assertRaises(WorkspaceLockError):
                lock.acquire()
            lock.path.write_text('{"pid":', encoding="utf-8")
            with self.assertRaises(WorkspaceLockError):
                lock.acquire()

    def test_an_old_owner_cannot_release_a_replacement_lock(self):
        with TemporaryDirectory() as directory:
            first = WorkspaceLock(directory, directory)
            first.acquire()
            replacement = WorkspaceLock(directory, directory)
            replacement.path.write_text(json.dumps({"pid": os.getpid(), "workspace": directory, "owner": replacement.owner}), encoding="utf-8")
            first.release()
            self.assertTrue(replacement.path.exists())
            self.assertEqual(json.loads(replacement.path.read_text(encoding="utf-8"))["owner"], replacement.owner)


class BackgroundEmitRaceTests(unittest.TestCase):
    def test_the_active_task_is_read_once(self):
        """Reading self.task twice let create_task's rollback null it between
        the check and the use, recording a task that had just succeeded as
        failed.  The task attribute is made to vanish *between reads* here, so
        a two-read implementation raises and this test fails."""
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("work")
            task = runtime.task

            class Vanishing:
                """Yields the task once, then None — one read per access."""

                def __init__(self, first):
                    self.values = [first]

                def __get__(self, instance, owner=None):
                    return self.values.pop(0) if self.values else None

                def __set__(self, instance, value):
                    self.values = [value]

            original = Runtime.task if "task" in vars(Runtime) else None
            Runtime.task = Vanishing(task)
            try:
                runtime._emit_background("background.task.completed", "bg-1", {"result": "ok"})
            finally:
                if original is None:
                    del Runtime.task
                else:
                    Runtime.task = original
                runtime.task = task
            types = [event.type for event in runtime.events.list()]
            self.assertEqual(types.count("background.task.completed"), 1)
            runtime.stop()


class ExecGateLayeringTests(unittest.TestCase):
    """The gate must be set by what the command is, and approving must work."""

    def test_a_non_zero_exit_is_a_failed_result(self):
        """Nothing asserted this: every 'failed validation' in the suite came
        from a refusal, not from a command that actually failed."""
        with TemporaryDirectory() as directory:
            self.assertTrue(Tools(directory).exec("true").ok)
            failed = Tools(directory).exec("false")
            self.assertFalse(failed.ok)
            self.assertEqual(failed.exit_code, 1)

    def test_an_unknown_program_asks_instead_of_running(self):
        """The inverted default: 'I don't know this program' means ask."""
        from fun.tools import classify_command
        from fun.policy import Risk

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # Two things are decided, everything else is admitted unknown:
            # inspection runs, provably irreversible operations always ask and
            # are never remembered, and anything unrecognised asks once.
            self.assertEqual(classify_command("echo hi", root).risk, Risk.LOW)
            self.assertEqual(classify_command("pytest -q", root).risk, Risk.HIGH)
            self.assertEqual(classify_command("some-unknown-tool --go", root).risk, Risk.HIGH)
            self.assertEqual(classify_command("rm -rf anything", root).risk, Risk.CRITICAL)

    def test_the_runtime_asks_at_the_risk_the_command_deserves(self):
        asked: list[tuple[str, str]] = []
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "smart", approve=lambda subject, risk: asked.append((subject, risk.value)) or True)
            runtime.create_task("t")
            Path(directory, "victim").mkdir()
            runtime.run_tool("exec", command="rm -rf victim")
            self.assertEqual(asked, [("exec:rm", "critical")])
            runtime.stop()

    def test_approving_a_critical_command_actually_runs_it(self):
        """It used to be refused a second time by the tool after being allowed."""
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "ask", approve=lambda subject, risk: True)
            runtime.create_task("t")
            Path(directory, "victim").mkdir()
            result = runtime.run_tool("exec", command="rm -rf victim")
            self.assertTrue(result.ok, result.text)
            self.assertFalse(Path(directory, "victim").exists())
            runtime.stop()

    def test_always_allow_is_scoped_to_the_program_not_to_the_word_exec(self):
        from fun.tools import classify_command

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(classify_command("awk 1 f.txt", root).program, "awk")
            self.assertEqual(classify_command("rm -rf x", root).program, "rm")


class ExecBypassRoundTwoTests(unittest.TestCase):
    """Bypasses found in the rewritten resolver — a denylist's second failure."""

    def _tools(self, directory):
        return Tools(directory, Policy(ApprovalMode.AUTO))

    def test_an_interpreter_with_a_system_escape_is_refused(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ("awk 'BEGIN{system(\"id\")}'", "gawk 'BEGIN{system(\"id\")}'", "vim -c '!id' /dev/null", "gdb -ex 'shell id'"):
                self.assertFalse(tools.exec(command).ok, command)

    def test_a_namespace_or_lock_wrapper_cannot_launder_a_shell(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ("flock . -c 'id'", "unshare rm -rf v", "setarch -R rm -rf v", "nsenter -t 1 -m id"):
                self.assertFalse(tools.exec(command).ok, command)

    def test_a_wrapper_cannot_relocate_the_child_out_of_the_workspace(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for command in ("env -C / cat etc/hostname", "env --chdir=/etc ls -d passwd"):
                result = tools.exec(command)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.text, "CRITICAL_WRAPPER_BLOCKED", command)

    def test_recursive_delete_is_caught_in_any_case(self):
        with TemporaryDirectory() as directory:
            tools = self._tools(directory)
            for flag in ("-rf", "-Rf", "-fR", "-RF", "-fr", "-r", "-R"):
                Path(directory, "victim").mkdir(exist_ok=True)
                result = tools.exec(f"rm {flag} victim")
                self.assertFalse(result.ok, flag)
                self.assertTrue(Path(directory, "victim").exists(), flag)


class SessionInvariantTests(unittest.TestCase):
    def test_a_new_prompt_cannot_discard_a_pending_recovery(self):
        """run_goal calls create_task directly, which guarded only "running" —
        so a typed prompt silently overwrote a task awaiting recovery."""
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("first")
            runtime.task.status = "recovery_required"
            with self.assertRaises(RuntimeError) as caught:
                runtime.create_task("second")
            self.assertEqual(str(caught.exception), "RECOVERY_REQUIRED")
            runtime.task.status = "running"
            runtime.stop()

    def test_a_new_prompt_cannot_orphan_a_paused_task(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("first")
            runtime.pause()
            with self.assertRaises(RuntimeError) as caught:
                runtime.create_task("second")
            self.assertEqual(str(caught.exception), "TASK_PAUSED")
            runtime.stop()

    def test_those_refusals_tell_the_user_what_to_do(self):
        from fun.commands import Session
        from fun.config import FunConfig
        from fun.frontends import run_goal

        class Frontend:
            locale = "en-US"

            def __init__(self):
                self.said: list[str] = []
                self.statuses: list[str] = []

            def say(self, text): self.said.append(text)
            def status(self, text): self.statuses.append(text)
            def notify(self, text): pass
            def clear(self): pass

        class Provider:
            def stream(self, messages, tools=None):
                yield {"choices": [{"delta": {"content": "hi"}}]}

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=Provider(), state_dir=directory)
            runtime.create_task("first")
            runtime.pause()
            session = Session(runtime, FunConfig(), f"{directory}/config.json")
            frontend = Frontend()
            run_goal(session, frontend, "something new")
            self.assertIn("/resume", frontend.said[-1])
            runtime.stop()

    def test_a_provider_failure_is_recorded_as_itself(self):
        """provider.stream is a generator, so the try around the call could
        never fire and every failure was logged as MALFORMED_RESPONSE."""
        from fun.provider import ProviderError

        class Failing:
            def stream(self, messages, tools=None):
                raise ProviderError("PROVIDER_AUTH_FAILED")
                yield  # pragma: no cover

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=Failing(), state_dir=directory)
            runtime.create_task("t")
            with self.assertRaises(ProviderError):
                runtime.run_model_turn()
            types = [event.type for event in runtime.events.list()]
            self.assertIn("model.failed", types)
            self.assertEqual(runtime.task.model_error["error_tag"], "PROVIDER_AUTH_FAILED")
            failure = [event for event in runtime.events.list() if event.type == "response.failed"][0]
            self.assertEqual(failure.payload["error_tag"], "PROVIDER_AUTH_FAILED")
            runtime.stop()

    def test_a_resumed_session_keeps_its_model_prompt_and_telemetry(self):
        class Telemetry:
            install = "anon"

            def send(self, payload):
                return True

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory, model="gpt-4o", system_prompt="prefer terse")
            runtime.create_task("x")
            session_id = runtime.session_id
            runtime.stop()
            recovered = Runtime.recover(directory, directory, session_id, telemetry=Telemetry(), model="gpt-4o", system_prompt="prefer terse")
            self.assertEqual(recovered.model, "gpt-4o")
            self.assertIn("prefer terse", recovered.system_prompt)
            self.assertIsNotNone(recovered.telemetry)
            recovered.shutdown()

    def test_commands_that_emit_work_between_prompts(self):
        """complete() closes the store, so /diff, /checkpoint and /agent were
        broken for the entire idle time of every session."""
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("t")
            runtime.complete("done")
            snapshot = runtime.checkpoint("view")
            self.assertEqual(snapshot["label"], "view")
            runtime.shutdown()

    def test_an_interrupt_during_a_tool_call_is_not_an_error(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("t")
            runtime.task.plan = ["step"]
            runtime.task.plan_status = ["pending"]
            original = runtime.tools.exec

            def stopping(*args, **kwargs):
                result = original(*args, **kwargs)
                runtime.task.status = "stopped"    # a Ctrl-C landing right here
                return result

            runtime.tools.exec = stopping
            result = runtime.run_tool("exec", command="echo hi")
            self.assertTrue(result.ok)
            runtime.task.status = "running"
            runtime.stop()


class JourneyFirstRunTests(unittest.TestCase):
    """Journey: a new user's first launch."""

    def _run(self, argv, state=None):
        import contextlib
        import io as _io

        from fun import cli

        out, err = _io.StringIO(), _io.StringIO()
        environ = {"FUN_STATE_DIR": state or tempfile.mkdtemp()}
        with patch.dict(os.environ, environ, clear=False), \
             patch("sys.stdin", _io.StringIO("")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_mistyped_workspace_is_an_error_not_a_traceback(self):
        code, _out, err = self._run(["--workspace", "/tmp/does-not-exist-zzz"])
        self.assertEqual(code, 2)
        self.assertIn("workspace does not exist", err)

    def test_a_workspace_that_is_a_file_is_refused(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        code, _out, err = self._run(["--workspace", path])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_a_state_dir_that_is_a_file_is_refused(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        with TemporaryDirectory() as workspace:
            code, _out, err = self._run(["--workspace", workspace], state=path)
        self.assertEqual(code, 2)
        self.assertIn("state directory", err)

    def test_resuming_an_unknown_session_says_so(self):
        """It used to hand back a blank session, so the user's previous task
        looked as though it had vanished."""
        with TemporaryDirectory() as workspace:
            code, _out, err = self._run(["--workspace", workspace, "--resume-session", "ses_made_up"])
        self.assertEqual(code, 2)
        self.assertIn("no such session", err)

    def test_recover_refuses_a_session_with_no_events(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError) as caught:
                Runtime.recover(directory, directory, "ses_made_up")
            self.assertIn("UNKNOWN_SESSION", str(caught.exception))


class JourneyTurnIntegrityTests(unittest.TestCase):
    def test_every_declared_tool_call_gets_a_reply(self):
        """An approval callback that raises used to leave the assistant's
        tool_calls message with no replies, so every later request 400ed."""
        def exploding(subject, risk):
            raise RuntimeError("callback failed")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "ask", approve=exploding, state_dir=directory)
            runtime.create_task("t")
            calls = [
                {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": json.dumps({"command": "rm -rf x"})}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}},
            ]
            runtime.task.messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            with self.assertRaises(RuntimeError):
                runtime.execute_tool_calls(calls)
            answered = {item["tool_call_id"] for item in runtime.task.messages if item.get("role") == "tool"}
            self.assertEqual(answered, {"c1", "c2"})
            runtime.task.status = "running"
            runtime.stop()

    def test_a_stop_between_tool_calls_still_answers_them(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("t")
            calls = [{"id": f"c{index}", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "missing.py"})}} for index in range(3)]
            runtime.task.messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            runtime.task.status = "stopped"
            with self.assertRaises(RuntimeError):
                runtime.execute_tool_calls(calls)
            answered = {item["tool_call_id"] for item in runtime.task.messages if item.get("role") == "tool"}
            self.assertEqual(answered, {"c0", "c1", "c2"})
            runtime.close()


class JourneyLongSessionTests(unittest.TestCase):
    def test_rendering_cost_does_not_grow_with_history(self):
        from fun.ui.state import UiState
        from fun.ui.theme import Theme as T

        state = UiState(theme=T(mode="none", locale="en-US"))
        for _ in range(60):
            state.add_user("a question with some detail " * 4)
            state.add_assistant("an answer with rather more detail " * 8)
        state.compose(100, 30)
        started = time.monotonic()
        state.compose(100, 30)
        small = time.monotonic() - started
        for _ in range(600):
            state.add_user("a question with some detail " * 4)
            state.add_assistant("an answer with rather more detail " * 8)
        state.compose(100, 30)
        started = time.monotonic()
        state.compose(100, 30)
        large = time.monotonic() - started
        self.assertLess(large, max(0.05, small * 6), f"{small * 1000:.1f}ms -> {large * 1000:.1f}ms")

    def test_a_long_history_can_still_be_scrolled_to_the_top(self):
        from fun.ui.state import UiState
        from fun.ui.theme import Theme as T

        state = UiState(theme=T(mode="none", locale="en-US"))
        for index in range(300):
            state.add_user(f"message {index}")
        state.scroll(-99999)
        frame = "\n".join(state.compose(100, 30))
        self.assertIn("message 0", frame)
        state.scroll(99999)
        self.assertEqual(state.scroll_offset, 0)
        self.assertIn("message 299", "\n".join(state.compose(100, 30)))


class ReviewFindingsTests(unittest.TestCase):
    """Findings raised in review, each reproduced before it was fixed."""

    def test_always_allow_never_covers_a_critical_command(self):
        """Approving `rm -rf build` once remembered `exec:rm` for the session,
        so the next `rm -rf` — of anything — ran with no prompt."""
        from fun.tools import classify_command

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(classify_command("rm -rf build", root).risk.value, "critical")
            self.assertEqual(classify_command("rm -rf important", root).risk.value, "critical")

    def test_nothing_that_can_run_other_code_is_called_benign(self):
        """The benign set is the one claim this code makes about safety, so it
        holds only programs that read and report.  `git` is deliberately absent:
        aliases and hooks make it a launcher."""
        from fun.tools import BENIGN

        for program in ("pytest", "pip", "gcc", "java", "cargo", "cmake", "git", "make", "npm", "docker"):
            self.assertNotIn(program, BENIGN, program)
        for program in ("ls", "cat", "grep", "wc", "diff", "head"):
            self.assertIn(program, BENIGN, program)

    def test_a_local_executable_cannot_impersonate_a_benign_basename(self):
        from fun.policy import ApprovalMode, Policy, Risk
        from fun.tools import classify_command

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fake = root / "cat"
            fake.write_text("#!/bin/sh\necho PWNED > marker\n", encoding="utf-8")
            fake.chmod(0o755)
            for command in ("./cat", "env ./cat"):
                self.assertEqual(classify_command(command, root).risk, Risk.HIGH, command)
                result = Tools(root, Policy(mode=ApprovalMode.AUTO)).exec(command)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.text, "APPROVAL_REQUIRED")
            with patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ.get("PATH", "")}):
                self.assertEqual(classify_command("cat", root).risk, Risk.HIGH)
                self.assertEqual(Tools(root, Policy(mode=ApprovalMode.AUTO)).exec("cat").text, "APPROVAL_REQUIRED")
            self.assertFalse((root / "marker").exists())

    def test_public_exec_has_no_forgeable_approval_flag(self):
        import inspect

        self.assertNotIn("approved", inspect.signature(Tools.exec).parameters)
        with TemporaryDirectory() as directory:
            victim = Path(directory) / "victim"
            victim.mkdir()
            with self.assertRaises(TypeError):
                Tools(directory, Policy(mode=ApprovalMode.AUTO)).exec("rm -rf victim", approved=True)
            self.assertTrue(victim.exists())

    def test_an_unfamiliar_program_asks_once_even_in_auto_mode(self):
        """A gap in this tool's knowledge must not fail open in the mode people
        actually leave it in."""
        from fun.policy import ApprovalMode, Policy, Risk
        from fun.tools import classify_command

        with TemporaryDirectory() as directory:
            plan = classify_command("pytest -q", Path(directory).resolve())
            self.assertEqual(plan.risk, Risk.HIGH)
            for mode in (ApprovalMode.AUTO, ApprovalMode.SMART, ApprovalMode.ASK):
                self.assertTrue(Policy(mode=mode).requires_approval(plan.risk), mode)
            self.assertFalse(Policy(mode=ApprovalMode.AUTO).requires_approval(Risk.LOW))

    def test_an_unfamiliar_program_can_be_remembered_but_a_destructive_one_cannot(self):
        from fun.policy import Risk
        from fun.tools import classify_command

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertNotEqual(classify_command("pytest -q", root).risk, Risk.CRITICAL)
            self.assertEqual(classify_command("rm -rf build", root).risk, Risk.CRITICAL)
            self.assertEqual(classify_command("git reset --hard", root).risk, Risk.CRITICAL)
            self.assertEqual(classify_command("sudo id", root).risk, Risk.CRITICAL)

    def test_check_name_outside_the_workspace_raises_policy_error(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PolicyError, "PATH_OUTSIDE_WORKSPACE"):
                WorkspaceGuard(directory).check_name(Path("/etc/passwd"))

    def test_an_unknown_agent_mode_is_refused_rather_than_permissive(self):
        """read_only tests membership, so a typo granted edit and exec."""
        with self.assertRaisesRegex(PolicyError, "UNKNOWN_AGENT_MODE"):
            Policy(agent_mode="Reveiw")

    def test_stop_does_not_release_the_lock_under_a_running_turn(self):
        import threading

        class Slow:
            def stream(self, messages, tools=None):
                time.sleep(0.4)
                yield {"choices": [{"delta": {"content": "ok"}}]}

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=Slow(), state_dir=directory)
            runtime.create_task("t")
            lock_path = runtime.lock.path
            worker = threading.Thread(target=lambda: self._swallow(runtime.run_model_turn), daemon=True)
            worker.start()
            time.sleep(0.15)
            runtime.stop()
            self.assertTrue(lock_path.exists(), "the workspace was handed away mid-turn")
            worker.join(3)
            time.sleep(0.2)
            self.assertFalse(lock_path.exists())

    def test_a_shutdown_does_not_close_the_store_under_a_running_turn(self):
        import threading

        class Slow:
            def stream(self, messages, tools=None):
                time.sleep(0.4)
                yield {"choices": [{"delta": {"content": "ok"}}]}

        errors: list[str] = []
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=Slow(), state_dir=directory)
            runtime.create_task("t")

            def turn():
                try:
                    runtime.run_model_turn()
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")

            worker = threading.Thread(target=turn, daemon=True)
            worker.start()
            time.sleep(0.15)
            runtime.close(shutdown=True)
            worker.join(3)
            self.assertEqual(errors, [])

    def test_a_recorded_event_cannot_change_afterwards(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("goal")
            created = [event for event in runtime.events.list() if event.type == "task.created"][0]
            before = len(created.payload["messages"])
            runtime.task.messages.append({"role": "assistant", "content": "later"})
            runtime.task.messages[0]["content"] = "mutated"
            self.assertEqual(len(created.payload["messages"]), before)
            self.assertNotEqual(created.payload["messages"][0]["content"], "mutated")
            runtime.stop()

    def test_a_tool_that_raises_leaves_no_pending_call(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("t")
            runtime.task.plan = ["step"]
            runtime.task.plan_status = ["pending"]

            def exploding(**kwargs):
                raise OSError("disk on fire")

            runtime.tools.read = exploding
            with self.assertRaises(OSError):
                runtime.run_tool("read", path="a.py")
            self.assertIsNone(runtime.task.pending_tool)
            self.assertEqual(runtime.task.plan_status[0], "blocked")
            runtime.stop()

    def test_every_task_reports_telemetry_not_only_the_first(self):
        class Counting:
            install = "anon"

            def __init__(self):
                self.calls = 0

            def send(self, payload):
                self.calls += 1
                return True

        telemetry = Counting()
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", telemetry=telemetry, state_dir=directory)
            for _ in range(3):
                runtime.create_task("t")
                runtime.complete("done")
            self.assertEqual(telemetry.calls, 3)
            runtime.shutdown()

    def test_streamed_tool_call_fragments_are_validated(self):
        from fun.runtime import valid_tool_calls

        self.assertEqual(valid_tool_calls([{"id": "", "name": "read", "arguments": "{}"}]), [])
        self.assertEqual(valid_tool_calls([{"id": "c1", "name": "", "arguments": "{}"}]), [])
        self.assertEqual(valid_tool_calls([{"id": "c1", "name": "read", "arguments": None}]), [])
        doubled = valid_tool_calls([
            {"id": "c1", "name": "read", "arguments": "{}"},
            {"id": "c1", "name": "edit", "arguments": "{}"},
        ])
        self.assertEqual(len(doubled), 1)
        self.assertEqual(doubled[0]["function"]["name"], "read")

    def test_a_truncated_escape_sequence_does_not_block_the_ui(self):
        from fun.ui.input import read_key

        for payload in (b"\x1b[", b"\x1b", b"\x1bO", b"\x1b[1;"):
            reader, _writer = os.pipe()
            os.write(_writer, payload)
            started = time.monotonic()
            self.assertEqual(read_key(reader), "escape", payload)
            self.assertLess(time.monotonic() - started, 1.0, payload)

    def test_a_paste_without_a_terminator_is_bounded(self):
        from fun.ui.input import paste_text, read_key

        reader, writer = os.pipe()
        os.write(writer, "\x1b[200~no terminator".encode())
        started = time.monotonic()
        key = read_key(reader)
        self.assertEqual(paste_text(key), "no terminator")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_the_background_cap_is_checked_and_taken_together(self):
        import threading

        from fun.background import BackgroundTaskManager

        manager = BackgroundTaskManager(lambda *args: None)
        release = threading.Event()
        refused = []

        def worker(goal, cancel):
            release.wait(2.0)

        def spawn():
            try:
                manager.spawn("x", worker)
            except RuntimeError:
                refused.append(True)

        threads = [threading.Thread(target=spawn) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        live = [task for task in manager.list() if task.status in {"created", "running"}]
        self.assertLessEqual(len(live), manager.MAX_LIVE)
        release.set()
        manager.close()

    def _swallow(self, call):
        try:
            call()
        except Exception:
            return
