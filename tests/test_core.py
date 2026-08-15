import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.events import Event, EventStore
from fun.policy import ApprovalMode, Policy, PolicyError
from fun.renderer import TerminalRenderer
from fun.runtime import Runtime
from fun.tools import Tools, file_hash
from fun.usage import Usage


def runtime_usage_summary():
    return Usage().summary()


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

    def test_runtime_recovers_agent_state_from_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("recover state")
            runtime._node("tools.executing")
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "tools.executing")
            recovered.stop()

    def test_recovery_required_blocks_until_acknowledged(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("recover side effect")
            runtime.emit("tool.executing", runtime.task.id, call_id="call_1", name="exec", arguments={"command": "echo hi"})
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

    def test_approval_pending_replays_arguments(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("approval")
            runtime.emit("approval.pending", runtime.task.id, call_id="call_2", name="edit", risk="medium", arguments={"path": "a.txt"})
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.status, "recovery_required")
            self.assertEqual(recovered.recovery_summary()["arguments"]["path"], "a.txt")
            recovered.acknowledge_recovery("discard")
            self.assertIsNone(recovered.task.pending_tool)
            self.assertIn("recovery.discarded", [event.type for event in recovered.events.list()])
            replayed = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(replayed.task.status, "running")
            self.assertIsNone(replayed.task.pending_tool)
            replayed.stop()

    def test_runtime_recovers_task_from_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            task = runtime.create_task("recover me")
            runtime.pause()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.id, task.id)
            self.assertEqual(recovered.task.status, "paused")
            recovered.stop()

    def test_failed_task_preserves_failure_fact(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.create_task("will fail")
            runtime.fail("provider unavailable")
            self.assertEqual(runtime.task.agent_state, "failed")
            self.assertIn("task.failed", [event.type for event in runtime.events.list()])
            self.assertEqual(runtime.events.list()[-2].payload["reason"], "provider unavailable")

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

    def test_runtime_can_persist_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, state_dir=directory)
            runtime.create_task("persist")
            self.assertGreaterEqual(len(runtime.events.list()), 2)
            self.assertTrue((Path(directory) / "events.db").exists())

    def test_repl_control_methods_emit_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.create_task("controls")
            runtime.pause()
            runtime.resume()
            runtime.checkpoint()
            runtime.stop()
            self.assertEqual([event.type for event in runtime.events.list()][-4:], ["task.paused", "task.resumed", "checkpoint.created", "task.stopped"])

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

    def test_agent_node_event_is_renderable(self):
        renderer = TerminalRenderer(color=False)
        self.assertTrue(renderer.event("agent.node", {"node": "validation.started"}).startswith("◌"))
        self.assertTrue(renderer.event("recovery.required", {"reason": "tool.executing"}).startswith("×"))
        self.assertTrue(renderer.event("approval.rejected", {"name": "edit"}).startswith("×"))
        self.assertIn("✓ inspect", renderer.plan(["inspect"], ["done"]))
        self.assertIn("× repair", renderer.plan(["repair"], ["blocked"]))

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

    def test_exec_limits_output_and_timeout(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec("python3 -c \"print('x' * 300000)\"")
            self.assertTrue(result.ok)
            self.assertIn("OUTPUT_TRUNCATED", result.text)
            timeout = Tools(directory).exec("python3 -c \"import time; time.sleep(2)\"", timeout=0.05)
            self.assertFalse(timeout.ok)
            self.assertIn("COMMAND_TIMEOUT", timeout.text)

    def test_exec_blocks_critical_argv_forms(self):
        with TemporaryDirectory() as directory:
            for command in ("rm -rf build", "git reset --hard HEAD", "git clean -fd", "sudo echo nope", "curl https://example.com"):
                result = Tools(directory, Policy(ApprovalMode.ASK)).exec(command)
                self.assertFalse(result.ok)
                self.assertEqual(result.text, "APPROVAL_REQUIRED")

    def test_exec_does_not_invoke_a_shell(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "injected"
            result = Tools(directory).exec(f"echo safe; touch {marker}")
            self.assertTrue(result.ok)
            self.assertFalse(marker.exists())
            self.assertEqual(result.text, "safe; touch " + str(marker))

    def test_exec_rejects_invalid_command_syntax(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec("python3 -c 'unterminated")
            self.assertFalse(result.ok)
            self.assertIn("INVALID_COMMAND", result.text)

    def test_exec_runs_inside_workspace(self):
        with TemporaryDirectory() as directory:
            result = Tools(directory).exec("python3 -c \"print('ok')\"")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "ok")

    def test_renderer_is_single_column_and_symbol_driven(self):
        renderer = TerminalRenderer(color=False)
        self.assertIn("◇ PLAN", renderer.plan(["inspect files"]))
        self.assertTrue(renderer.activity("reading").startswith("◌"))
        self.assertTrue(renderer.finding("risk").startswith("!"))
        self.assertTrue(renderer.event("tool.completed", {"text": "ok"}).startswith("✓"))
        self.assertIn("ttft", runtime_usage_summary())

    def test_checkpoint_restore_reapplies_git_diff(self):
        with TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
            path = Path(directory) / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=directory, check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], cwd=directory, check=True)
            runtime = Runtime(directory, "auto")
            runtime.create_task("restore")
            path.write_text("two\n", encoding="utf-8")
            snapshot = runtime.checkpoint("before")
            path.write_text("three\n", encoding="utf-8")
            runtime.restore_checkpoint(snapshot)
            self.assertEqual(path.read_text(encoding="utf-8"), "two\n")

    def test_validation_repair_is_bounded_and_evented(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("repair")
            first = runtime.repair("python3 -c \"raise SystemExit(1)\"")
            second = runtime.repair("python3 -c \"raise SystemExit(1)\"")
            blocked = runtime.repair("true")
            self.assertFalse(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(blocked.text, "REPAIR_BUDGET_EXCEEDED")
            self.assertIn("repair.failed", [event.type for event in runtime.events.list()])
            self.assertIn("repair.blocked", [event.type for event in runtime.events.list()])

    def test_checkpoint_and_validation_emit_events(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("validate")
            result = runtime.validate("python3 -c \"print('pass')\"")
            self.assertTrue(result.ok)
            snapshot = runtime.checkpoint("test")
            self.assertEqual(snapshot["label"], "test")
            self.assertEqual([event.type for event in runtime.events.list()][-5:], ["tool.completed", "plan.step_updated", "plan.step_updated", "validation.completed", "checkpoint.created"])

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
