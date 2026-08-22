import os
import time
import re
import io
import unittest

from fun.ui import components
from fun.ui.app import App
from fun.ui.fullscreen import FullscreenSurface
from fun.ui.input import CONTROL_KEYS, is_text
from fun.ui.modal import field_modal, prompt_modal, select_modal
from fun.ui.screen import DockWriter, ScreenWriter
from fun.ui.state import UiState
from fun.ui.stream import StreamSurface
from fun.ui.text import display_width, fit, pad, strip_ansi, truncate, wrap
from fun.ui.theme import Theme, detect_color_support, theme_names

PLAIN = Theme(mode="none", unicode=True, locale="zh-CN")
COLOR = Theme(mode="truecolor", unicode=True, locale="zh-CN")


class TextTests(unittest.TestCase):
    def test_display_width_counts_columns_not_characters(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("中文"), 4)
        self.assertEqual(display_width("a中b"), 4)

    def test_display_width_ignores_ansi_sequences(self):
        self.assertEqual(display_width("\033[36mabc\033[0m"), 3)
        self.assertEqual(strip_ansi("\033[1;36mx\033[0m"), "x")

    def test_display_width_ignores_combining_marks(self):
        self.assertEqual(display_width("é"), 1)

    def test_wrap_never_exceeds_the_budget(self):
        samples = [
            "a very long user message that should wrap instead of overflowing",
            "这是一个很长的中文句子用来测试换行是否会超出终端宽度限制",
            "mixed 中英文 content that wraps 混合排版 test",
            "",
            "singleverylongunbrokentokenthatmustbehardwrapped",
        ]
        for width in (8, 12, 20, 40):
            for sample in samples:
                for line in wrap(sample, width):
                    self.assertLessEqual(display_width(line), width, f"{width} {line!r}")

    def test_wrap_preserves_existing_newlines(self):
        self.assertEqual(wrap("a\nb", 10), ["a", "b"])

    def test_truncate_keeps_ansi_and_adds_an_ellipsis(self):
        result = truncate("\033[36mhello world\033[0m", 8)
        self.assertTrue(result.startswith("\033[36m"))
        self.assertEqual(display_width(result), 8)

    def test_truncate_leaves_short_text_untouched(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_fit_pads_and_clips_to_exact_width(self):
        self.assertEqual(display_width(fit("中文", 8)), 8)
        self.assertEqual(display_width(fit("a" * 20, 8)), 8)
        self.assertEqual(pad("ab", 5, "right"), "   ab")
        self.assertEqual(display_width(pad("中", 5, "center")), 5)


class ThemeTests(unittest.TestCase):
    def test_no_color_wins_over_everything(self):
        self.assertEqual(detect_color_support({"NO_COLOR": "1", "COLORTERM": "truecolor"}), "none")

    def test_force_color_overrides_detection(self):
        self.assertEqual(detect_color_support({"FORCE_COLOR": "3"}, is_tty=False), "truecolor")
        self.assertEqual(detect_color_support({"FORCE_COLOR": "0"}), "none")

    def test_capability_ladder(self):
        self.assertEqual(detect_color_support({"TERM": "xterm-256color", "COLORTERM": "truecolor"}), "truecolor")
        self.assertEqual(detect_color_support({"TERM": "xterm-256color"}), "256")
        self.assertEqual(detect_color_support({"TERM": "xterm"}), "16")
        self.assertEqual(detect_color_support({"TERM": "dumb"}), "none")
        self.assertEqual(detect_color_support({"TERM": "xterm"}, is_tty=False), "none")

    def test_style_is_a_no_op_without_color(self):
        self.assertEqual(PLAIN.style("x", "accent", bold=True), "x")
        self.assertIn("\033[", COLOR.style("x", "accent"))

    def test_lower_capability_modes_still_emit_color(self):
        self.assertIn("\033[", Theme(mode="256").style("x", "success"))
        self.assertIn("\033[", Theme(mode="16").style("x", "success"))

    def test_glyph_degrades_without_unicode(self):
        self.assertEqual(Theme(mode="none", unicode=False).glyph("✓", "v"), "v")


class ComponentTests(unittest.TestCase):
    """Components production actually renders.

    These assert against the `*_body` helpers the spine layout calls, not the
    superseded block components — a test that keeps an unused code path alive is
    guarding nothing while looking like coverage.
    """

    def _assert_within(self, lines, width):
        for line in lines:
            self.assertLessEqual(display_width(line), width, repr(line))

    def test_components_respect_the_width_budget(self):
        view = components.ToolView("exec", "completed", {"command": "pytest -q"}, 120, 0, "out\n" * 30)
        for theme in (PLAIN, COLOR):
            for width in (32, 48, 80):
                self._assert_within(components.banner(theme, width, "v1"), width)
                self._assert_within(components.tool_body(theme, view, width), width)
                self._assert_within(components.plan_body(theme, ["step one", "步骤二"], ["done", "active"], width), width)
                self._assert_within(components.recovery_body(theme, {"name": "exec", "call_id": "c1", "arguments": "x"}, width), width)
                self._assert_within(components.background_block(theme, [{"id": "bg", "status": "running", "goal": "x"}], width), width)
                self._assert_within([components.hint_bar(theme, [("Enter", "send")], width)], width)

    def test_plan_body_marks_each_status(self):
        rendered = "\n".join(components.plan_body(PLAIN, ["a", "b", "c", "d"], ["done", "active", "blocked", "pending"], 60))
        for marker in ("✓", "●", "×", "○"):
            self.assertIn(marker, rendered)

    def test_tool_body_reports_failure_details(self):
        view = components.ToolView("exec", "failed", {"command": "false"}, 5, 1, "", "permission denied")
        self.assertIn("permission denied", "\n".join(components.tool_body(PLAIN, view, 60)))

    def test_a_successful_read_is_summarised_not_reprinted(self):
        view = components.ToolView("read", "completed", {}, 1, 0, "line\n" * 50)
        summary = "\n".join(components.tool_body(PLAIN, view, 60))
        self.assertIn("行输出", summary)
        self.assertNotIn("line", summary)
        self.assertIn("line", "\n".join(components.tool_body(PLAIN, view, 60, expanded=True)))

    def test_a_failure_is_truncated_from_the_front_not_the_back(self):
        """The assertion and the summary are at the end of a failing run."""
        output = "\n".join(f"noise {index}" for index in range(40)) + "\nFAILED test_x - AssertionError"
        view = components.ToolView("exec", "failed", {"command": "pytest"}, 1, 1, output)
        rendered = "\n".join(components.tool_body(PLAIN, view, 60))
        self.assertIn("FAILED test_x", rendered)
        self.assertNotIn("noise 0", rendered)
        self.assertIn("前面还有", rendered)

    def test_a_long_successful_output_is_truncated_from_the_back(self):
        view = components.ToolView("exec", "completed", {"command": "ls"}, 1, 0, "\n".join(f"row {i}" for i in range(40)))
        rendered = "\n".join(components.tool_body(PLAIN, view, 60))
        self.assertIn("row 0", rendered)
        self.assertIn("还有", rendered)

    def test_the_header_shows_the_identifying_argument_only(self):
        view = components.ToolView("edit", "completed", {"path": "fun/cli.py", "expected_hash": "ab12", "patch": "@@ huge @@"}, 1, 0, "")
        header = components._format_arguments(view.arguments or {}, 60, view.name)
        self.assertEqual(header, "fun/cli.py")
        self.assertEqual(components._format_arguments({"command": "pytest -q"}, 60, "exec"), "pytest -q")

    def test_the_approval_body_offers_a_choice_not_a_key_list(self):
        view = components.ToolView("exec", "approval", {"command": "rm -rf ."}, risk="critical")
        rendered = "\n".join(components.approval_body(PLAIN, view, 60))
        self.assertIn("需要授权", rendered)
        self.assertIn("critical", rendered)
        self.assertIn("允许一次", rendered)
        self.assertIn("拒绝", rendered)

    def test_a_diff_is_coloured_by_line_kind(self):
        view = components.ToolView("edit", "completed", {"path": "a.py"}, 1, 0, "@@ -1 +1 @@\n-old\n+new")
        rendered = "\n".join(components.tool_body(COLOR, view, 60))
        self.assertIn("+new", strip_ansi(rendered))
        self.assertIn("\033[", rendered)

    def test_ascii_fallback_avoids_unicode_glyphs(self):
        ascii_theme = Theme(mode="none", unicode=False)
        rendered = "\n".join(components.plan_body(ascii_theme, ["a"], ["done"], 40))
        self.assertNotIn("✓", rendered)

    def test_the_exit_status_reaches_the_tool_node(self):
        """Exit codes live on the spine node, which the state model builds."""
        state = UiState(theme=PLAIN)
        state.tool_status("tool.failed", {"call_id": "c1", "name": "exec", "exit_code": 1, "elapsed_ms": 5})
        self.assertIn("exit 1", state.render(80))


class ModalTests(unittest.TestCase):
    def test_modal_borders_align_at_a_fixed_width(self):
        for theme in (PLAIN, COLOR):
            for modal in (
                select_modal("Choose model", ["a", "bb"], lambda value: None),
                select_modal("Choose model", ["模型-大", "gpt-4o"], lambda value: None, multi=True, chosen=["gpt-4o"]),
                field_modal("Provider", ["base_url", ("api_key", True)], lambda values: None),
                prompt_modal("系统提示词", "value", lambda value: None),
            ):
                widths = {display_width(line) for line in modal.lines(theme, 60)}
                self.assertEqual(len(widths), 1, f"{modal.kind}: {widths}")

    def test_select_modal_returns_the_highlighted_option(self):
        picked = []
        modal = select_modal("Choose", ["a", "b", "c"], picked.append)
        modal.handle("down")
        modal.handle("down")
        self.assertTrue(modal.handle("enter"))
        self.assertEqual(picked, ["c"])

    def test_a_long_model_list_is_filtered_by_typing(self):
        picked = []
        modal = select_modal("Choose", ["gpt-4o-mini", "claude-opus", "claude-haiku"], picked.append)
        for char in "haiku":
            modal.handle(char)
        self.assertEqual(modal.visible(), ["claude-haiku"])
        modal.handle("enter")
        self.assertEqual(picked, ["claude-haiku"])

    def test_a_filter_that_matches_nothing_says_so_instead_of_showing_everything(self):
        modal = select_modal("Choose", ["a-one", "a-two"], lambda value: None)
        for char in "zzz":
            modal.handle(char)
        self.assertEqual(modal.visible(), [])
        rendered = "\n".join(modal.lines(PLAIN, 60))
        self.assertIn(PLAIN.text("ui_select_empty"), rendered)
        # Enter on nothing must not pick an unrelated option.
        picked = []
        modal.callback = picked.append
        modal.handle("enter")
        self.assertEqual(picked, [None])

    def test_space_picks_several_models_and_enter_returns_them_in_order(self):
        picked = []
        modal = select_modal("Choose", ["a", "b", "c"], picked.append, multi=True)
        modal.handle("down")
        modal.handle(" ")  # the terminal delivers a literal space, not "space"
        modal.handle("down")
        modal.handle("space")
        modal.handle("enter")
        self.assertEqual(picked, [["b", "c"]])

    def test_multi_select_without_a_pick_still_takes_one_keypress(self):
        picked = []
        modal = select_modal("Choose", ["a", "b"], picked.append, multi=True)
        modal.handle("enter")
        self.assertEqual(picked, [["a"]])

    def test_filtering_does_not_forget_what_was_already_ticked(self):
        picked = []
        modal = select_modal("Choose", ["alpha", "beta"], picked.append, multi=True)
        modal.handle("space")
        for char in "beta":
            modal.handle(char)
        modal.handle("space")
        modal.handle("enter")
        self.assertEqual(picked, [["alpha", "beta"]])

    def test_the_highlight_follows_the_filtered_list_not_the_original_one(self):
        picked = []
        modal = select_modal("Choose", ["x-one", "y-two", "y-three"], picked.append)
        for char in "y-":
            modal.handle(char)
        modal.handle("down")
        modal.handle("enter")
        self.assertEqual(picked, ["y-three"])

    def test_select_modal_ignores_enter_while_loading(self):
        picked = []
        modal = select_modal("Choose", ["a"], picked.append)
        modal.loading = True
        self.assertFalse(modal.handle("enter"))
        self.assertEqual(picked, [])

    def test_field_modal_collects_every_field_in_order(self):
        captured = []
        modal = field_modal("Provider", ["base_url", "model"], captured.append)
        for char in "url":
            modal.handle(char)
        modal.handle("enter")
        for char in "gpt":
            modal.handle(char)
        self.assertTrue(modal.handle("enter"))
        self.assertEqual(captured, [{"base_url": "url", "model": "gpt"}])

    def test_secret_fields_are_never_rendered(self):
        modal = field_modal("Provider", [("api_key", True)], lambda values: None)
        modal.value = "super-secret"
        rendered = "\n".join(modal.lines(PLAIN, 60))
        self.assertNotIn("super-secret", rendered)
        self.assertIn("•", rendered)

    def test_escape_cancels_with_none(self):
        captured = []
        modal = prompt_modal("Prompt", "", captured.append)
        self.assertTrue(modal.handle("escape"))
        self.assertEqual(captured, [None])


class ScreenTests(unittest.TestCase):
    def test_dock_writer_never_clears_the_screen(self):
        buffer = io.StringIO()
        writer = DockWriter(buffer)
        writer.draw(["status", "prompt"])
        writer.write_above("history line")
        writer.draw(["status", "prompt"])
        output = buffer.getvalue()
        self.assertNotIn("\033[2J", output)
        self.assertIn("history line", output)

    def test_dock_writer_skips_identical_repaints(self):
        buffer = io.StringIO()
        writer = DockWriter(buffer)
        writer.draw(["a"])
        size = len(buffer.getvalue())
        writer.draw(["a"])
        self.assertEqual(len(buffer.getvalue()), size)

    def test_screen_writer_only_repaints_changed_rows(self):
        buffer = io.StringIO()
        writer = ScreenWriter(buffer)
        writer.enter()
        writer.draw(["one", "two", "three"], 20, 3)
        buffer.seek(0)
        buffer.truncate()
        writer.draw(["one", "CHANGED", "three"], 20, 3)
        output = buffer.getvalue()
        self.assertIn("CHANGED", output)
        self.assertNotIn("three", output)

    def test_screen_writer_enters_and_leaves_the_alternate_screen(self):
        buffer = io.StringIO()
        writer = ScreenWriter(buffer)
        writer.enter()
        self.assertIn("\033[?1049h", buffer.getvalue())
        writer.close()
        self.assertIn("\033[?1049l", buffer.getvalue())

    def test_screen_writer_repaints_everything_after_a_resize(self):
        buffer = io.StringIO()
        writer = ScreenWriter(buffer)
        writer.enter()
        writer.draw(["one", "two"], 20, 2)
        buffer.seek(0)
        buffer.truncate()
        writer.draw(["one", "two"], 30, 2)
        self.assertIn("one", buffer.getvalue())


class SurfaceTests(unittest.TestCase):
    def test_stream_surface_sends_settled_history_to_scrollback(self):
        buffer = io.StringIO()
        surface = StreamSurface(buffer)
        state = UiState(theme=PLAIN)
        state.add_user("hello")
        state.mode = "ready"
        surface.start()
        surface.paint(state, 60, 20)
        self.assertIn("hello", buffer.getvalue())
        self.assertNotIn("\033[2J", buffer.getvalue())

    def test_fullscreen_surface_paints_into_the_alternate_screen(self):
        buffer = io.StringIO()
        surface = FullscreenSurface(buffer)
        state = UiState(theme=PLAIN)
        state.add_user("hello")
        surface.start()
        surface.paint(state, 60, 20)
        self.assertIn("hello", buffer.getvalue())
        surface.stop()
        self.assertIn("\033[?1049l", buffer.getvalue())

    def test_surfaces_declare_their_scrollback_support(self):
        self.assertTrue(StreamSurface(io.StringIO()).supports_scrollback)
        self.assertFalse(FullscreenSurface(io.StringIO()).supports_scrollback)


class AppTests(unittest.TestCase):
    def _app(self):
        return App(StreamSurface(io.StringIO()), theme=PLAIN, commands=["/help", "/status"])

    def test_events_are_applied_on_the_ui_thread_only(self):
        app = self._app()
        app.post("user", "hello")
        app.post("assistant", "hi")
        self.assertEqual(app.state.transcript, [])
        app.paint()
        self.assertEqual(len(app.state.transcript), 2)

    def test_typing_and_submitting_clears_the_composer(self):
        app = self._app()
        submitted = []
        for char in "run tests":
            app._handle_key(char, submitted.append)
        self.assertEqual(app.state.composer, "run tests")
        app._handle_key("enter", submitted.append)
        self.assertEqual(submitted, ["run tests"])
        self.assertEqual(app.state.composer, "")

    def test_backspace_and_kill_edit_the_composer(self):
        app = self._app()
        for char in "abc":
            app._handle_key(char, lambda text: None)
        app._handle_key("backspace", lambda text: None)
        self.assertEqual(app.state.composer, "ab")
        app._handle_key("kill_to_start", lambda text: None)
        self.assertEqual(app.state.composer, "")

    def test_cursor_keys_edit_in_the_middle_of_the_draft(self):
        app = self._app()
        for char in "helo":
            app._handle_key(char, lambda text: None)
        app._handle_key("left", lambda text: None)
        app._handle_key("l", lambda text: None)
        self.assertEqual(app.state.composer, "hello")
        app._handle_key("home", lambda text: None)
        app._handle_key(">", lambda text: None)
        self.assertEqual(app.state.composer, ">hello")

    def test_word_motions_and_word_kill(self):
        app = self._app()
        for char in "run the focused tests":
            app._handle_key(char, lambda text: None)
        app._handle_key("kill_word_left", lambda text: None)
        self.assertEqual(app.state.composer, "run the focused ")
        app._handle_key("word_left", lambda text: None)
        app._handle_key("kill_to_end", lambda text: None)
        self.assertEqual(app.state.composer, "run the ")
        app._handle_key("yank", lambda text: None)
        self.assertEqual(app.state.composer, "run the focused ")

    def test_arrows_never_replace_a_draft_with_history(self):
        """Up used to fall through to history at the top of the draft, so it
        replaced what was being typed — and Enter then sent the old message.
        Terminals translate the wheel into Up, so scrolling did it too."""
        app = self._app()
        app.state.editor.history.extend(["earlier message"])
        for char in "one":
            app._handle_key(char, lambda text: None)
        app._handle_key("newline", lambda text: None)
        for char in "two":
            app._handle_key(char, lambda text: None)
        app._handle_key("up", lambda text: None)
        self.assertEqual(app.state.composer, "one\ntwo")
        app._handle_key("up", lambda text: None)
        self.assertEqual(app.state.composer, "one\ntwo", "the draft was replaced by history")

    def test_history_is_still_reachable_from_an_empty_composer(self):
        app = self._app()
        app.state.editor.history.extend(["first", "second"])
        app._handle_key("up", lambda text: None)
        self.assertEqual(app.state.composer, "second")
        app._handle_key("up", lambda text: None)
        self.assertEqual(app.state.composer, "first")
        app._handle_key("down", lambda text: None)
        self.assertEqual(app.state.composer, "second")
        app._handle_key("down", lambda text: None)
        self.assertEqual(app.state.composer, "")

    def test_slash_cycles_through_command_suggestions(self):
        app = self._app()
        app._handle_key("/", lambda text: None)
        app._handle_key("down", lambda text: None)
        self.assertTrue(app.state.composer.startswith("/"))

    def test_approval_keys_resolve_a_waiting_request(self):
        app = self._app()
        app.post("approval", type("R", (), {"name": "exec", "risk": "medium", "arguments": {}})())
        app.paint()
        self.assertEqual(app.state.mode, "approval")
        app._handle_key("y", lambda text: None)
        kinds = []
        while not app.events.empty():
            kinds.append(app.events.get_nowait()[0])
        self.assertIn("approval_answer", kinds)

    def test_recovery_keys_route_to_the_handler(self):
        app = self._app()
        actions = []
        app.recovery_handler = actions.append
        app.post("recovery", {"name": "exec", "call_id": "c1", "arguments": ""})
        app.paint()
        app._handle_key("r", lambda text: None)
        app.paint()
        self.assertEqual(actions, ["resume"])

    def test_modal_captures_keys_while_open(self):
        app = self._app()
        submitted = []
        app.open_prompt("Prompt", "", lambda value: submitted.append(value))
        app._handle_key("x", lambda text: None)
        app._handle_key("enter", lambda text: None)
        self.assertEqual(submitted, ["x"])
        self.assertIsNone(app.modal)
        self.assertEqual(app.state.composer, "")

    def test_status_events_drive_task_state(self):
        app = self._app()
        app.post("status", "working")
        app.paint()
        self.assertEqual(app.state.mode, "working")
        app.post("status", "ready")
        app.paint()
        self.assertEqual(app.state.mode, "ready")

    def test_typing_o_is_never_stolen_by_the_output_toggle(self):
        """Plain letters always reach the buffer; the toggle lives on Ctrl-O."""
        app = self._app()
        app.state.tool_status("tool.completed", {"call_id": "c1", "name": "read", "text": "x"})
        for char in "open the file":
            app._handle_key(char, lambda text: None)
        self.assertEqual(app.state.composer, "open the file")
        self.assertNotIn("c1", app.state.expanded_tools)
        app._handle_key("toggle_output", lambda text: None)
        self.assertIn("c1", app.state.expanded_tools)


class InputTests(unittest.TestCase):
    def test_control_characters_map_to_named_keys(self):
        self.assertEqual(CONTROL_KEYS["\r"], "enter")
        self.assertEqual(CONTROL_KEYS["\x0e"], "newline")
        self.assertEqual(CONTROL_KEYS["\x03"], "cancel")

    def test_is_text_rejects_named_keys(self):
        self.assertTrue(is_text("a"))
        self.assertTrue(is_text("中"))
        self.assertFalse(is_text("enter"))


if __name__ == "__main__":
    unittest.main()


class EditorTests(unittest.TestCase):
    def _editor(self, text="", cursor=None):
        from fun.ui.editor import Editor

        editor = Editor()
        editor.set(text)
        if cursor is not None:
            editor.cursor = cursor
        return editor

    def test_insert_happens_at_the_cursor_not_the_end(self):
        editor = self._editor("helo", 3)
        editor.insert("l")
        self.assertEqual(editor.text, "hello")

    def test_column_is_measured_in_display_width(self):
        editor = self._editor("中文abc", 2)
        self.assertEqual(editor.column, 4)

    def test_word_motions_are_symmetric_with_word_kill(self):
        editor = self._editor("run the focused tests")
        editor.move_buffer_end()
        editor.kill_word_left()
        self.assertEqual(editor.text, "run the focused ")
        editor.yank()
        self.assertEqual(editor.text, "run the focused tests")

    def test_kill_to_end_and_start_operate_on_the_current_line(self):
        editor = self._editor("alpha beta", 6)
        editor.kill_to_end()
        self.assertEqual(editor.text, "alpha ")
        editor = self._editor("alpha beta", 6)
        editor.kill_to_start()
        self.assertEqual(editor.text, "beta")
        self.assertEqual(editor.cursor, 0)

    def test_vertical_motion_preserves_the_visual_column_across_wide_chars(self):
        editor = self._editor("中文abc\nxyzlonger")
        editor.cursor = len("中文abc\n") + 6
        column = editor.column
        self.assertTrue(editor.move_up())
        self.assertEqual(editor.column, column)
        self.assertTrue(editor.move_down())
        self.assertEqual(editor.column, column)

    def test_vertical_motion_reports_the_edges(self):
        editor = self._editor("only line")
        self.assertFalse(editor.move_up())
        self.assertFalse(editor.move_down())

    def test_home_and_end_stay_within_the_current_line(self):
        editor = self._editor("first\nsecond", 8)
        editor.move_home()
        self.assertEqual(editor.cursor, 6)
        editor.move_end()
        self.assertEqual(editor.cursor, 12)

    def test_history_navigation_restores_the_pending_draft(self):
        editor = self._editor()
        editor.set("first"); editor.submit()
        editor.set("second"); editor.submit()
        editor.set("draft")
        editor.history_previous()
        self.assertEqual(editor.text, "second")
        editor.history_previous()
        self.assertEqual(editor.text, "first")
        editor.history_next()
        editor.history_next()
        self.assertEqual(editor.text, "draft")

    def test_render_places_the_cursor_on_the_right_character(self):
        editor = self._editor("修复登录测试", 2)
        lines, row, column = editor.visual_lines(40)
        self.assertEqual((row, column), (0, 4))
        rendered = editor.render(40, cursor_style="<", reset=">")
        self.assertIn("<登>", rendered[0])

    def test_render_never_exceeds_the_width(self):
        editor = self._editor("这是一段很长的中文输入用来验证换行" * 2, 5)
        for width in (12, 20, 40):
            for line in editor.visual_lines(width)[0]:
                self.assertLessEqual(display_width(line), width)


class SyntaxTests(unittest.TestCase):
    def test_tokenizing_never_loses_or_duplicates_characters(self):
        from fun.ui.syntax import tokenize

        samples = {
            "python": "def f(x):  # hi\n    return 'a' + 1",
            "js": "const x = `t ${n}`; // note",
            "go": 'func main() { fmt.Println("x") }',
            "json": '{"a": 1, "b": true}',
            "bash": "echo $HOME # comment",
            None: "plain text 123",
        }
        for language, source in samples.items():
            self.assertEqual("".join(text for _, text in tokenize(source, language)), source, language)

    def test_keywords_strings_numbers_and_comments_are_classified(self):
        from fun.ui.syntax import COMMENT, KEYWORD, NUMBER, STRING, tokenize

        kinds = dict((text, kind) for kind, text in tokenize("return 'x' + 42  # note", "python"))
        self.assertEqual(kinds["return"], KEYWORD)
        self.assertEqual(kinds["'x'"], STRING)
        self.assertEqual(kinds["42"], NUMBER)
        self.assertEqual(kinds["# note"], COMMENT)

    def test_unterminated_string_does_not_swallow_the_rest_of_the_file(self):
        from fun.ui.syntax import tokenize

        tokens = tokenize("x = 'oops\ny = 1", "python")
        self.assertEqual("".join(text for _, text in tokens), "x = 'oops\ny = 1")

    def test_language_is_guessed_from_a_path(self):
        from fun.ui.syntax import guess_language

        self.assertEqual(guess_language("src/auth/login.py"), "py")
        self.assertEqual(guess_language("a/b.ts"), "ts")
        self.assertIsNone(guess_language("README"))


class MarkdownTests(unittest.TestCase):
    SOURCE = (
        "## Heading\n\n"
        "Body with **bold**, `code` and a [link](https://x.dev).\n\n"
        "- first item\n- second item\n\n"
        "1. step one\n2. step two\n\n"
        "```python\ndef f():\n    return 1\n```\n\n"
        "> quoted note\n\n---\n"
    )

    def test_rendering_is_width_safe_with_and_without_colour(self):
        from fun.ui.markdown import render

        for theme in (PLAIN, COLOR):
            for width in (24, 40, 72):
                for line in render(theme, self.SOURCE, width):
                    self.assertLessEqual(display_width(line), width, f"{width} {line!r}")

    def test_markup_is_consumed_and_content_survives(self):
        from fun.ui.markdown import render

        plain = "\n".join(strip_ansi(line) for line in render(COLOR, self.SOURCE, 72))
        for expected in ("Heading", "bold", "code", "link", "first item", "step one", "quoted note"):
            self.assertIn(expected, plain)
        self.assertNotIn("**", plain)
        self.assertNotIn("```", plain)

    def test_code_fences_are_clipped_rather_than_reflowed(self):
        from fun.ui.markdown import render

        source = "```python\n" + "x = " + "a" * 200 + "\n```\n"
        lines = [line for line in render(PLAIN, source, 40) if "x =" in line]
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(display_width(lines[0]), 40)

    def test_an_unterminated_fence_still_renders(self):
        from fun.ui.markdown import render

        plain = "\n".join(strip_ansi(line) for line in render(PLAIN, "```python\ndef f():\n", 40))
        self.assertIn("def f():", plain)

    def test_adjacent_segments_with_one_style_emit_one_escape(self):
        from fun.ui.markdown import Segment, paint

        painted = paint(COLOR, [Segment("a"), Segment("b"), Segment("c")])
        self.assertEqual(painted.count("\033["), 2)  # one opener, one reset

    def test_assistant_messages_render_markdown_but_user_messages_do_not(self):
        state = UiState(theme=PLAIN)
        state.add_assistant("see **this**")
        rendered = state.render(60)
        self.assertIn("this", rendered)
        self.assertNotIn("**", rendered)
        verbatim = UiState(theme=PLAIN)
        verbatim.add_user("see **this**")
        self.assertIn("**this**", verbatim.render(60))

    def test_tool_output_is_highlighted_by_kind(self):
        # expanded=True: a successful read is summarised by default now.
        read = components.ToolView("read", "completed", {"path": "a.py"}, 1, 0, "def f():\n    return 1")
        self.assertIn("def f():", strip_ansi("\n".join(components.tool_body(COLOR, read, 60, expanded=True))))
        edit = components.ToolView("edit", "completed", {"path": "a.py"}, 1, 0, "@@ -1 +1 @@\n-old\n+new")
        body = "\n".join(components.tool_body(COLOR, edit, 60))
        self.assertIn("+new", strip_ansi(body))
        self.assertIn("\033[", body)


class InterruptTests(unittest.TestCase):
    """Ctrl-C must always have a way out, without being a one-key kill switch."""

    def _app(self):
        return App(StreamSurface(io.StringIO()), theme=PLAIN)

    def test_cancel_with_a_draft_clears_it_and_does_not_exit(self):
        app = self._app()
        for char in "hello":
            app._handle_key(char, lambda text: None)
        app._handle_key("cancel", lambda text: None)
        self.assertEqual(app.state.composer, "")
        self.assertFalse(app._stop)

    def test_two_cancels_on_an_empty_buffer_exit(self):
        app = self._app()
        app._handle_key("cancel", lambda text: None)
        self.assertFalse(app._stop)
        app._handle_key("cancel", lambda text: None)
        self.assertTrue(app._stop)

    def test_typing_between_cancels_disarms_the_exit(self):
        app = self._app()
        app._handle_key("cancel", lambda text: None)
        app._handle_key("x", lambda text: None)
        app._handle_key("cancel", lambda text: None)
        self.assertFalse(app._stop)

    def test_cancel_interrupts_a_running_task_before_it_exits(self):
        app = self._app()
        calls = []
        app.interrupt_handler = lambda: (calls.append(1), True)[1]
        app._handle_key("cancel", lambda text: None)
        self.assertEqual(len(calls), 1)
        self.assertFalse(app._stop)

    def test_cancel_falls_through_when_there_is_nothing_to_interrupt(self):
        app = self._app()
        app.interrupt_handler = lambda: False
        app._handle_key("cancel", lambda text: None)
        app._handle_key("cancel", lambda text: None)
        self.assertTrue(app._stop)

    def test_ctrl_d_deletes_forward_but_exits_on_an_empty_buffer(self):
        app = self._app()
        app.state.editor.set("abc")
        app.state.editor.cursor = 0
        app._handle_key("eof", lambda text: None)
        self.assertEqual(app.state.composer, "bc")
        self.assertFalse(app._stop)
        app.state.editor.clear()
        app._handle_key("eof", lambda text: None)
        self.assertTrue(app._stop)

    def test_the_hint_bar_states_what_ctrl_c_will_do(self):
        app = self._app()
        self.assertIn(("Ctrl-C", PLAIN.text("ui_hint_exit")), app.state.hints())
        app.state.editor.set("draft")
        self.assertIn(("Ctrl-C", PLAIN.text("ui_hint_cancel")), app.state.hints())


class CompletionTests(unittest.TestCase):
    def _completer(self):
        from fun.ui.completion import Completer, FileIndex

        completer = Completer(commands={"/model": "switch model", "/plan": "show plan", "/permissions": "approval"})
        completer.files = FileIndex(".")
        return completer

    def test_context_detection_covers_commands_files_and_prose(self):
        from fun.ui.completion import detect

        self.assertEqual(detect("/mod", 4).kind, "command")
        self.assertEqual(detect("/mod", 4).query, "mod")
        file_context = detect("fix @src/au", 11)
        self.assertEqual(file_context.kind, "file")
        self.assertEqual(file_context.query, "src/au")
        self.assertEqual((file_context.start, file_context.end), (4, 11))
        self.assertIsNone(detect("hello", 5))

    def test_a_command_only_completes_as_the_first_token(self):
        from fun.ui.completion import detect

        self.assertIsNone(detect("/model gpt", 10))

    def test_completion_works_mid_line_not_just_at_the_end(self):
        from fun.ui.completion import detect

        context = detect("see @ui and then some more", 7)
        self.assertEqual(context.query, "ui")
        self.assertEqual((context.start, context.end), (4, 7))

    def test_scoring_prefers_anchored_and_consecutive_matches(self):
        from fun.ui.completion import score

        self.assertIsNone(score("zzz", "fun/ui/state.py"))
        anchored = score("fun", "fun/ui/state.py")
        scattered = score("fun", "a/f/u/n.py")
        self.assertIsNotNone(anchored)
        self.assertGreater(anchored, scattered)

    def test_ranking_puts_the_obvious_file_first(self):
        from fun.ui.completion import Candidate, rank

        pool = [Candidate(p) for p in ("fun/ui/state.py", "fun/ui/screen.py", "fun/build/units.py")]
        self.assertEqual(rank("uis", pool, 3)[0].value, "fun/ui/state.py")

    def test_applying_a_candidate_splices_it_into_the_buffer(self):
        from fun.ui.completion import detect

        completer = self._completer()
        context = detect("see @st rest", 7)
        text, cursor = completer.apply("see @st rest", context, "fun/ui/state.py")
        self.assertEqual(text, "see @fun/ui/state.py rest")
        self.assertEqual(cursor, len("see @fun/ui/state.py"))
        at_end, end_cursor = completer.apply("see @st", detect("see @st", 7), "fun/ui/state.py")
        self.assertEqual(at_end, "see @fun/ui/state.py ")
        self.assertEqual(end_cursor, len(at_end))

    def test_narrowing_keeps_the_highlight_on_the_same_entry(self):
        from fun.ui.completion import CompletionState

        completer = self._completer()
        state = CompletionState()
        state.refresh(completer, "/p", 2)
        state.move(1)
        chosen = state.selected()
        state.refresh(completer, "/pe", 3)
        if any(item.value == chosen for item in state.candidates):
            self.assertEqual(state.selected(), chosen)

    def test_the_popup_keys_select_and_accept(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN, commands=["/model", "/plan", "/permissions"])
        app.completer.commands = {"/model": "switch model", "/plan": "show plan", "/permissions": "approval"}
        noop = lambda text: None
        for char in "/p":
            app._handle_key(char, noop)
        self.assertTrue(app.completion.active)
        first = app.completion.selected()
        app._handle_key("down", noop)
        self.assertNotEqual(app.completion.selected(), first)
        accepted = app.completion.selected()
        app._handle_key("tab", noop)
        self.assertEqual(app.state.composer, accepted + " ")
        self.assertFalse(app.completion.active)

    def test_escape_dismisses_the_popup_without_editing(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN, commands=["/model", "/plan"])
        app.completer.commands = {"/model": "switch model", "/plan": "show plan"}
        noop = lambda text: None
        for char in "/p":
            app._handle_key(char, noop)
        app._handle_key("escape", noop)
        self.assertFalse(app.completion.active)
        self.assertEqual(app.state.composer, "/p")

    def test_enter_accepts_a_candidate_instead_of_submitting(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN, commands=["/plan"])
        app.completer.commands = {"/plan": "show plan"}
        submitted = []
        for char in "/pl":
            app._handle_key(char, submitted.append)
        app._handle_key("enter", submitted.append)
        self.assertEqual(submitted, [])
        self.assertEqual(app.state.composer, "/plan ")

    def test_the_menu_is_width_safe(self):
        from fun.ui.completion import Candidate

        candidates = [Candidate("fun/ui/components.py", "a fairly long description here"), Candidate("a.py", "short")]
        for theme in (PLAIN, COLOR):
            for width in (32, 48, 80):
                for line in components.completion_menu(theme, candidates, 0, width):
                    self.assertLessEqual(display_width(line), width, f"{width} {line!r}")

    def test_the_file_index_skips_noise_directories(self):
        from fun.ui.completion import FileIndex

        paths = FileIndex(".").paths()
        self.assertTrue(paths)
        self.assertFalse([p for p in paths if "__pycache__" in p or p.startswith(".git")])


class ThemeSystemTests(unittest.TestCase):
    def test_every_theme_defines_every_semantic_slot(self):
        from fun.ui.theme import THEMES

        slots = set(THEMES["sky"])
        for name, palette in THEMES.items():
            self.assertEqual(set(palette), slots, f"{name} is missing slots")

    def test_switching_theme_changes_the_emitted_colour(self):
        sky = Theme(mode="truecolor", name="sky").style("x", "accent")
        ember = Theme(mode="truecolor", name="ember").style("x", "accent")
        self.assertNotEqual(sky, ember)

    def test_an_unknown_theme_falls_back_rather_than_crashing(self):
        theme = Theme(mode="truecolor", name="does-not-exist")
        self.assertTrue(theme.style("x", "accent"))

    def test_themes_render_the_whole_frame_within_budget(self):
        from fun.ui.theme import theme_names

        for name in theme_names():
            state = UiState(theme=Theme(mode="truecolor", name=name))
            state.add_user("修复登录测试")
            state.tool_status("tool.completed", {"call_id": "c1", "name": "read", "text": "ok", "elapsed_ms": 3})
            for width in (40, 76):
                for line in state.render(width).splitlines():
                    self.assertLessEqual(display_width(line), width, f"{name} {width} {line!r}")


class AgentModeTests(unittest.TestCase):
    def test_tab_cycles_the_mode_and_notifies_the_runtime(self):
        from fun.policy import AGENT_MODES

        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        seen = []
        app.mode_handler = seen.append
        self.assertEqual(app.state.agent_mode, "Build")
        for expected in AGENT_MODES[1:] + AGENT_MODES[:1]:
            app._handle_key("tab", lambda text: None)
            self.assertEqual(app.state.agent_mode, expected)
        self.assertEqual(seen, list(AGENT_MODES[1:]) + [AGENT_MODES[0]])

    def test_read_only_modes_are_a_real_capability_boundary(self):
        from fun.policy import Policy

        build = Policy(agent_mode="Build")
        self.assertTrue(build.allows("edit"))
        self.assertTrue(build.allows("exec"))
        for mode in ("Plan", "Review"):
            policy = Policy(agent_mode=mode)
            self.assertTrue(policy.read_only)
            self.assertFalse(policy.allows("edit"))
            self.assertFalse(policy.allows("exec"))
            self.assertTrue(policy.allows("read"))
            self.assertTrue(policy.allows("explore"))

    def test_the_runtime_refuses_mutating_tools_in_a_read_only_mode(self):
        from tempfile import TemporaryDirectory

        from fun.runtime import Runtime

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("check the mode boundary")
            runtime.policy.agent_mode = "Plan"
            self.assertTrue(runtime.run_tool("explore", path=".").ok)
            blocked = runtime.run_tool("exec", command="echo hi")
            self.assertFalse(blocked.ok)
            self.assertIn("MODE_FORBIDS_TOOL", blocked.text)
            tags = [event.payload.get("error_tag") for event in runtime.events.list()]
            self.assertIn("MODE_FORBIDS_TOOL", tags)
            runtime.stop()

    def test_the_dock_states_the_current_mode(self):
        state = UiState(theme=PLAIN, agent_mode="Plan")
        self.assertIn("Plan", "\n".join(state.dock_lines(60)))


class IntroTests(unittest.TestCase):
    """The streaming frontend must actually show a start screen."""

    def _state(self):
        state = UiState(theme=COLOR)
        state.model_name = "gpt-4o"
        state.workspace = "~/fun"
        state.version = "v1.0.0a6"
        state.session_label = "ses_1"
        return state

    def test_the_streaming_surface_prints_an_intro(self):
        buffer = io.StringIO()
        surface = StreamSurface(buffer)
        surface.start()
        surface.paint(self._state(), 76, 24)
        self.assertIn("Runtime-first", strip_ansi(buffer.getvalue()))

    def test_the_intro_is_printed_exactly_once(self):
        buffer = io.StringIO()
        surface = StreamSurface(buffer)
        state = self._state()
        surface.start()
        for _ in range(4):
            surface.paint(state, 76, 24)
        self.assertEqual(strip_ansi(buffer.getvalue()).count("Runtime-first"), 1)

    def test_the_intro_carries_the_session_context(self):
        plain = strip_ansi("\n".join(self._state().intro_lines(76)))
        for expected in ("~/fun", "gpt-4o", "Build", "smart", "v1.0.0a6", "ses_1"):
            self.assertIn(expected, plain)

    def test_the_intro_is_width_safe_and_degrades(self):
        for theme in (PLAIN, COLOR, Theme(mode="none", unicode=False)):
            state = self._state()
            state.theme = theme
            for width in (32, 44, 76):
                for line in state.intro_lines(width):
                    self.assertLessEqual(display_width(line), width, f"{width} {line!r}")

    def test_a_narrow_or_ascii_terminal_still_names_the_product(self):
        state = self._state()
        state.theme = Theme(mode="none", unicode=False)
        self.assertIn("FUN", "\n".join(state.intro_lines(76)))


class FullscreenCanvasTests(unittest.TestCase):
    def _capture(self, theme, width=80, height=20):
        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=theme)
        state = UiState(theme=theme, model_name="gpt-4o", workspace="~/fun", version="v1", session_label="ses_1")
        surface.start()
        surface.paint(state, width, height)
        return buffer.getvalue()

    def test_the_canvas_is_painted_edge_to_edge(self):
        raw = self._capture(Theme(mode="truecolor", name="sky"))
        self.assertIn("\033[48;2;11;15;22m", raw)

    def test_every_theme_defines_its_own_canvas(self):
        from fun.ui.theme import THEMES

        for name in THEMES:
            self.assertIn("canvas", THEMES[name], name)
            self.assertTrue(Theme(mode="truecolor", name=name).canvas())

    def test_no_canvas_is_painted_without_colour(self):
        self.assertEqual(Theme(mode="none").canvas(), "")
        raw = self._capture(Theme(mode="none"))
        self.assertNotIn("\033[48;", raw)

    def test_the_fullscreen_start_screen_shows_the_wordmark(self):
        raw = self._capture(Theme(mode="truecolor"), 92, 28)
        self.assertIn("Runtime-first", strip_ansi(raw))
        self.assertIn("█", raw)

    def test_the_alternate_screen_is_entered_and_restored(self):
        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=Theme(mode="truecolor"))
        surface.start()
        self.assertIn("\033[?1049h", buffer.getvalue())
        surface.stop()
        tail = buffer.getvalue()
        self.assertIn("\033[?1049l", tail)
        self.assertIn("\033[?25h", tail)

    def test_filled_rows_stay_within_the_width(self):
        from fun.ui.screen import ScreenWriter

        writer = ScreenWriter(io.StringIO(), background="\033[48;2;11;15;22m")
        filled = writer._fill(COLOR.style("中文 mixed", "accent"), 40)
        self.assertEqual(display_width(filled), 40)


class CaretPlacementTests(unittest.TestCase):
    """The real terminal cursor must sit in the input, on either layout.

    This is not cosmetic: macOS anchors the IME candidate window to the terminal
    cursor, so a misplaced caret puts the Chinese input popup in the corner.
    """

    def _caret(self, state, width=96, height=30):
        import re

        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=state.theme)
        surface.start()
        surface.paint(state, width, height)
        raw = buffer.getvalue()
        rows = {}
        for match in re.finditer(r"\x1b\[(\d+);1H\x1b\[2K((?:(?!\x1b\[\d+;1H).)*)", raw, re.S):
            rows[int(match.group(1))] = strip_ansi(match.group(2))
        placements = re.findall(r"\x1b\[(\d+);(\d+)H\x1b\[\?25h", raw)
        caret = (int(placements[-1][0]), int(placements[-1][1])) if placements else None
        return rows, caret

    def _state(self):
        return UiState(theme=COLOR, model_name="gpt-4o", workspace="~/fun", version="v1", session_label="ses_1")

    def test_the_caret_sits_in_the_input_panel_when_empty(self):
        rows, caret = self._caret(self._state())
        self.assertIsNotNone(caret)
        self.assertIn("▌", rows.get(caret[0], ""))

    def test_the_caret_sits_in_the_session_input_panel(self):
        state = self._state()
        state.add_user("你好")
        state.editor.set("再试一次")
        rows, caret = self._caret(state)
        self.assertIsNotNone(caret)
        self.assertIn("再试一次", rows.get(caret[0], ""))

    def test_the_caret_tracks_the_column_through_wide_characters(self):
        state = self._state()
        state.add_user("你好")
        state.editor.set("再试一次")
        state.editor.cursor = 0
        _, at_start = self._caret(state)
        state.editor.cursor = 2  # two wide characters in
        _, moved = self._caret(state)
        self.assertEqual(moved[1] - at_start[1], 4)

    def test_the_caret_is_hidden_when_input_is_not_accepting(self):
        state = self._state()
        state.add_user("你好")
        state.mode = "working"
        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=state.theme)
        surface.start()
        surface.paint(state, 96, 30)
        self.assertTrue(buffer.getvalue().rstrip().endswith("\033[?25l"))

    def test_the_caret_stays_in_the_dock_whatever_the_body_holds(self):
        """One layout: the caret is in the dock whether or not there is history."""
        idle = self._state()
        idle_rows, idle_caret = self._caret(idle)
        busy = self._state()
        busy.add_user("你好")
        busy_rows, busy_caret = self._caret(busy)
        # An empty session centres the input block, so the columns differ; what
        # must hold either way is that the caret lands on the input row.
        self.assertIn("▌", idle_rows.get(idle_caret[0], ""))
        self.assertIn("▌", busy_rows.get(busy_caret[0], ""))
        self.assertGreater(idle_caret[1], busy_caret[1])


class StartScreenInputTests(unittest.TestCase):
    """The start screen must render the real buffer, not a picture of one.

    Drawing a static placeholder there made typed input invisible: the editor
    was never consulted, so nothing the user typed could appear anywhere.
    """

    def _rows(self, state, width=96, height=30):
        import re

        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=state.theme)
        surface.start()
        surface.paint(state, width, height)
        rows = {}
        for match in re.finditer(r"\x1b\[(\d+);1H\x1b\[2K((?:(?!\x1b\[\d+;1H).)*)", buffer.getvalue(), re.S):
            rows[int(match.group(1))] = strip_ansi(match.group(2))
        placements = re.findall(r"\x1b\[(\d+);(\d+)H\x1b\[\?25h", buffer.getvalue())
        return rows, (int(placements[-1][0]), int(placements[-1][1])) if placements else None

    def _state(self):
        return UiState(theme=COLOR, model_name="gpt-4o", workspace="~/fun", version="v1", session_label="ses_1")

    def test_an_empty_buffer_shows_the_placeholder(self):
        rows, _ = self._rows(self._state())
        self.assertTrue(any("描述你想做的事" in row for row in rows.values()))

    def test_completion_reaches_an_empty_session(self):
        """`/` and `@` used to be advertised but never drawn on the start screen."""
        from fun.ui.completion import FileIndex

        app = App(StreamSurface(io.StringIO()), theme=COLOR, commands=["/plan", "/diff"])
        app.completer.commands = {"/plan": "show plan", "/diff": "diff"}
        app.completer.files = FileIndex(".")
        for char in "/p":
            app._handle_key(char, lambda text: None)
        rows, _ = self._rows(app.state)
        self.assertTrue(any("/plan" in row for row in rows.values()))

    def test_the_command_palette_opens_over_the_session(self):
        """Ctrl-P shows the grouped surface, not a prefilled slash draft."""
        app = App(StreamSurface(io.StringIO()), theme=COLOR, commands=["/plan", "/diff", "/model"])
        app._handle_key("palette", lambda text: None)
        self.assertIsNotNone(app.modal)
        self.assertEqual(app.modal.kind, "palette")
        self.assertFalse(app.completion.active)
        self.assertEqual(app.state.editor.text, "")
        listed = {row.command for row in app.modal.rows if not row.heading}
        for name in ("/plan", "/diff", "/model"):
            self.assertIn(name, listed)

    def test_the_mode_tabs_are_always_visible(self):
        rows, _ = self._rows(self._state())
        joined = "\n".join(rows.values())
        for name in ("Build", "Plan", "Review"):
            self.assertIn(name, joined)

    def test_typed_text_is_visible_on_the_start_screen(self):
        state = self._state()
        state.editor.set("你好世界")
        rows, _ = self._rows(state)
        self.assertTrue(any("你好世界" in row for row in rows.values()))

    def test_the_placeholder_disappears_once_typing_starts(self):
        state = self._state()
        state.editor.set("x")
        rows, _ = self._rows(state)
        self.assertFalse(any("描述你想做的事" in row for row in rows.values()))

    def test_the_caret_follows_typed_wide_characters(self):
        state = self._state()
        state.editor.set("你好世界")
        state.editor.cursor = 0
        _, at_start = self._rows(state)
        state.editor.cursor = 4
        _, at_end = self._rows(state)
        self.assertEqual(at_end[1] - at_start[1], 8)

    def test_a_multiline_draft_grows_the_panel_and_moves_the_caret(self):
        state = self._state()
        state.editor.set("第一行\n第二行")
        state.editor.cursor = len("第一行\n第二")
        rows, caret = self._rows(state)
        self.assertTrue(any("第一行" in row for row in rows.values()))
        self.assertIn("第二行", rows.get(caret[0], ""))

    def test_typing_through_the_app_reaches_the_screen(self):
        """End to end: keystrokes land in the buffer and then on the canvas."""
        app = App(StreamSurface(io.StringIO()), theme=COLOR)
        for char in "hello":
            app._handle_key(char, lambda text: None)
        rows, _ = self._rows(app.state)
        self.assertTrue(any("hello" in row for row in rows.values()))


class CanvasFrameTests(unittest.TestCase):
    """The border wraps every frame, and closes at exactly the terminal width."""

    def _rows(self, state, width=100, height=32):
        import re

        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=state.theme)
        surface.start()
        surface.paint(state, width, height)
        rows = {}
        for match in re.finditer(r"\x1b\[(\d+);1H\x1b\[2K((?:(?!\x1b\[\d+;1H).)*)", buffer.getvalue(), re.S):
            rows[int(match.group(1))] = strip_ansi(match.group(2))
        return [rows.get(index, "") for index in range(1, height + 1)]

    def _state(self):
        return UiState(theme=COLOR, model_name="gpt-4o", workspace="~/fun", version="v1", session_label="ses_1")

    def test_the_border_closes_at_the_exact_width(self):
        for width in (60, 80, 100, 132):
            rows = self._rows(self._state(), width, 28)
            self.assertEqual(display_width(rows[0]), width, f"top at {width}")
            self.assertEqual(display_width(rows[-1]), width, f"bottom at {width}")
            self.assertTrue(rows[0].endswith("╮"))
            self.assertTrue(rows[-1].endswith("╯"))

    def test_the_border_survives_a_session_with_content(self):
        state = self._state()
        state.add_user("修复登录测试")
        rows = self._rows(state, 100, 26)
        self.assertTrue(rows[0].startswith("╭"))
        self.assertTrue(rows[-1].startswith("╰"))
        self.assertIn("修复登录测试", "\n".join(rows))

    def test_the_rails_carry_session_and_workspace(self):
        rows = self._rows(self._state())
        self.assertIn("ses_1", rows[0])
        self.assertIn("~/fun", rows[-1])

    def test_every_framed_row_fits_the_width(self):
        state = self._state()
        state.add_user("这是一段很长的中文目标用来验证边框不会被宽字符撑破")
        for width in (60, 100):
            for row in self._rows(state, width, 24):
                self.assertLessEqual(display_width(row), width)

    def test_an_empty_session_centres_the_input_block(self):
        rows = self._rows(self._state())
        edge_rows = [row for row in rows if "▌" in row]
        self.assertTrue(edge_rows)
        indent = min(row.index("▌") for row in edge_rows)
        self.assertGreater(indent, 10, "the input block should not hug the left edge when empty")


class BodyAlignmentTests(unittest.TestCase):
    """Short sessions read from the top; long ones keep the newest events."""

    def _rows(self, state, width=100, height=30):
        import re

        buffer = io.StringIO()
        surface = FullscreenSurface(buffer, theme=state.theme)
        surface.start()
        surface.paint(state, width, height)
        rows = {}
        for match in re.finditer(r"\x1b\[(\d+);1H\x1b\[2K((?:(?!\x1b\[\d+;1H).)*)", buffer.getvalue(), re.S):
            rows[int(match.group(1))] = strip_ansi(match.group(2))
        return [rows.get(index, "") for index in range(1, height + 1)]

    def test_a_short_session_starts_at_the_top(self):
        state = UiState(theme=COLOR, model_name="gpt-4o")
        state.add_user("你好")
        rows = self._rows(state)
        # Row 1 is the border; the first event belongs immediately under it.
        self.assertIn("你", rows[1])

    def test_the_slack_sits_above_the_input_not_above_the_history(self):
        state = UiState(theme=COLOR, model_name="gpt-4o")
        state.add_user("你好")
        rows = self._rows(state)
        first_edge = next(index for index, row in enumerate(rows) if "▌" in row)
        blank_before_input = sum(1 for row in rows[2:first_edge] if not row.strip("│ "))
        self.assertGreater(blank_before_input, 3)

    def test_an_overflowing_session_keeps_the_newest_events(self):
        state = UiState(theme=COLOR, model_name="gpt-4o")
        for index in range(40):
            state.add_user(f"message-{index}")
        joined = "\n".join(self._rows(state))
        self.assertIn("message-39", joined)
        self.assertNotIn("message-0\n", joined)

# ------------------------------------------------------------------- palette


def _palette():
    from fun.commands import grouped_commands
    from fun.ui.modal import palette_modal

    chosen: list[str | None] = []
    groups = [
        (group, [(c.name, f"{c.name}  {c.summary}", c.key) for c in commands])
        for group, commands in grouped_commands()
    ]
    return palette_modal("命令", groups, chosen.append), chosen


def _palette_app():
    return App(StreamSurface(io.StringIO()), theme=PLAIN)


class PaletteTest(unittest.TestCase):
    def test_lists_every_registered_command_exactly_once(self):
        from fun.commands import ALIAS, REGISTRY

        modal, _ = _palette()
        listed = [row.command for row in modal.rows if not row.heading]
        expected = sorted(name for name, command in REGISTRY.items() if command.group != ALIAS)
        self.assertEqual(sorted(listed), expected)
        self.assertEqual(len(listed), len(set(listed)))

    def test_search_never_leaves_a_bare_group_heading(self):
        modal, _ = _palette()
        for char in "theme":
            modal.handle(char)
        commands = [row.command for row in modal.rows if not row.heading]
        self.assertIn("/theme", commands)
        headings = [index for index, row in enumerate(modal.rows) if row.heading]
        for index in headings:
            self.assertTrue(index + 1 < len(modal.rows) and not modal.rows[index + 1].heading)

    def test_selection_only_ever_lands_on_a_command(self):
        modal, _ = _palette()
        count = len([row for row in modal.rows if not row.heading])
        for _ in range(count + 2):
            self.assertFalse(modal.rows[modal.index].heading)
            modal.handle("down")

    def test_enter_reports_the_highlighted_command(self):
        modal, chosen = _palette()
        modal.handle("down")
        expected = modal.rows[modal.index].command
        self.assertTrue(modal.handle("enter"))
        self.assertEqual(chosen, [expected])

    def test_escape_reports_nothing(self):
        modal, chosen = _palette()
        self.assertTrue(modal.handle("escape"))
        self.assertEqual(chosen, [None])

    def test_every_row_is_exactly_as_wide_as_the_surface(self):
        modal, _ = _palette()
        for name in theme_names():
            for support in ("truecolor", "none"):
                theme = Theme(mode=support, name=name, locale="zh-CN")
                for width in (40, 60, 80, 120):
                    rendered = modal.palette_lines(theme, width)
                    widths = {display_width(strip_ansi(line)) for line in rendered}
                    self.assertEqual(len(widths), 1, (name, support, width, widths))
                    self.assertLessEqual(widths.pop(), width)

    def test_a_long_registry_scrolls_rather_than_growing(self):
        modal, _ = _palette()
        modal.max_rows = 6
        body = modal.palette_lines(Theme(mode="none", name="sky", locale="zh-CN"), 80)
        self.assertLess(len(body), len(modal.rows))

    def test_ctrl_p_opens_the_palette_and_dispatches_the_choice(self):
        app = _palette_app()
        submitted: list[str] = []
        app._submit = submitted.append
        app._handle_key("palette", submitted.append)
        self.assertIsNotNone(app.modal)
        self.assertEqual(app.modal.kind, "palette")
        expected = app.modal.rows[app.modal.index].command
        app._handle_key("enter", submitted.append)
        self.assertIsNone(app.modal)
        self.assertEqual(submitted, [expected])

    def test_commands_that_need_an_argument_are_typed_not_run(self):
        from fun.commands import REGISTRY

        app = _palette_app()
        submitted: list[str] = []
        app._submit = submitted.append
        app._handle_key("palette", submitted.append)
        while REGISTRY[app.modal.rows[app.modal.index].command].takes_argument is False:
            app._handle_key("down", submitted.append)
        expected = app.modal.rows[app.modal.index].command
        app._handle_key("enter", submitted.append)
        self.assertEqual(submitted, [])
        self.assertEqual(app.state.editor.text, f"{expected} ")
        self.assertEqual(app.state.editor.cursor, len(app.state.editor.text))

    def test_a_name_match_hides_the_description_matches(self):
        """"th" must mean /theme, not every summary containing t…h."""
        modal, _ = _palette()
        for char in "th":
            modal.handle(char)
        self.assertEqual([row.command for row in modal.rows if not row.heading], ["/theme"])

    def test_the_description_is_still_searchable_when_no_name_matches(self):
        modal, _ = _palette()
        for char in "transcript":
            modal.handle(char)
        self.assertIn("/clear", [row.command for row in modal.rows if not row.heading])

    def test_a_query_matching_nothing_says_so_without_crashing(self):
        modal, chosen = _palette()
        for char in "zzzz":
            modal.handle(char)
        self.assertEqual(modal.rows, [])
        rendered = "\n".join(modal.palette_lines(Theme(mode="none", name="sky", locale="zh-CN"), 60))
        self.assertIn("没有匹配的命令", rendered)
        self.assertTrue(modal.handle("enter"))
        self.assertEqual(chosen, [None])

    def test_backspace_restores_the_filtered_out_commands(self):
        modal, _ = _palette()
        full = len([row for row in modal.rows if not row.heading])
        for char in "theme":
            modal.handle(char)
        for _ in range(5):
            modal.handle("backspace")
        self.assertEqual(len([row for row in modal.rows if not row.heading]), full)


class TurnFooterTests(unittest.TestCase):
    def _state(self):
        state = UiState(theme=PLAIN)
        state.add_user("重构一下")
        state.add_assistant("好的,我先看一下 `fun/ui`。")
        return state

    def test_the_footer_hangs_off_the_reply_it_describes(self):
        state = self._state()
        state.set_turn_footer("Build  ·  gpt-4o  ·  1.4s")
        rendered = "\n".join(state.body_lines(80, 40))
        self.assertIn("Build  ·  gpt-4o  ·  1.4s", rendered)
        self.assertLess(rendered.index("好的"), rendered.index("gpt-4o"))

    def test_a_turn_without_a_reply_is_not_a_crash(self):
        state = UiState(theme=PLAIN)
        state.add_user("hi")
        state.set_turn_footer("Build  ·  gpt-4o  ·  1.4s")
        self.assertNotIn("gpt-4o", "\n".join(state.body_lines(80, 40)))

    def test_a_second_turn_does_not_restamp_the_first(self):
        state = self._state()
        state.set_turn_footer("first")
        state.add_user("再来")
        state.add_assistant("好")
        state.set_turn_footer("second")
        footers = [item.footer for item in state.transcript if item.role == "assistant"]
        self.assertEqual(footers, ["first", "second"])

    def test_the_footer_stays_inside_the_column(self):
        state = self._state()
        state.set_turn_footer("Build  ·  a-very-long-model-name-that-keeps-going-and-going  ·  12.5s")
        for line in state.body_lines(48, 40):
            self.assertLessEqual(display_width(strip_ansi(line)), 48)

    def test_the_app_applies_a_posted_turn_footer(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        app.post("assistant", "done")
        app.post("turn", "Build  ·  gpt-4o  ·  0.3s")
        app._consume()
        self.assertEqual(app.state.transcript[-1].footer, "Build  ·  gpt-4o  ·  0.3s")


# ------------------------------------------------------------------- sidebar


def _session_state(**overrides):
    from fun.ui.state import ToolCard, TranscriptItem

    state = UiState(theme=PLAIN, workspace="~/fun", model_name="gpt-4o", usage_text="312 tok")
    state.task_state = "working"
    state.goal = "把 completion 的排序改成命令名优先"
    state.add_user("把排序改一下")
    state.add_assistant("好的。")
    card = ToolCard("1", "read", {"path": "fun/ui/completion.py"}, "completed", 12)
    state.tools["1"] = card
    state.transcript.append(TranscriptItem("tool", tool=card))
    state.set_plan(["读文件", "改 score()", "补测试"], ["done", "active", "pending"])
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class SidebarTests(unittest.TestCase):
    def test_the_rail_appears_only_when_the_transcript_can_spare_the_columns(self):
        state = _session_state()
        self.assertFalse(state.rail_visible(80))
        self.assertTrue(state.rail_visible(120))

    def test_an_empty_session_has_no_rail(self):
        """A rail reporting no plan, no events and no goal is a column of absences."""
        self.assertFalse(UiState(theme=PLAIN).rail_visible(140))

    def test_the_divider_runs_the_full_height(self):
        state = _session_state()
        frame = state.compose(120, 30)
        room = len(frame) - len(state.dock_lines(120))
        # Measured in display columns, not string indexes: the transcript is
        # full of wide characters, so the two differ by design.
        columns = {display_width(strip_ansi(line).rsplit("│", 1)[0]) for line in frame[:room] if "│" in line}
        self.assertEqual(len(columns), 1, columns)

    def test_the_plan_is_in_the_rail_and_not_also_in_the_spine(self):
        state = _session_state()
        frame = "\n".join(state.compose(120, 30))
        self.assertEqual(frame.count("改 score()"), 1)
        self.assertIn("计划", frame)

    def test_the_plan_returns_to_the_spine_when_the_rail_is_hidden(self):
        state = _session_state()
        state.show_sidebar = False
        frame = "\n".join(state.compose(120, 30))
        self.assertIn("改 score()", frame)
        self.assertNotIn("上下文", frame)

    def test_the_rail_carries_goal_state_and_context(self):
        state = _session_state()
        frame = "\n".join(state.compose(120, 30))
        for expected in ("任务", "运行中", "把 completion", "计划", "上下文", "gpt-4o", "312 tok"):
            self.assertIn(expected, frame)

    def test_a_pending_recovery_outranks_every_other_card(self):
        from fun.ui import sidebar

        state = _session_state()
        state.recovery = {"name": "exec", "call_id": "c-7"}
        lines = sidebar.rail(PLAIN, state, 30, 20)
        self.assertIn("待恢复", lines[0])

    def test_a_short_rail_drops_whole_cards_and_says_so(self):
        from fun.ui import sidebar

        state = _session_state()
        lines = sidebar.rail(PLAIN, state, 30, 8)
        self.assertLessEqual(len(lines), 8)
        self.assertTrue(any("另有" in line for line in lines))

    def test_no_row_overflows_at_any_width_or_theme(self):
        state = _session_state()
        for name in theme_names():
            for support in ("truecolor", "none"):
                state.theme = Theme(mode=support, name=name, locale="zh-CN")
                for width in (92, 100, 120, 160):
                    for line in state.compose(width, 30):
                        self.assertLessEqual(display_width(strip_ansi(line)), width, (name, width, line))

    def test_ctrl_t_toggles_the_rail(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        self.assertTrue(app.state.show_sidebar)
        app._handle_key("sidebar", lambda text: None)
        self.assertFalse(app.state.show_sidebar)
        app._handle_key("sidebar", lambda text: None)
        self.assertTrue(app.state.show_sidebar)

    def test_the_toggle_is_advertised_only_where_it_does_something(self):
        state = _session_state()
        self.assertIn("Ctrl-T", [key for key, _ in state.dock_hints(120)])
        self.assertNotIn("Ctrl-T", [key for key, _ in state.dock_hints(80)])

    def test_the_caret_still_lands_in_the_composer_with_a_rail(self):
        state = _session_state()
        frame = state.compose(120, 30)
        row, column = state.cursor_hint
        self.assertTrue(0 <= row < len(frame))
        self.assertIn("▌", strip_ansi(frame[row]))


# --------------------------------------------------------------------- paste


class BracketedPasteTests(unittest.TestCase):
    """Pasting a key used to cancel the dialog and then type ``00~`` into it."""

    def _feed(self, payload: bytes):
        import os

        from fun.ui.input import read_key

        reader, writer = os.pipe()
        os.write(writer, payload)
        os.close(writer)
        return read_key(reader)

    def test_a_paste_arrives_as_one_event_not_as_an_escape(self):
        from fun.ui.input import paste_text

        key = self._feed(b"\x1b[200~sk-proj-AbCdEf1234\x1b[201~")
        self.assertEqual(paste_text(key), "sk-proj-AbCdEf1234")

    def test_an_unhandled_escape_sequence_is_consumed_whole(self):
        """Its tail must not fall through and be read as typing."""
        import os

        from fun.ui.input import read_key

        reader, writer = os.pipe()
        os.write(writer, b"\x1b[1;5Cx")
        self.assertEqual(read_key(reader), "escape")
        self.assertEqual(read_key(reader), "x")

    def test_the_navigation_keys_still_decode(self):
        for payload, expected in ((b"\x1b[5~", "pageup"), (b"\x1b[6~", "pagedown"), (b"\x1b[3~", "delete"), (b"\x1b[A", "up"), (b"\x1bOD", "left")):
            self.assertEqual(self._feed(payload), expected)

    def test_a_pasted_key_reaches_a_secret_field_intact(self):
        captured: list[dict] = []
        modal = field_modal("Provider", ["base_url", ("api_key", True)], captured.append)
        modal.handle("h")
        modal.handle("enter")
        modal.handle("paste:sk-proj-AbCdEf1234\n")
        modal.handle("enter")
        self.assertEqual(captured, [{"base_url": "h", "api_key": "sk-proj-AbCdEf1234"}])

    def test_a_paste_can_never_submit_or_cancel_a_dialog(self):
        captured: list[object] = []
        modal = prompt_modal("Prompt", "", captured.append)
        self.assertFalse(modal.handle("paste:one\ntwo"))
        self.assertFalse(modal.handle("paste:\x1b"))
        self.assertEqual(captured, [])

    def test_a_paste_into_the_composer_is_text_not_control(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        submitted: list[str] = []
        app._handle_key("paste:/help\nrm -rf", submitted.append)
        self.assertEqual(app.state.editor.text, "/help\nrm -rf")
        self.assertEqual(submitted, [])

    def test_a_secret_field_still_never_renders_its_value(self):
        modal = field_modal("Provider", [("api_key", True)], lambda values: None)
        modal.handle("paste:sk-proj-AbCdEf1234")
        rendered = "\n".join(modal.lines(PLAIN, 60))
        self.assertNotIn("AbCdEf", rendered)
        self.assertIn("•", rendered)


# ----------------------------------------------------- rendering invariants


class RenderInvariantTests(unittest.TestCase):
    def test_truncate_closes_the_style_it_cuts(self):
        colour = Theme(mode="truecolor")
        cut = truncate(colour.style(" Review ", "accent", reverse=True) + colour.style(" tail", "faint"), 6)
        self.assertTrue(cut.endswith("\x1b[0m"), repr(cut))
        self.assertEqual(display_width(cut), 6)

    def test_truncate_does_not_invent_a_reset_for_plain_text(self):
        self.assertEqual(truncate("hello world", 5), "hell…")

    def test_the_mode_tab_strip_is_clipped_like_every_other_dock_row(self):
        state = UiState(theme=PLAIN)
        for width in (16, 20, 24, 28, 32, 40):
            for line in state.dock_lines(width):
                self.assertLessEqual(display_width(strip_ansi(line)), width, (width, line))

    def test_the_composer_shows_the_character_that_was_just_typed(self):
        state = UiState(theme=PLAIN)
        state.editor.set("x" * 36)
        state.editor.cursor = 36
        rows = state.dock_lines(40)
        panel = [line for line in rows if "▌" in line and "x" in line]
        self.assertTrue(panel)
        self.assertNotIn("…", panel[-1])
        caret_row, caret_column = state.dock_caret
        self.assertLess(caret_column, 40)

    def test_every_frame_is_exactly_the_height_it_was_asked_for(self):
        state = UiState(theme=PLAIN)
        state.add_user("hello")
        state.add_assistant("hi")
        for height in range(1, 30):
            self.assertEqual(len(state.compose(60, height)), height, height)

    def test_the_composer_survives_a_terminal_too_short_for_the_dock(self):
        state = UiState(theme=PLAIN)
        state.add_user("hello")
        for height in (2, 3, 4, 6, 8):
            frame = "\n".join(strip_ansi(line) for line in state.compose(60, height))
            self.assertIn("▌", frame, height)

    def test_the_frame_border_never_exceeds_the_terminal(self):
        from fun.ui.layout import frame_canvas

        workspace = "/Users/someone/Development/clients/acme/services/api-gateway/worker"
        for width in (40, 60, 80, 120):
            framed = frame_canvas(PLAIN, ["body"], width, 6, session="ses_abcdef", workspace=workspace, mode="Build", approval="smart", version="v1.0.0a6")
            for line in framed:
                self.assertEqual(display_width(strip_ansi(line)), width, (width, line))

    def test_a_queued_item_behind_a_running_one_is_not_flushed_early(self):
        """Scrollback is never repainted, so an early flush freezes the card."""
        state = UiState(theme=PLAIN)
        state.tool_status("tool.executing", {"call_id": "a", "name": "exec", "arguments": {"command": "sleep 5"}})
        state.tool_status("tool.requested", {"call_id": "b", "name": "read", "arguments": {"path": "x.py"}})
        self.assertEqual(state.flushable(), [])
        state.tool_status("tool.completed", {"call_id": "a", "text": "IMPORTANT OUTPUT", "elapsed_ms": 10})
        flushed = "\n".join(state.flush(80))
        self.assertIn("IMPORTANT OUTPUT", flushed)


class DockWriterScrollbackTests(unittest.TestCase):
    """`_erase_dock` assumed the cursor was on the last dock row; it is not."""

    def _writer(self):
        stream = io.StringIO()
        return DockWriter(stream), stream

    def test_a_repaint_after_place_cursor_walks_up_the_right_number_of_rows(self):
        writer, stream = self._writer()
        writer.draw(["one", "two", "three", "four"])
        writer.place_cursor(1, 0)
        stream.truncate(0)
        stream.seek(0)
        writer.draw(["one", "two", "three", "CHANGED"])
        # From row 1, reaching row 0 is exactly one cursor-up.
        self.assertEqual(stream.getvalue().count("\033[F"), 1)

    def test_writing_above_from_a_parked_cursor_does_not_climb_past_the_dock(self):
        writer, stream = self._writer()
        writer.draw(["a", "b", "c", "d", "e", "f", "g"])
        writer.place_cursor(3, 0)
        stream.truncate(0)
        stream.seek(0)
        writer.write_above("transcript line")
        self.assertEqual(stream.getvalue().count("\033[F"), 3)

    def test_the_cursor_row_is_tracked_across_a_full_paint_cycle(self):
        writer, _ = self._writer()
        for row in (0, 2, 5):
            writer.draw(["0", "1", "2", "3", "4", "5"])
            writer.place_cursor(row, 4)
            self.assertEqual(writer._cursor_row, row)


class InteractionRegressionTests(unittest.TestCase):
    def _app(self, **kwargs):
        return App(StreamSurface(io.StringIO()), theme=PLAIN, **kwargs)

    def test_ctrl_c_does_not_leave_a_ghost_completion_behind(self):
        """Enter after Ctrl-C used to complete the draft it had just cleared."""
        app = self._app(commands=["/help", "/hello"])
        app.completer.commands = {"/help": "help", "/hello": "hi"}
        submitted: list[str] = []
        for char in "/he":
            app._handle_key(char, submitted.append)
        self.assertTrue(app.completion.active)
        app._handle_key("cancel", submitted.append)
        self.assertEqual(app.state.editor.text, "")
        self.assertFalse(app.completion.active)
        app._handle_key("enter", submitted.append)
        self.assertEqual(app.state.editor.text, "")
        self.assertEqual(submitted, [])

    def test_a_pending_recovery_blocks_the_composer_rather_than_half_eating_it(self):
        """It used to accept every key except r/d/f/s, so typing "restart"
        resumed the task on its first character and dropped the rest."""
        app = self._app()
        app.state.set_recovery({"name": "exec", "call_id": "c-1"})
        for char in "xyz hello wpqt":
            app._handle_key(char, lambda text: None)
        self.assertEqual(app.state.editor.text, "")
        kinds = []
        while not app.events.empty():
            kinds.append(app.events.get_nowait()[0])
        self.assertNotIn("recovery_action", kinds)

    def test_recovery_keys_still_work_from_an_empty_composer(self):
        app = self._app()
        app.state.set_recovery({"name": "exec", "call_id": "c-1"})
        app._handle_key("r", lambda text: None)
        kinds = []
        while not app.events.empty():
            kinds.append(app.events.get_nowait()[0])
        self.assertIn("recovery_action", kinds)

    def test_cancelling_the_palette_does_not_end_a_running_turn(self):
        app = self._app()
        app.post("status", "working")
        app._consume()
        self.assertEqual(app.state.mode, "working")
        app._handle_key("palette", lambda text: None)
        app._handle_key("escape", lambda text: None)
        app._consume()
        self.assertEqual(app.state.mode, "working")
        self.assertEqual(app.state.task_state, "working")

    def test_a_late_model_list_is_not_applied_to_a_different_dialog(self):
        app = self._app()
        stale = app.open_select("Choose model", ["gpt-4o"], lambda value: None)
        app.modal = None
        app.open_select("Approval", ["ask", "smart", "auto"], lambda value: None)
        app.post("model_options", (stale, ["gpt-4o", "gpt-4o-mini"]))
        app._consume()
        self.assertEqual(app.modal.options, ["ask", "smart", "auto"])

    def test_a_matching_model_list_is_applied(self):
        app = self._app()
        token = app.open_select("Choose model", ["gpt-4o"], lambda value: None)
        app.post("model_options", (token, ["gpt-4o", "gpt-4o-mini"]))
        app._consume()
        self.assertEqual(app.modal.options, ["gpt-4o", "gpt-4o-mini"])

    def test_ctrl_u_and_ctrl_k_work_inside_a_dialog(self):
        captured: list[object] = []
        modal = prompt_modal("Prompt", "some text", captured.append)
        modal.handle("kill_to_start")
        self.assertEqual(modal.value, "")
        modal = field_modal("Provider", ["base_url"], lambda values: None)
        for char in "https://x":
            modal.handle(char)
        modal.handle("kill_to_end")
        self.assertEqual(modal.value, "")

    def test_an_empty_kill_does_not_erase_the_kill_ring(self):
        from fun.ui.editor import Editor

        editor = Editor()
        editor.set("hello world")
        editor.cursor = len(editor.text)
        editor.kill_word_left()
        self.assertEqual(editor.killed, "world")
        editor.kill_to_end()          # nothing to the right
        self.assertEqual(editor.killed, "world")
        editor.yank()
        self.assertEqual(editor.text, "hello world")

    def test_slash_commands_are_recalled_by_the_up_arrow(self):
        state = UiState(theme=PLAIN)
        state.add_command("/status")
        state.add_user("do the thing")
        self.assertEqual(state.composer_history, ["/status", "do the thing"])

    def test_the_background_comparison_converges_for_a_long_goal(self):
        from fun.ui.state import normalize_background

        tasks = [{"id": "t-1", "status": "running", "goal": "x" * 300, "result": "", "error": ""}]
        state = UiState(theme=PLAIN)
        state.set_background(tasks)
        self.assertEqual([normalize_background(item) for item in tasks], state.background)

    def test_painting_is_skipped_when_nothing_changed(self):
        class Counting(StreamSurface):
            painted = 0

            def paint(self, state, width, height, overlay=None):
                Counting.painted += 1

        app = App(Counting(io.StringIO()), theme=PLAIN)
        app.paint(force=True)
        first = Counting.painted
        app.paint()
        app.paint()
        self.assertEqual(Counting.painted, first)
        app._handle_key("x", lambda text: None)
        app.paint()
        self.assertEqual(Counting.painted, first + 1)

    def test_an_animating_state_still_repaints(self):
        class Counting(StreamSurface):
            painted = 0

            def paint(self, state, width, height, overlay=None):
                Counting.painted += 1

        app = App(Counting(io.StringIO()), theme=PLAIN)
        app.post("status", "working")
        app.paint(force=True)
        before = Counting.painted
        app.paint()
        self.assertGreater(Counting.painted, before)


class UntrustedContentTests(unittest.TestCase):
    """Model output reaches the terminal, which treats some bytes as commands."""

    def test_escape_sequences_never_reach_the_terminal(self):
        state = UiState(theme=PLAIN)
        state.add_assistant("hello \x1b[2J\x1b]0;pwned\x07 world")
        state.tool_status("tool.started", {"call_id": "c", "name": "read"})
        state.tool_status("tool.completed", {"call_id": "c", "text": "a\x1b]52;c;ZXZpbA==\x07b"})
        state.expanded_tools.add("c")
        rendered = "\n".join(state.body_lines(70))
        self.assertNotIn("\x1b", rendered)
        self.assertIn("hello  world", rendered)
        self.assertIn("ab", rendered)

    def test_a_carriage_return_cannot_erase_the_drawn_line(self):
        state = UiState(theme=PLAIN)
        state.add_assistant("visible\rhidden")
        self.assertIn("visible", "\n".join(state.body_lines(70)))

    def test_ordinary_content_including_cjk_and_tabs_survives(self):
        from fun.ui.text import sanitize

        self.assertEqual(sanitize("你好\t换行\n还在"), "你好\t换行\n还在")
        self.assertEqual(sanitize("plain text"), "plain text")


class RenderPerformanceTests(unittest.TestCase):
    """Both of these froze the UI thread on every repaint."""

    def test_one_unbroken_long_token_wraps_in_reasonable_time(self):
        from fun.ui.markdown import Segment, wrap_segments

        started = time.monotonic()
        rows = wrap_segments([Segment("a" * 100000)], 80)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(rows), 1250)

    def test_a_long_run_of_one_token_kind_tokenizes_in_reasonable_time(self):
        from fun.ui.syntax import tokenize

        started = time.monotonic()
        tokens = tokenize(" " * 400000, "python")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual("".join(text for _, text in tokens), " " * 400000)

    def test_the_tokenizer_is_still_lossless(self):
        import random

        from fun.ui.syntax import tokenize

        random.seed(11)
        alphabet = "ab \n\t\"'#/*()你好\\`{}[]0123.-"
        for _ in range(1500):
            source = "".join(random.choice(alphabet) for _ in range(random.randint(0, 60)))
            for language in ("python", "javascript", "go", "rust", "bash", "json", "yaml", "toml", "sql", "diff", None):
                self.assertEqual("".join(text for _, text in tokenize(source, language)), source, (language, source))


class EditorCursorMappingTests(unittest.TestCase):
    def test_a_typed_space_is_a_column_the_caret_can_stand_in(self):
        from fun.ui.editor import Editor

        editor = Editor()
        editor.text = "alpha\nbeta \ngamma"
        editor.cursor = 11
        lines, row, column = editor.visual_lines(40)
        # Column 5, not 4: the space the user typed occupies a cell.  Reporting
        # 4 meant that pressing space changed neither the rendered line nor the
        # caret position, and the composer looked frozen until the next visible
        # character arrived.
        self.assertEqual((row, column), (1, 5))
        self.assertEqual(lines[1], "beta ")

    def test_pressing_space_always_changes_something_on_screen(self):
        from fun.ui.editor import Editor

        for text in ("abc", "帮我", "a b", ""):
            before = Editor()
            before.insert(text)
            after = Editor()
            after.insert(text + " ")
            self.assertNotEqual(before.visual_lines(20), after.visual_lines(20), repr(text))

    def test_wrapped_rows_never_overflow_the_panel(self):
        import random

        from fun.ui.editor import Editor
        from fun.ui.text import display_width, strip_ansi

        random.seed(11)
        for _ in range(3000):
            text = "".join(random.choice("ab 中\n") for _ in range(random.randint(0, 20)))
            width = random.randint(4, 12)
            editor = Editor()
            editor.text = text
            editor.cursor = random.randint(0, len(text))
            lines, row, column = editor.visual_lines(width)
            self.assertTrue(0 <= row < len(lines), (text, width))
            self.assertLessEqual(column, width, (text, width))
            for line in lines:
                self.assertLessEqual(display_width(line), width, (text, width, line))
            for line in editor.render(width):
                self.assertLessEqual(display_width(strip_ansi(line)), width, (text, width))
            if "\n" not in text:
                self.assertEqual("".join(lines), text, "wrapping must not drop what was typed")

    def test_a_full_row_gives_the_caret_the_row_below(self):
        from fun.ui.editor import Editor

        editor = Editor()
        editor.insert("a" * 10)
        lines, row, column = editor.visual_lines(10)
        self.assertEqual((row, column), (1, 0), "the caret may not sit one column past the edge")
        self.assertEqual(lines, ["a" * 10, ""])

    def test_the_caret_is_always_inside_the_line_it_reports(self):
        import random

        from fun.ui.editor import Editor

        random.seed(5)
        for _ in range(4000):
            text = "".join(random.choice("ab \n你好  ") for _ in range(random.randint(0, 24)))
            editor = Editor()
            editor.text = text
            editor.cursor = random.randint(0, len(text))
            for width in (6, 12, 40):
                lines, row, column = editor.visual_lines(width)
                self.assertTrue(0 <= row < len(lines), (text, width, row))
                self.assertLessEqual(column, display_width(lines[row]), (text, width))


class CompletionSpanTests(unittest.TestCase):
    def test_completing_from_the_start_of_the_buffer_does_not_duplicate(self):
        from fun.ui.completion import Completer, detect

        completer = Completer(commands={"/help": "h", "/hello": "x"})
        self.assertIsNone(detect("/hel", 0))
        context = detect("/hel", 4)
        self.assertEqual(completer.apply("/hel", context, "/help"), ("/help ", 6))

    def test_completing_mid_command_replaces_the_whole_command(self):
        from fun.ui.completion import Completer, detect

        completer = Completer(commands={"/help": "h"})
        context = detect("/hel", 2)
        self.assertEqual(completer.apply("/hel", context, "/help"), ("/help ", 6))


class ToolCardArgumentTests(unittest.TestCase):
    def test_a_card_shows_what_the_tool_was_called_with(self):
        """Only approval.pending carried arguments, so every auto-approved call
        rendered as a bare "read" with no path."""
        state = UiState(theme=PLAIN)
        state.tool_status("tool.started", {"call_id": "c", "name": "read", "arguments": {"path": "fun/cli.py"}})
        state.tool_status("tool.executing", {"call_id": "c", "name": "read", "arguments": {"path": "fun/cli.py"}})
        self.assertIn("fun/cli.py", "\n".join(state.body_lines(80)))


class LocaleTests(unittest.TestCase):
    """The chrome was hardcoded Chinese: `fun --locale en-US` asked an English
    speaker to approve a critical exec in a language they may not read."""

    CHINESE = re.compile(r"[一-鿿]")

    def _state(self, locale):
        from fun.ui.state import ToolCard, TranscriptItem

        state = UiState(theme=Theme(mode="none", locale=locale), workspace="~/fun", model_name="gpt-4o")
        state.task_state = "working"
        state.goal = "refactor ranking"
        state.add_user("do the thing")
        state.add_assistant("ok")
        card = ToolCard("1", "read", {"path": "a.py"}, "failed", 12)
        state.tools["1"] = card
        state.transcript.append(TranscriptItem("tool", tool=card))
        state.set_plan(["read", "edit"], ["done", "active"])
        state.set_background([{"id": "bg_1", "status": "running", "goal": "scan"}])
        state.set_recovery({"name": "exec", "call_id": "c-1"})
        return state

    def test_an_english_session_has_no_chinese_chrome(self):
        state = self._state("en-US")
        frame = "\n".join(state.compose(120, 30))
        self.assertIsNone(self.CHINESE.search(frame), frame)

    def test_a_chinese_session_is_chinese(self):
        # A blocking recovery panel owns the screen, so the plan section is
        # legitimately scrolled off; assert on the chrome that is on screen.
        frame = "\n".join(self._state("zh-CN").compose(120, 30))
        for expected in ("你", "计划", "待恢复", "上次运行没有正常退出", "继续执行"):
            self.assertIn(expected, frame)

    def test_the_approval_gate_speaks_the_session_language(self):
        view = components.ToolView("exec", "approval", {"command": "rm -rf ."}, risk="critical")
        english = "\n".join(components.approval_body(Theme(mode="none", locale="en-US"), view, 60))
        self.assertIsNone(self.CHINESE.search(english), english)
        self.assertIn("allow once", english)
        chinese = "\n".join(components.approval_body(Theme(mode="none", locale="zh-CN"), view, 60))
        self.assertIn("允许一次", chinese)

    def test_the_recovery_gate_speaks_the_session_language(self):
        english = "\n".join(components.recovery_body(Theme(mode="none", locale="en-US"), {"name": "exec", "call_id": "c"}, 60))
        self.assertIsNone(self.CHINESE.search(english), english)

    def test_the_palette_speaks_the_session_language(self):
        app = App(StreamSurface(io.StringIO()), theme=Theme(mode="none", locale="en-US"))
        app._handle_key("palette", lambda text: None)
        rendered = "\n".join(app.modal.palette_lines(app.state.theme, 80))
        self.assertIsNone(self.CHINESE.search(rendered), rendered)
        self.assertIn("Session", rendered)
        self.assertIn("Commands", rendered)

    def test_the_completion_popup_speaks_the_session_language(self):
        from fun.ui.completion import Candidate

        rendered = "\n".join(components.completion_menu(Theme(mode="none", locale="en-US"), [Candidate("/help", "show help")], 0, 60))
        self.assertIsNone(self.CHINESE.search(rendered), rendered)

    def test_every_ui_key_exists_in_both_locales(self):
        from fun.i18n import TEXT

        english, chinese = set(TEXT["en-US"]), set(TEXT["zh-CN"])
        self.assertEqual(english, chinese)
        self.assertTrue({key for key in english if key.startswith("ui_")})

    def test_switching_theme_keeps_the_locale(self):
        from fun.ui.theme import Theme as T

        current = T(mode="none", name="sky", locale="zh-CN")
        switched = T(current.mode, current.unicode, "ember", current.locale)
        self.assertEqual(switched.locale, "zh-CN")


class ScrollTests(unittest.TestCase):
    """PgUp/PgDn did nothing: scrolling dropped items from the front while
    overflow handling kept the tail, so the visible window never moved."""

    def _state(self, count=40):
        state = UiState(theme=PLAIN)
        for index in range(count):
            state.add_user(f"message {index}")
        return state

    def _visible(self, state, width=70, height=20):
        return [line for line in (strip_ansi(row) for row in state.compose(width, height)) if "message" in line]

    def test_the_bottom_shows_the_newest(self):
        state = self._state()
        self.assertIn("message 39", self._visible(state)[-1])

    def test_scrolling_back_reveals_older_content(self):
        state = self._state()
        first = self._visible(state)[0]
        state.scroll(-5)
        self.assertNotEqual(self._visible(state)[0], first)
        state.scroll(-999)
        self.assertIn("message 0", self._visible(state)[0])

    def test_scrolling_forward_returns_to_the_newest(self):
        state = self._state()
        state.scroll(-999)
        state.scroll(999)
        self.assertEqual(state.scroll_offset, 0)
        self.assertIn("message 39", self._visible(state)[-1])

    def test_the_offset_is_clamped_to_what_exists(self):
        state = self._state(count=3)
        state.scroll(-999)
        self.assertIn("message 0", self._visible(state)[0])
        self.assertEqual(state.scroll_offset, 0, "a view that fits has nothing above it")

    def test_a_scrolled_view_says_how_much_is_above_it(self):
        state = self._state()
        state.scroll(-10)
        frame = "\n".join(strip_ansi(row) for row in state.compose(70, 20))
        self.assertIn("PgUp/PgDn", frame)

    def test_scrolling_works_with_the_rail_shown(self):
        state = self._state()
        state.set_plan(["a"], ["active"])
        state.scroll(-999)
        rows = [line for line in (strip_ansi(row) for row in state.compose(120, 20)) if "message" in line]
        self.assertIn("message 0", rows[0])


class ApprovalCardTests(unittest.TestCase):
    def test_an_approval_does_not_create_a_second_card(self):
        """The phantom carried the Runtime's internal subject and str(Risk...),
        was never settled, and so froze scrollback flushing for the session."""
        from fun.policy import Risk
        from fun.ui.app import ApprovalRequest

        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        app.state.tool_status("tool.started", {"call_id": "call_1", "name": "exec", "arguments": {"command": "ls -la"}})
        app.state.tool_status("approval.pending", {"call_id": "call_1", "name": "exec", "risk": "medium", "arguments": {"command": "ls -la"}})
        app.post("approval", ApprovalRequest("exec:ls", Risk.MEDIUM, {"command": "ls -la"}))
        app._consume()
        cards = [item.tool for item in app.state.transcript if item.tool]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].call_id, "call_1")
        self.assertEqual(cards[0].name, "exec")
        self.assertEqual(cards[0].arguments, {"command": "ls -la"})

    def test_flushing_continues_after_an_approval(self):
        from fun.policy import Risk
        from fun.ui.app import ApprovalRequest

        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        app.state.tool_status("tool.started", {"call_id": "c", "name": "exec", "arguments": {}})
        app.post("approval", ApprovalRequest("exec:ls", Risk.MEDIUM, {}))
        app._consume()
        app.state.tool_status("tool.completed", {"call_id": "c", "text": "ok", "elapsed_ms": 1})
        app.post("assistant", "after the approval")
        app._consume()
        app.state.mode = "ready"
        flushed = "\n".join(app.state.flush(80))
        self.assertIn("after the approval", flushed)
        self.assertNotIn("Risk.", flushed)
        self.assertNotIn("exec:ls", flushed)


class LivePlanTests(unittest.TestCase):
    """The plan only reached the UI after the whole turn — exactly when it had
    stopped being useful."""

    def _runtime(self, directory, provider):
        from fun.runtime import Runtime

        return Runtime(directory, "auto", provider=provider, state_dir=directory)

    def test_the_plan_and_its_progress_arrive_during_the_turn(self):
        import json
        import tempfile
        from pathlib import Path

        class Provider:
            def __init__(self):
                self.calls = 0

            def stream(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield {"choices": [{"delta": {"plan": ["read", "edit", "test"]}}]}
                    yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}}]}}]}
                else:
                    yield {"choices": [{"delta": {"content": "done"}}]}

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a.py").write_text("x = 1\n", encoding="utf-8")
            runtime = self._runtime(directory, Provider())
            updates: list[tuple[list[str], list[str]]] = []
            runtime.on_plan = lambda steps, statuses: updates.append((list(steps), list(statuses)))
            runtime.create_task("look")
            runtime.run_model_turn()
            self.assertGreaterEqual(len(updates), 2, "no plan update arrived before the turn ended")
            self.assertEqual(updates[0][0], ["read", "edit", "test"])
            self.assertIn("done", updates[-1][1])
            runtime.stop()

    def test_a_failing_plan_listener_cannot_break_the_turn(self):
        import tempfile

        class Provider:
            def stream(self, messages, tools=None):
                yield {"choices": [{"delta": {"plan": ["one", "two"]}}]}
                yield {"choices": [{"delta": {"content": "ok"}}]}

        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, Provider())
            runtime.on_plan = lambda steps, statuses: 1 / 0
            runtime.create_task("look")
            self.assertEqual(runtime.run_model_turn(), "ok")
            runtime.stop()


class ScrollUsabilityTests(unittest.TestCase):
    """Three complaints that were one story: the wheel destroyed drafts."""

    def _state(self, count=40):
        state = UiState(theme=PLAIN)
        for index in range(count):
            state.add_user(f"message {index}")
        return state

    def _rows(self, state, width=70, height=14):
        return [strip_ansi(line) for line in state.compose(width, height)]

    def test_the_scroll_banner_does_not_eat_a_line_of_content(self):
        state = self._state()
        before = [row for row in self._rows(state) if "message" in row]
        state.scroll(-5)
        rows = self._rows(state)
        self.assertIn("PgUp", rows[0])
        self.assertNotIn("message", rows[0])
        after = [row for row in rows[1:] if "message" in row]
        # The banner costs one row of the viewport; it must not overwrite a row
        # of the conversation, which is what it used to do.
        self.assertGreaterEqual(len(after), len(before) - 2)
        self.assertNotIn(before[0].strip(), rows[0])

    def test_the_view_is_held_still_while_reading_back(self):
        state = self._state()
        state.scroll(-5)
        top = [row for row in self._rows(state)[1:] if "message" in row][0]
        for index in range(3):
            state.add_assistant(f"a reply that arrived while reading {index}")
            state.add_user(f"and another {index}")
        held = [row for row in self._rows(state)[1:] if "message" in row][0]
        self.assertEqual(held, top, "an arriving reply dragged the reader forward")

    def test_arrivals_are_counted_while_the_view_is_held(self):
        state = self._state()
        state.scroll(-5)
        state.add_assistant("something new")
        self.assertIn("↓", self._rows(state)[0])

    def test_returning_to_the_bottom_shows_what_arrived(self):
        state = self._state()
        state.scroll(-5)
        state.add_assistant("something new")
        state.scroll(99999)
        self.assertIsNone(state.scroll_anchor)
        self.assertIn("something new", "\n".join(self._rows(state)))

    def test_the_wheel_scrolls_instead_of_being_read_as_escape(self):
        from fun.ui.input import read_key

        for payload, expected in ((b"\x1b[<64;10;5M", "wheel_up"), (b"\x1b[<65;10;5M", "wheel_down"), (b"\x1b[M\x60\x21\x21", "wheel_up")):
            reader, writer = os.pipe()
            os.write(writer, payload)
            self.assertEqual(read_key(reader), expected, payload)

    def test_a_mouse_click_is_ignored_rather_than_closing_a_dialog(self):
        from fun.ui.input import read_key

        reader, writer = os.pipe()
        os.write(writer, b"\x1b[<0;10;5M")
        self.assertEqual(read_key(reader), "mouse")
        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        app._handle_key("palette", lambda text: None)
        app._handle_key("mouse", lambda text: None)
        self.assertIsNotNone(app.modal, "a click closed the palette")

    def test_the_wheel_moves_the_transcript(self):
        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        for index in range(40):
            app.post("user", f"row {index}")
        app._consume()
        app._handle_key("wheel_up", lambda text: None)
        self.assertGreater(app.state.scroll_offset, 0)
        app._handle_key("wheel_down", lambda text: None)
        app._handle_key("wheel_down", lambda text: None)
        self.assertEqual(app.state.scroll_offset, 0)


class ConfigureJourneyTests(unittest.TestCase):
    """Endpoint → key → pick from the real model list, in one pass."""

    def test_config_hands_the_form_straight_to_a_loaded_model_picker(self):
        import tempfile
        from unittest.mock import patch

        from fun.commands import Session, dispatch
        from fun.config import FunConfig
        from fun.frontends import AppFrontend
        from fun.runtime import Runtime

        directory = tempfile.mkdtemp()
        app = App(StreamSurface(io.StringIO()), theme=PLAIN)
        runtime = Runtime(directory, "auto", state_dir=directory)
        session = Session(runtime, FunConfig(), os.path.join(directory, "config.json"))
        try:
            with patch("fun.config._keychain_set", return_value=False), patch("fun.config._keychain_get", return_value=""), patch(
                "fun.provider.OpenAICompatible.list_models", return_value=["gpt-4o", "claude-opus", "claude-haiku"]
            ):
                dispatch("/config", session, AppFrontend(app, "zh-CN"))
                self.assertEqual([name for name, _ in app.modal.fields], ["base_url", "api_key"], "the model is not typed here any more")
                for char in "https://x/v1":
                    app.modal.handle(char)
                app.modal.handle("enter")
                for char in "sk-abc":
                    app.modal.handle(char)
                app._handle_key("enter", lambda *_: None)
                self.assertEqual(app.modal.kind, "select")
                deadline = time.time() + 5
                while app.modal.loading and time.time() < deadline:
                    app._consume()
                    time.sleep(0.01)
                self.assertEqual(app.modal.options, ["gpt-4o", "claude-opus", "claude-haiku"])
                self.assertTrue(app.modal.multi)
                for char in "claude":
                    app.modal.handle(char)
                self.assertEqual(app.modal.visible(), ["claude-opus", "claude-haiku"])
                app.modal.handle(" ")
                app.modal.handle("down")
                app.modal.handle(" ")
                app._handle_key("enter", lambda *_: None)
            self.assertEqual(session.model, "claude-opus")
            self.assertEqual(FunConfig.load(session.config_path).models, ["claude-opus", "claude-haiku"])
        finally:
            runtime.stop()


class MentionTests(unittest.TestCase):
    """`@ files` has to mean something, and survive a space in the name."""

    def test_mentions_are_read_back_including_quoted_paths(self):
        from fun.ui.completion import mention_token, mentions

        self.assertEqual(mentions('看 @a.py 和 @"src/my file.py"，还有 @b.md。'), ["a.py", "src/my file.py", "b.md"])
        self.assertEqual(mentions("mail me at a@b.com"), [], "an email address is not a file reference")
        self.assertEqual(mention_token("src/my file.py"), '@"src/my file.py"')
        self.assertEqual(mention_token("a.py"), "@a.py")

    def test_completing_a_path_with_a_space_stays_one_reference(self):
        import tempfile
        from fun.ui.completion import Completer, FileIndex

        directory = tempfile.mkdtemp()
        open(os.path.join(directory, "my file.py"), "w").close()
        completer = Completer(files=FileIndex(directory))
        from fun.ui.completion import detect

        text = "看 @my"
        context = detect(text, len(text))
        spliced, cursor = completer.apply(text, context, "my file.py")
        self.assertEqual(spliced, '看 @"my file.py" ')
        from fun.ui.completion import mentions

        self.assertEqual(mentions(spliced), ["my file.py"])
        # And the cursor still sits after the reference, not inside it.
        self.assertEqual(spliced[:cursor], '看 @"my file.py" ')

    def test_a_referenced_file_is_named_for_the_model_and_a_missing_one_for_the_user(self):
        import tempfile
        from fun.frontends import attach_mentions

        directory = tempfile.mkdtemp()
        open(os.path.join(directory, "real.py"), "w").close()
        sent, missing = attach_mentions("看下 @real.py 和 @ghost.py", directory)
        self.assertIn("- real.py", sent)
        self.assertNotIn("- ghost.py", sent)
        self.assertEqual(missing, ["ghost.py"])
        # A path that climbs out of the workspace is reported, never attached.
        _, escaped = attach_mentions("@../../etc/passwd", directory)
        self.assertEqual(escaped, ["../../etc/passwd"])
        # An ordinary message is passed through untouched.
        self.assertEqual(attach_mentions("普通消息", directory), ("普通消息", []))


class ProviderErrorMessageTests(unittest.TestCase):
    """No screen ever shows a bare error tag."""

    def test_every_provider_tag_becomes_a_sentence(self):
        import re

        from fun.frontends import friendly_error
        from fun.provider import ProviderError

        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fun", "provider.py"), encoding="utf-8") as handle:
            source = handle.read()
        tags = sorted(set(re.findall(r'ProviderError\("([A-Z_]+)"', source)))
        self.assertTrue(tags)
        for tag in tags:
            for locale in ("zh-CN", "en-US"):
                message = friendly_error(ProviderError(tag), locale)
                self.assertNotEqual(message, tag, f"{tag} is printed raw at {locale}")
                self.assertGreater(len(message), len(tag) // 2)

    def test_an_http_failure_says_which_status_and_what_to_do(self):
        from fun.frontends import friendly_error
        from fun.provider import ProviderError

        seen = set()
        for status in (404, 429, 500, 400):
            message = friendly_error(ProviderError("PROVIDER_HTTP_FAILED", status=status), "zh-CN")
            self.assertIn(str(status), message)
            seen.add(message)
        self.assertEqual(len(seen), 4, "different statuses must not collapse into one message")

    def test_an_unknown_tag_still_reads_as_a_sentence(self):
        from fun.frontends import friendly_error
        from fun.provider import ProviderError

        message = friendly_error(ProviderError("PROVIDER_SOMETHING_NEW"), "zh-CN")
        self.assertTrue(message.startswith(PLAIN.text("provider_unavailable")))
        self.assertIn("PROVIDER_SOMETHING_NEW", message)


class RecoveryPanelTests(unittest.TestCase):
    """The screen you meet after a crash has to explain itself."""

    def _panel(self, locale="zh-CN", **pending):
        from fun.ui import components
        from fun.ui.state import UiState

        state = UiState(theme=Theme(mode="none", locale=locale))
        state.set_recovery({"name": "exec", "call_id": "c9", "arguments": {"command": "git push origin main"}, "goal": "把改动推上去", **pending})
        return "\n".join(components.recovery_body(state.theme, state.recovery, 70))

    def test_it_says_what_happened_and_what_had_been_asked(self):
        panel = self._panel()
        self.assertIn(PLAIN.text("ui_recovery_needed"), panel)
        self.assertIn("把改动推上去", panel)
        self.assertIn("git push origin main", panel)

    def test_arguments_are_rendered_not_repred(self):
        panel = self._panel()
        self.assertNotIn("{'command'", panel)
        self.assertNotIn("command=", panel, "the identifying argument speaks for itself")

    def test_every_choice_says_what_it_will_do(self):
        for locale in ("zh-CN", "en-US"):
            theme = Theme(mode="none", locale=locale)
            panel = self._panel(locale)
            for key in ("resume", "discard", "mark_failed", "stop"):
                self.assertIn(theme.text(f"ui_recovery_{key}_why"), panel, (locale, key))

    def test_resuming_warns_that_it_runs_the_command_again(self):
        # This is the whole risk of the screen: the call may already have run.
        for locale in ("zh-CN", "en-US"):
            self.assertIn(Theme(mode="none", locale=locale).text("ui_recovery_resume_why"), self._panel(locale))

    def test_the_composer_stops_inviting_input_while_it_blocks(self):
        from fun.ui.state import UiState

        for mode, key in (("recovery", "ui_composer_recovery"), ("approval", "ui_composer_approval")):
            state = UiState(theme=PLAIN)
            state.mode = mode
            frame = "\n".join(strip_ansi(line) for line in state.compose(70, 20))
            self.assertIn(PLAIN.text(key), frame, mode)
            self.assertNotIn(PLAIN.text("ui_composer_placeholder"), frame, mode)

    def test_a_missing_goal_does_not_leave_a_dangling_label(self):
        panel = self._panel(goal="")
        self.assertNotIn(PLAIN.text("ui_recovery_goal").split("{")[0].strip(), panel)
