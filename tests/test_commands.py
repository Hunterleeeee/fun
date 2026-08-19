import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from typing import Any, Callable, Sequence

from fun.commands import REGISTRY, Session, command_names, dispatch, resolve_command_prefix
from fun.config import FunConfig
from fun.runtime import Runtime


class RecordingFrontend:
    """A Frontend that records everything instead of drawing it."""

    locale = "en-US"

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.said: list[str] = []
        self.notified: list[str] = []
        self.statuses: list[str] = []
        self.quit_called = False
        self.cleared = False
        self.answers = answers or {}

    def say(self, text: str) -> None:
        self.said.append(text)

    def notify(self, text: str) -> None:
        self.notified.append(text)

    def status(self, text: str) -> None:
        self.statuses.append(text)

    def clear(self) -> None:
        self.cleared = True

    def form(self, title: str, fields: Sequence[Any], callback: Callable[[dict[str, str] | None], None]) -> None:
        callback(self.answers.get("form"))

    def select(self, title: str, options: Sequence[str], callback: Callable[[str | None], None], loader: Callable[[], list[str]] | None = None) -> None:
        callback(self.answers.get("select"))

    def edit(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        callback(self.answers.get("edit"))

    def quit(self) -> None:
        self.quit_called = True

    @property
    def text(self) -> str:
        return "\n".join(self.said + self.notified + self.statuses)


class CommandRegistryTests(unittest.TestCase):
    def test_prefix_resolution_matches_the_old_behaviour(self):
        commands = {"/model", "/status", "/stop"}
        self.assertEqual(resolve_command_prefix("/model", commands), ("/model", []))
        self.assertEqual(resolve_command_prefix("/mod", commands), ("/model", []))
        self.assertEqual(resolve_command_prefix("/st", commands), (None, ["/status", "/stop"]))
        self.assertEqual(resolve_command_prefix("hello", commands), ("hello", []))
        self.assertEqual(resolve_command_prefix("/unknown", commands), (None, []))

    def test_previously_tui_only_commands_are_registered(self):
        for name in ("/logout", "/diff", "/checkpoint", "/goal", "/prompt", "/recover", "/cancel", "/plan", "/usage"):
            self.assertIn(name, REGISTRY, name)

    def test_every_command_documents_itself(self):
        for name, command in REGISTRY.items():
            self.assertTrue(command.summary.strip(), name)

    def test_command_names_are_sorted_and_prefixed(self):
        names = command_names()
        self.assertEqual(names, sorted(names))
        self.assertTrue(all(name.startswith("/") for name in names))


class CommandDispatchTests(unittest.TestCase):
    def _session(self, directory: str):
        runtime = Runtime(directory, "auto", state_dir=directory)
        config = FunConfig()
        return Session(runtime, config, f"{directory}/config.json"), runtime

    def test_plain_text_is_not_treated_as_a_command(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            self.assertFalse(dispatch("fix the login bug", session, frontend))
            runtime.stop()

    def test_unknown_command_is_reported_not_executed(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            self.assertTrue(dispatch("/definitely-not-real", session, frontend))
            self.assertTrue(frontend.said)
            runtime.stop()

    def test_ambiguous_prefix_lists_the_candidates(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/st", session, frontend)
            self.assertIn("/status", frontend.text)
            self.assertIn("/stop", frontend.text)
            runtime.stop()

    def test_help_lists_every_command(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/help", session, frontend)
            for name in ("/status", "/diff", "/logout"):
                self.assertIn(name, frontend.text)
            runtime.stop()

    def test_status_reports_session_and_usage(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            runtime.create_task("inspect the workspace")
            frontend = RecordingFrontend()
            dispatch("/status", session, frontend)
            self.assertIn(runtime.session_id, frontend.text)
            runtime.stop()

    def test_plan_renders_step_status(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            runtime.create_task("fix the failing login test")
            frontend = RecordingFrontend()
            dispatch("/plan", session, frontend)
            self.assertIn("inspect workspace", frontend.text)
            runtime.stop()

    def test_goal_without_argument_reports_the_current_goal(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            runtime.create_task("inspect the workspace")
            frontend = RecordingFrontend()
            dispatch("/goal", session, frontend)
            self.assertIn("inspect the workspace", frontend.text)
            runtime.stop()

    def test_permissions_changes_the_approval_mode(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/permissions ask", session, frontend)
            self.assertEqual(runtime.policy.mode, "ask")
            self.assertEqual(session.config.approval, "ask")
            runtime.stop()

    def test_prompt_argument_persists_a_safe_preference(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/prompt always run the focused tests", session, frontend)
            self.assertIn("always run the focused tests", runtime.system_prompt)
            self.assertIn("safety-first terminal coding agent", runtime.system_prompt)
            self.assertEqual(session.config.system_prompt, "always run the focused tests")
            runtime.stop()

    def test_pause_and_resume_move_the_task(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            runtime.create_task("inspect the workspace")
            frontend = RecordingFrontend()
            dispatch("/pause", session, frontend)
            self.assertEqual(runtime.task.status, "paused")
            dispatch("/resume", session, frontend)
            self.assertEqual(runtime.task.status, "running")
            runtime.stop()

    def test_cancel_requires_an_identifier(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/cancel", session, frontend)
            self.assertIn("Usage", frontend.text)
            runtime.stop()

    def test_recover_reports_when_nothing_needs_recovery(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            runtime.create_task("inspect the workspace")
            frontend = RecordingFrontend()
            dispatch("/recover", session, frontend)
            self.assertIn("RECOVERY_NOT_REQUIRED", frontend.text)
            runtime.stop()

    def test_exit_asks_the_frontend_to_quit(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/exit", session, frontend)
            self.assertTrue(frontend.quit_called)
            runtime.stop()

    def test_logout_clears_credentials_and_goes_offline(self):
        # Patched: unpatched, this deletes the developer's real keychain entry.
        with patch("fun.config._keychain_delete", return_value=True), patch("fun.config._keychain_get", return_value=""), TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            session.base_url, session.api_key, session.model = "https://x/v1", "k", "m"
            frontend = RecordingFrontend()
            dispatch("/logout", session, frontend)
            self.assertEqual(session.api_key, "")
            self.assertIsNone(runtime.provider)
            runtime.stop()

    def test_diff_reports_an_empty_worktree(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            runtime.create_task("inspect the workspace")
            frontend = RecordingFrontend()
            dispatch("/diff", session, frontend)
            self.assertTrue(frontend.said)
            runtime.stop()


if __name__ == "__main__":
    unittest.main()


class ToolCallCorrelationTests(unittest.TestCase):
    """One model tool call must render as one card, not two."""

    class _StubProvider:
        def __init__(self):
            self.calls = 0

        def stream(self, messages, tools=None):
            import json as _json

            self.calls += 1
            if self.calls == 1:
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "explore", "arguments": _json.dumps({"path": "."})}}]}}]}
            else:
                yield {"choices": [{"delta": {"content": "done"}}]}

    def test_all_events_of_one_call_share_the_model_call_id(self):
        from fun.runtime import Runtime

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", self._StubProvider(), state_dir=directory)
            runtime.create_task("describe this workspace")
            runtime.run_model_turn()
            ids = {event.payload.get("call_id") for event in runtime.events.list() if event.type in {"tool.requested", "tool.executing", "tool.completed"}}
            self.assertEqual(ids, {"call_1"})
            runtime.stop()

    def test_the_ui_creates_a_single_card_for_a_single_call(self):
        from fun.frontends import AppFrontend, run_goal
        from fun.runtime import Runtime
        from fun.ui.app import App
        from fun.ui.stream import StreamSurface
        from fun.ui.theme import Theme
        import io as _io

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", self._StubProvider(), state_dir=directory)
            session = Session(runtime, FunConfig(), f"{directory}/config.json")
            app = App(StreamSurface(_io.StringIO()), theme=Theme(mode="none"))
            run_goal(session, AppFrontend(app, "en-US"), "describe this workspace", on_status=lambda kind, payload: app.post("tool", (kind, payload)))
            app.paint()
            self.assertEqual(len(app.state.tools), 1)
            runtime.stop()

    def test_the_streaming_surface_never_clears_the_screen(self):
        from fun.frontends import AppFrontend, run_goal
        from fun.runtime import Runtime
        from fun.ui.app import App
        from fun.ui.stream import StreamSurface
        from fun.ui.theme import Theme
        import io as _io

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", self._StubProvider(), state_dir=directory)
            session = Session(runtime, FunConfig(), f"{directory}/config.json")
            buffer = _io.StringIO()
            app = App(StreamSurface(buffer), theme=Theme(mode="none"))
            app.post("user", "describe this workspace")
            app.paint()
            run_goal(session, AppFrontend(app, "en-US"), "describe this workspace", on_text=lambda chunk: app.post("assistant", chunk), on_status=lambda kind, payload: app.post("tool", (kind, payload)))
            app.paint()
            self.assertNotIn("\033[2J", buffer.getvalue())
            self.assertIn("describe this workspace", buffer.getvalue())
            runtime.stop()


class CrashPathTests(unittest.TestCase):
    """Four paths that were guaranteed to fail rather than merely likely to."""

    def test_the_non_tty_entry_point_starts(self):
        """`banner` was called but never imported: NameError on the first line."""
        import subprocess
        import sys

        result = subprocess.run([sys.executable, "-m", "fun"], input="", capture_output=True, text=True, timeout=60)
        self.assertNotIn("NameError", result.stderr)
        self.assertEqual(result.returncode, 0)

    def test_setting_the_approval_mode_leaves_an_enum_behind(self):
        from fun.policy import ApprovalMode, Policy

        policy = Policy()
        self.assertEqual(policy.set_mode("auto"), ApprovalMode.AUTO)
        self.assertEqual(policy.mode.value, "auto")   # three call sites read this
        self.assertEqual(Policy(mode="ask").mode, ApprovalMode.ASK)
        with self.assertRaises(ValueError):
            policy.set_mode("nonsense")

    def test_an_unambiguous_prefix_really_clears(self):
        """/cle resolved to /clear and said "cleared" while clearing nothing."""
        with TemporaryDirectory() as directory:
            session = Session(Runtime(directory, "auto"), FunConfig(), f"{directory}/config.json")
            frontend = RecordingFrontend()
            self.assertTrue(dispatch("/cle", session, frontend))
            self.assertTrue(frontend.cleared)
            self.assertIn("cleared", frontend.statuses)

    def test_a_command_may_open_a_modal_from_a_modal_callback(self):
        import io

        from fun.ui.app import App
        from fun.ui.stream import StreamSurface
        from fun.ui.theme import Theme

        app = App(StreamSurface(io.StringIO()), theme=Theme(mode="none"))

        def submit(text: str) -> None:
            if text == "/config":
                app.open_form("Provider", ["base_url", ("api_key", True)], lambda values: None)

        app._submit = submit
        app._handle_key("palette", submit)
        for char in "config":
            app._handle_key(char, submit)
        self.assertEqual(app.modal.rows[app.modal.index].command, "/config")
        app._handle_key("enter", submit)
        self.assertIsNotNone(app.modal, "the form opened by the callback was thrown away")
        self.assertEqual(app.modal.kind, "fields")

    def test_always_allow_is_remembered_for_the_session(self):
        """The `a` key allowed one call and then asked again immediately."""
        from fun.ui.app import ApprovalRequest

        request = ApprovalRequest("exec", "medium")
        request.answer = "a"
        self.assertTrue(request.allowed)
        self.assertTrue(request.remembered)
        once = ApprovalRequest("exec", "medium")
        once.answer = "y"
        self.assertTrue(once.allowed)
        self.assertFalse(once.remembered)

    def test_an_explicit_no_is_not_truthy_permission(self):
        from fun.cli import _approval_allowed

        self.assertFalse(_approval_allowed("no"))
        self.assertFalse(_approval_allowed(""))
        self.assertTrue(_approval_allowed("yes"))
        self.assertTrue(_approval_allowed("always"))

    def test_the_approval_callback_distinguishes_once_from_always(self):
        import io
        import threading

        from fun.ui.app import App
        from fun.ui.stream import StreamSurface
        from fun.ui.theme import Theme

        app = App(StreamSurface(io.StringIO()), theme=Theme(mode="none"))
        answers: list[str] = []
        waiter = threading.Thread(target=lambda: answers.append(app.request_approval("exec", "medium")))
        waiter.start()
        _kind, request = app.events.get(timeout=2)
        request.answer = "a"
        request.done.set()
        waiter.join(2)
        self.assertEqual(answers, ["always"])
