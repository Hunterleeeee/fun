import re
import time
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
        self.selects: list[dict[str, Any]] = []
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

    def select(self, title: str, options: Sequence[str], callback: Callable[[Any], None], loader: Callable[[], list[str]] | None = None, multi: bool = False, chosen: Sequence[str] = ()) -> None:
        self.selects.append({"title": title, "options": list(options), "multi": multi, "chosen": list(chosen), "loader": loader})
        answer = self.answers.get("select")
        if multi and not isinstance(answer, list):
            answer = [answer] if answer else []
        callback(answer)

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


class ModelPickerTests(unittest.TestCase):
    """The model is chosen from the provider's list, not typed from memory."""

    def _session(self, directory: str):
        runtime = Runtime(directory, "auto", state_dir=directory)
        return Session(runtime, FunConfig(), f"{directory}/config.json"), runtime

    def test_config_asks_for_the_model_in_a_picker_not_a_text_field(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend({"form": {"base_url": "https://x/v1", "api_key": "sk-a"}, "select": ["m-large", "m-small"]})
            with patch("fun.config._keychain_set", return_value=False), patch("fun.config._keychain_get", return_value=""):
                dispatch("/config", session, frontend)
            # The credentials form no longer carries a "model" field at all.
            self.assertEqual(len(frontend.selects), 1)
            self.assertTrue(frontend.selects[0]["multi"])
            self.assertIsNotNone(frontend.selects[0]["loader"], "the list has to come from the provider")
            self.assertEqual(session.model, "m-large")
            self.assertEqual(session.models, ["m-large", "m-small"])
            self.assertEqual(FunConfig.load(session.config_path).models, ["m-large", "m-small"])
            runtime.stop()

    def test_a_cancelled_picker_leaves_the_saved_model_alone(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            session.base_url, session.api_key, session.model = "https://x/v1", "sk-a", "kept"
            frontend = RecordingFrontend({"select": None})
            with patch("fun.config._keychain_set", return_value=False), patch("fun.config._keychain_get", return_value=""):
                dispatch("/model", session, frontend)
            self.assertEqual(session.model, "kept")
            runtime.stop()

    def test_the_picker_offers_the_models_already_chosen(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            session.base_url, session.api_key = "https://x/v1", "sk-a"
            session.models = ["m-large", "m-small"]
            dispatch("/model", session, RecordingFrontend({"select": None}))
            runtime.stop()

    def test_naming_a_model_directly_adds_it_to_the_shortlist(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            session.base_url, session.api_key = "https://x/v1", "sk-a"
            with patch("fun.config._keychain_set", return_value=False), patch("fun.config._keychain_get", return_value=""):
                dispatch("/model m-tiny", session, RecordingFrontend())
            self.assertEqual(session.model, "m-tiny")
            self.assertIn("m-tiny", session.models)
            runtime.stop()

    def test_model_without_credentials_says_so_instead_of_opening_an_empty_list(self):
        with TemporaryDirectory() as directory:
            session, runtime = self._session(directory)
            frontend = RecordingFrontend()
            dispatch("/model", session, frontend)
            self.assertEqual(frontend.selects, [])
            self.assertIn("provider", frontend.text.lower())
            runtime.stop()


class ApprovalGateTests(unittest.TestCase):
    """Denying has to deny.  This is the one answer that must never be guessed."""

    def _gate(self, answer, remembered=None, interactive=True):
        from fun.cli import approval_gate

        class FakeApp:
            asked = []

            def request_approval(self, name, risk, arguments=None):
                FakeApp.asked.append((name, str(getattr(risk, "value", risk))))
                return answer

        FakeApp.asked = []
        holder = {"app": FakeApp()}
        gate = approval_gate(remembered if remembered is not None else set(), holder, "en-US", interactive=interactive)
        return gate, FakeApp

    def test_no_means_no(self):
        # `request_approval` returns the *strings* "yes"/"no"/"always"; the
        # caller used to do bool(answer), and bool("no") is True — so pressing n
        # on `rm -rf build` ran it, and the card showed a green tick afterwards.
        with patch("fun.cli.sys.stdin.isatty", return_value=True):
            for answer in ("no", "", None):
                gate, _ = self._gate(answer)
                self.assertFalse(gate("exec:rm", "critical"), repr(answer))

    def test_yes_and_always_are_the_only_answers_that_run_it(self):
        from fun.cli import ALLOWING_ANSWERS

        with patch("fun.cli.sys.stdin.isatty", return_value=True):
            for answer in ("yes", "always"):
                gate, _ = self._gate(answer)
                self.assertTrue(gate("exec:ls", "high"), answer)
            for answer in ("no", "n", "y", "a", "maybe", "true"):
                gate, _ = self._gate(answer)
                self.assertEqual(gate("exec:ls", "high"), answer in ALLOWING_ANSWERS, answer)

    def test_the_ui_only_ever_returns_answers_the_gate_understands(self):
        import inspect

        from fun.cli import ALLOWING_ANSWERS
        from fun.ui.app import App

        source = inspect.getsource(App.request_approval)
        returned = set(re.findall(r'return "([a-z]+)"', source))
        self.assertTrue(returned)
        self.assertTrue(returned <= {"yes", "no", "always"}, returned)
        self.assertTrue(ALLOWING_ANSWERS <= {"yes", "always"})

    def test_always_is_remembered_but_never_for_a_critical_operation(self):
        with patch("fun.cli.sys.stdin.isatty", return_value=True):
            remembered = set()
            gate, app = self._gate("always", remembered)
            self.assertTrue(gate("exec:ls", "high"))
            self.assertIn("exec:ls", remembered)
            self.assertTrue(gate("exec:ls", "high"))
            self.assertEqual(len(app.asked), 1, "a remembered subject is not asked twice")

            remembered = set()
            gate, app = self._gate("always", remembered)
            self.assertTrue(gate("exec:rm", "critical"))
            self.assertNotIn("exec:rm", remembered, "critical is never remembered")
            gate("exec:rm", "critical")
            self.assertEqual(len(app.asked), 2, "a critical subject is asked every time")

    def test_a_non_interactive_run_denies_rather_than_hanging(self):
        gate, app = self._gate("yes", interactive=False)
        self.assertFalse(gate("exec:rm", "critical"))
        self.assertEqual(app.asked, [])

    def test_pressing_n_in_the_app_answers_no(self):
        import io
        import threading

        from fun.ui.app import App
        from fun.ui.stream import StreamSurface
        from fun.ui.theme import Theme

        app = App(StreamSurface(io.StringIO()), theme=Theme(mode="none"))
        answers = []
        worker = threading.Thread(target=lambda: answers.append(app.request_approval("exec:rm", "critical")), daemon=True)
        worker.start()
        for _ in range(200):
            app._consume()
            if app._approval is not None:
                break
            time.sleep(0.01)
        app._handle_key("n", lambda *_: None)
        app._consume()
        worker.join(timeout=5)
        self.assertEqual(answers, ["no"])


class RefusalWordingTests(unittest.TestCase):
    """A tag is for the model; the person watching gets a sentence."""

    def test_every_refusal_tag_the_tools_can_return_has_wording(self):
        import re as regex
        from pathlib import Path

        from fun.ui.components import REFUSAL_MESSAGES

        root = Path(__file__).resolve().parent.parent
        source = (root / "fun" / "tools.py").read_text(encoding="utf-8") + (root / "fun" / "runtime.py").read_text(encoding="utf-8")
        # Only all-caps machine tags: "File does not exist: x" is already a
        # sentence, and is meant to reach the screen as written.
        tags = set(regex.findall(r'ToolResult\(False, f?"([A-Z][A-Z_]{3,})(?=[":\\ ])', source))
        tags |= set(regex.findall(r'error_tag="([A-Z_]+)"', source))
        missing = sorted(tag for tag in tags if tag not in REFUSAL_MESSAGES)
        self.assertEqual(missing, [], f"these refusals would print as raw tags: {missing}")

    def test_a_denied_command_reads_as_denied_not_as_a_tag(self):
        from fun.ui import components
        from fun.ui.theme import Theme

        for locale in ("zh-CN", "en-US"):
            theme = Theme(mode="none", locale=locale)
            view = components.ToolView("exec", "failed", {"command": "rm -rf build"}, 1, 1, "APPROVAL_REQUIRED")
            body = "\n".join(components.tool_body(theme, view, 60))
            self.assertNotIn("APPROVAL_REQUIRED", body)
            self.assertEqual(body.strip(), theme.text("refuse_approval_required"))

    def test_ordinary_output_is_left_alone(self):
        from fun.ui import components
        from fun.ui.theme import Theme

        theme = Theme(mode="none", locale="en-US")
        view = components.ToolView("exec", "completed", {"command": "ls"}, 1, 0, "README.md\nsetup.py")
        self.assertIn("README.md", "\n".join(components.tool_body(theme, view, 60)))


class DiscoverabilityTests(unittest.TestCase):
    """Everything reachable has to be findable, in the session's language."""

    def test_every_command_is_translated_in_both_locales(self):
        from fun.commands import REGISTRY

        untranslated = []
        for name, command in sorted(REGISTRY.items()):
            for locale in ("zh-CN", "en-US"):
                described = command.describe(locale)
                # describe() falls back to the English literal it was registered
                # with, which in a Chinese session is a missing translation, not
                # a design choice — that is how /quit stayed English.
                if locale == "zh-CN" and described == command.summary:
                    untranslated.append((name, locale))
        self.assertEqual(untranslated, [], f"missing cmd_* wording: {untranslated}")

    def test_a_first_run_says_that_nothing_is_configured(self):
        import io

        from fun.ui.app import App
        from fun.ui.stream import StreamSurface
        from fun.ui.text import strip_ansi
        from fun.ui.theme import Theme

        for locale in ("zh-CN", "en-US"):
            theme = Theme(mode="none", locale=locale)
            app = App(StreamSurface(io.StringIO()), theme=theme)
            app.state.provider_ready = False
            frame = "\n".join(strip_ansi(line) for line in app.state.compose(76, 20))
            self.assertIn(theme.text("ui_needs_setup"), frame, locale)
            self.assertIn("/config", frame)
            app.state.provider_ready = True
            ready = "\n".join(strip_ansi(line) for line in app.state.compose(76, 20))
            self.assertNotIn(theme.text("ui_needs_setup"), ready, "a configured session is not nagged")

    def test_the_palette_key_is_advertised(self):
        from fun.ui.state import UiState
        from fun.ui.theme import Theme

        theme = Theme(mode="none", locale="zh-CN")
        keys = [key for key, _ in UiState(theme=theme).dock_hints(80)]
        self.assertIn("Ctrl-P", keys)
        # At a narrow width it is dropped rather than wrapping the hint bar.
        self.assertNotIn("Ctrl-P", [key for key, _ in UiState(theme=theme).dock_hints(50)])

    def test_the_offline_message_says_what_to_do(self):
        from fun.i18n import t

        for locale in ("zh-CN", "en-US"):
            message = t(locale, "offline")
            self.assertIn("/config", message, locale)
