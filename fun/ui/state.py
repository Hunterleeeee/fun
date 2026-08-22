"""The UI state model a frontend renders.

This module owns *presentation* state only — a transcript, live tool cards, the
composer draft — and never Runtime state.  Keeping the two apart is what lets
the same state object drive both the streaming and the fullscreen frontend, and
lets tests assert on rendered output without starting a Runtime.

The model also tracks which transcript items have already been flushed into the
terminal's scrollback, so the streaming frontend can print new content exactly
once instead of repainting the whole conversation every frame.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import components, sidebar
from .components import ToolView
from .editor import Editor
from .layout import Spine, dock_panel, hero_block, intro, mode_tabs
from .text import display_width, sanitize, truncate, wrap
from .theme import RESET, REVERSE, Theme

BACKGROUND_FIELDS = ("id", "status", "goal", "result", "error")
BACKGROUND_FIELD_LIMIT = 4000


def normalize_background(item: dict[str, Any]) -> dict[str, str]:
    """Trim a background task to the fields and lengths the UI stores.

    The caller compared its own untrimmed dicts against these trimmed ones, so
    any task with a goal over the limit never compared equal and the loop
    reposted and repainted on every pass for the life of that task.
    """
    return {key: sanitize(str(item.get(key, "")))[:BACKGROUND_FIELD_LIMIT] for key in BACKGROUND_FIELDS}


TOOL_STATUS_MAP = {
    "tool.started": "queued",
    "tool.requested": "queued",
    "approval.pending": "approval",
    "approval.resolved": "running",
    "tool.executing": "running",
    "tool.progress": "running",
    "tool.completed": "completed",
    "tool.failed": "failed",
}


@dataclass
class ToolCard:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    elapsed_ms: int | None = None
    output: str = ""
    error: str = ""
    risk: str = ""
    exit_code: int | None = None

    def update(self, status: str, payload: dict[str, Any]) -> None:
        self.status = status
        if isinstance(payload.get("elapsed_ms"), int):
            self.elapsed_ms = payload["elapsed_ms"]
        if isinstance(payload.get("text"), str):
            self.output = sanitize(payload["text"][:4000])
        if isinstance(payload.get("error"), str):
            self.error = sanitize(payload["error"][:240])
        if isinstance(payload.get("risk"), str):
            self.risk = payload["risk"]
        if isinstance(payload.get("exit_code"), int):
            self.exit_code = payload["exit_code"]
        if payload.get("arguments"):
            self.arguments = dict(payload["arguments"])

    def view(self) -> ToolView:
        return ToolView(self.name, self.status, self.arguments, self.elapsed_ms, self.exit_code, self.output, self.error, self.risk)


@dataclass
class TranscriptItem:
    role: str
    text: str = ""
    tool: ToolCard | None = None
    command: bool = False
    footer: str = ""

    @property
    def settled(self) -> bool:
        """Whether this item can still change and therefore must not be flushed."""
        if self.tool is not None:
            return self.tool.status in {"completed", "failed"}
        return True


@dataclass
class UiState:
    """Presentation state for one interactive session."""

    locale: str = "en-US"
    theme: Theme = field(default_factory=Theme)
    editor: Editor = field(default_factory=Editor)
    mode: str = "ready"
    status_text: str = ""
    model_name: str = ""
    task_state: str = "idle"
    approval_mode: str = "smart"
    #: Whether a provider is actually configured.  The empty screen used to
    #: look identical with and without credentials, so a first run was a logo,
    #: an input box, and no hint that nothing could possibly happen yet.
    provider_ready: bool = True
    agent_mode: str = "Build"
    workspace: str = ""
    version: str = ""
    session_label: str = ""
    usage_text: str = ""
    transcript: list[TranscriptItem] = field(default_factory=list)
    tools: dict[str, ToolCard] = field(default_factory=dict)
    plan: list[str] = field(default_factory=list)
    plan_status: list[str] = field(default_factory=list)
    scroll_offset: int = 0
    background: list[dict[str, str]] = field(default_factory=list)
    recovery: dict[str, str] | None = None
    collapsed_tools: set[str] = field(default_factory=set)
    expanded_tools: set[str] = field(default_factory=set)
    show_plan: bool = True
    show_sidebar: bool = True
    goal: str = ""
    toast: str = ""
    toast_ticks: int = 0
    spinner_tick: int = 0
    flushed: int = 0
    completion: Any = None
    hidden_items: int = 0
    # How much of the transcript existed when the reader scrolled away from the
    # bottom.  While set, the view is frozen at that point: new messages pile up
    # below and are counted, rather than shoving the page forward line by line
    # while someone is reading.
    scroll_anchor: int | None = None
    cursor_hint: tuple[int, int] | None = None
    dock_caret: tuple[int, int] | None = None
    real_cursor: bool = True

    # ---------------------------------------------------------------- mutation

    @property
    def composer(self) -> str:
        """The composer draft.  Kept as a property so older callers still work."""
        return self.editor.text

    @composer.setter
    def composer(self, value: str) -> None:
        self.editor.set(value)

    @property
    def composer_history(self) -> list[str]:
        return self.editor.history

    def add_user(self, text: str) -> None:
        text = sanitize(text).strip()
        if not text:
            return
        self.transcript.append(TranscriptItem("user", text))
        if not self.editor.history or self.editor.history[-1] != text:
            self.editor.history.append(text)
        self.editor.history_index = None

    def add_command(self, text: str) -> None:
        """Record a slash command, history included.

        Only ``add_user`` appended to history, so ↑ recalled prompts but never
        commands — the one thing most worth repeating.
        """
        text = text.strip()
        if not text:
            return
        self.transcript.append(TranscriptItem("user", text, command=True))
        if not self.editor.history or self.editor.history[-1] != text:
            self.editor.history.append(text)
        self.editor.history_index = None

    def add_assistant(self, text: str) -> None:
        """Append streamed assistant text, coalescing into the current message."""
        text = sanitize(text)
        if not text:
            return
        if self.transcript and self.transcript[-1].role == "assistant":
            self.transcript[-1].text += text
        else:
            self.transcript.append(TranscriptItem("assistant", text))

    def set_turn_footer(self, text: str) -> None:
        """Stamp the finished reply with what produced it.

        It hangs off the assistant message rather than living in the dock so it
        scrolls with the turn it describes — reading back, "which mode was I in
        when it said that" is answerable without reconstructing the session.
        """
        for item in reversed(self.transcript):
            if item.role == "assistant":
                item.footer = text
                return

    def add_system(self, text: str) -> None:
        text = sanitize(text)
        if text:
            self.transcript.append(TranscriptItem("system", text))

    def set_plan(self, steps: list[str], statuses: list[str] | None = None) -> None:
        self.plan = list(steps)
        self.plan_status = list(statuses or [])

    def set_recovery(self, pending: dict[str, Any] | None) -> None:
        if pending:
            # ``arguments`` is rendered here rather than stringified: a raw dict
            # repr put braces, quotes and a key name in front of the one thing
            # the person needs to read, which is the command itself.
            name = str(pending.get("name", ""))
            raw = pending.get("arguments") or {}
            rendered = components._format_arguments(raw, 300, name) if isinstance(raw, dict) else str(raw)
            self.recovery = {
                "name": name[:300],
                "call_id": str(pending.get("call_id", ""))[:300],
                "arguments": rendered[:300],
                "goal": str(pending.get("goal", ""))[:300],
                "reason": str(pending.get("reason", ""))[:300],
            }
        else:
            self.recovery = None
        if pending:
            self.mode = "recovery"
            self.task_state = "recovery"

    def set_background(self, tasks: list[dict[str, str]]) -> None:
        self.background = [normalize_background(item) for item in tasks]

    def tool_status(self, kind: str, payload: dict[str, Any]) -> ToolCard | None:
        call_id = str(payload.get("call_id", ""))
        if not call_id:
            return None
        card = self.tools.get(call_id)
        if card is None:
            card = ToolCard(call_id, str(payload.get("name", "tool")), dict(payload.get("arguments") or {}))
            self.tools[call_id] = card
            self.transcript.append(TranscriptItem("tool", tool=card))
        card.update(TOOL_STATUS_MAP.get(kind, kind), payload)
        return card

    def restore_messages(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                self.add_user(content)
            elif role == "assistant" and isinstance(content, str) and content:
                self.add_assistant(content)
            elif role == "tool" and isinstance(content, str):
                call_id = str(message.get("tool_call_id", "restored-tool"))
                self.tool_status("tool.completed", {"call_id": call_id, "name": "tool", "text": content})

    def toggle_output(self, call_id: str | None = None) -> None:
        """Collapse or expand a tool's output; defaults to the most recent tool."""
        if call_id is None:
            if not self.tools:
                return
            call_id = next(reversed(self.tools))
        # The first press always shows *more*.  Toggling into "collapsed" first
        # meant Ctrl-O on a quiet card — the common case, a successful read —
        # appeared to do nothing at all.
        if call_id in self.expanded_tools:
            self.expanded_tools.discard(call_id)
            self.collapsed_tools.add(call_id)
        else:
            self.expanded_tools.add(call_id)
            self.collapsed_tools.discard(call_id)

    def history(self, direction: int) -> str:
        """Step through composer history; kept for callers predating the editor."""
        if direction < 0:
            self.editor.history_previous()
        else:
            self.editor.history_next()
        return self.editor.text

    def scroll(self, delta: int) -> int:
        """Move the viewport by ``delta`` rows.  Negative scrolls back.

        Counted in *rendered rows from the bottom*, not in transcript items.
        The old model dropped items from the front while overflow handling kept
        the tail — so scrolling back could never change what was on screen, and
        scrolling forward eventually hid the newest messages instead.  The real
        ceiling depends on the rendered height, so it is clamped in ``compose``.

        ``delta`` follows the arrow: PgUp passes a negative delta and means
        "further back", which is a *larger* distance from the bottom.
        """
        self.scroll_offset = max(0, self.scroll_offset - delta)
        if self.scroll_offset and self.scroll_anchor is None:
            self.scroll_anchor = len(self.transcript)
        elif not self.scroll_offset:
            self.scroll_anchor = None
        return self.scroll_offset

    def animating(self) -> bool:
        """Whether the frame changes on its own, independent of input."""
        return self.mode == "working" or bool(self.toast)

    def tick(self) -> None:
        self.spinner_tick += 1
        if self.toast:
            self.toast_ticks += 1
            if self.toast_ticks > 12:
                self.toast = ""
                self.toast_ticks = 0

    # -------------------------------------------------------------- rendering

    def item_lines(self, item: TranscriptItem, width: int) -> list[str]:
        """Render one transcript item as a spine node plus its detail."""
        spine = Spine(self.theme, width)
        self.write_item(spine, item)
        return spine.lines

    def write_item(self, spine: Spine, item: TranscriptItem) -> None:
        """Append one item onto an existing spine."""
        theme, body_width = self.theme, spine.body_width
        if item.role == "user":
            if item.command:
                spine.node("system", theme.style(item.text, "faint"))
                return
            spine.node("user", theme.style(theme.text("ui_you"), "user", bold=True))
            spine.body([theme.style(piece, "text") for piece in wrap(item.text, body_width)])
            return
        if item.role in {"assistant", "system"}:
            label, tone = ("Fun", "accent") if item.role == "assistant" else ("系统", "muted")
            spine.node(item.role, theme.style(label, tone, bold=True))
            if item.role == "assistant":
                from .markdown import render as render_markdown

                spine.body(render_markdown(theme, item.text, body_width))
                if item.footer:
                    # A blank line first: the receipt is metadata, and running
                    # it straight on from the last sentence reads as prose.
                    spine.body(["", theme.style(truncate(item.footer, body_width), "faint")])
            else:
                spine.body([theme.style(piece, "muted") for piece in wrap(item.text, body_width)])
            return
        if item.tool is not None:
            card = item.tool
            view = card.view()
            collapsed = card.call_id in self.collapsed_tools
            expanded = card.call_id in self.expanded_tools
            limit = 400 if expanded else 12
            arguments = components._format_arguments(view.arguments or {}, max(10, body_width - 20), view.name)
            title = theme.style(view.name, "text", bold=True)
            if arguments:
                title += "  " + theme.style(arguments, "muted")
            meta = f"{view.elapsed_ms}ms" if view.elapsed_ms is not None else ""
            if view.exit_code not in (None, 0):
                meta = f"{meta} exit {view.exit_code}".strip()
            spine.node(view.status, title, meta)
            spine.body(components.tool_body(theme, view, body_width, collapsed, limit, expanded))

    def flushable(self) -> list[TranscriptItem]:
        """Transcript items that are final and have not reached scrollback yet.

        Flushing stops at the *first* item that can still change, not only at
        the last one.  Scrollback is never repainted, so an unsettled item
        written early — a second tool card queued behind a running one — froze
        in its transient state and its real result was never shown anywhere.
        """
        pending = self.transcript[self.flushed:]
        ready: list[TranscriptItem] = []
        for index, item in enumerate(pending):
            is_tail = index == len(pending) - 1
            if not item.settled or (is_tail and item.role == "assistant" and self.mode == "working"):
                break
            ready.append(item)
        return ready

    def flush(self, width: int) -> list[str]:
        """Return lines for settled items and mark them as written."""
        ready = self.flushable()
        if not ready:
            return []
        lines: list[str] = []
        for item in ready:
            lines.extend(self.item_lines(item, width))
        self.flushed += len(ready)
        return lines

    def live_lines(self, width: int) -> list[str]:
        """Lines for the not-yet-settled tail, repainted every frame."""
        lines: list[str] = []
        for item in self.transcript[self.flushed:]:
            lines.extend(self.item_lines(item, width))
        return lines

    def status_segments(self) -> list[tuple[str, str]]:
        theme = self.theme
        state = self.task_state
        tone = {"working": "accent", "failed": "danger", "stopped": "muted", "recovery": "warning", "paused": "warning"}.get(state, "success")
        label = state
        if self.mode == "working":
            label = f"{components.spinner(theme, self.spinner_tick)} {state if state != 'idle' else 'working'}"
        elif self.mode == "approval":
            label = f"{theme.glyph('⚠', '!')} awaiting approval"
            tone = "warning"
        elif self.mode == "recovery":
            label = f"{theme.glyph('⚠', '!')} recovery required"
            tone = "warning"
        segments = [(label, tone)]
        if self.model_name:
            segments.append((self.model_name, "muted"))
        segments.append((self.approval_mode, "muted"))
        if self.usage_text:
            segments.append((self.usage_text, "faint"))
        extra = self._status_extra()
        if extra:
            segments.append((extra, "faint"))
        return segments

    def _status_extra(self) -> str:
        """Free-form status text, minus anything the segments already show."""
        raw = self.status_text.replace("·", " ")
        skip = {self.task_state, self.model_name, self.approval_mode, "working", "ready"}
        tokens = [token for token in raw.split() if token not in skip and not token.startswith(("model=", "approval=", "task="))]
        return truncate(" ".join(dict.fromkeys(tokens)), 42) if tokens else ""

    def intro_lines(self, width: int) -> list[str]:
        """The one-time opening banner used by scrollback-preserving frontends."""
        return intro(
            self.theme, width,
            version=self.version, workspace=self.workspace, model=self.model_name,
            mode=self.agent_mode, approval=self.approval_mode, session=self.session_label,
        )

    def dock_lines(self, width: int) -> list[str]:
        """The persistent bottom area: the input panel and its hint row.

        Context lives *inside* the panel rather than on a separate status bar,
        so the thing you are about to act with states its own mode, model and
        cost in one place.
        """
        theme = self.theme
        lines: list[str] = [""]
        tabs, tabs_width = mode_tabs(theme, self.agent_mode)
        if self.toast:
            lines.append(f" {theme.style(theme.glyph('✓', 'v'), 'success')} {theme.style(self.toast, 'success')}")
        if self.background:
            # Only the live ones.  Finished tasks stayed in the dock for the
            # rest of the session, squeezing the transcript out of the frame.
            live = [item for item in self.background if item.get("status") in {"created", "running", "cancel_requested"}]
            if live:
                lines.extend(components.background_block(theme, live, width))
        if self.recovery:
            spine = Spine(theme, width)
            spine.node("approval", theme.style(theme.text("ui_recovery"), "warning", bold=True))
            spine.body(components.recovery_body(theme, self.recovery, spine.body_width))
            lines.extend(spine.lines)
            lines.append("")
        # The one dock row that was never clipped: mode_tabs is a fixed 27
        # columns, so below ~29 it wrapped — and a wrapped row also made the
        # dock's recorded height disagree with the real one, which is what the
        # cursor walk counts.
        lines.append(truncate("  " + tabs, width))
        completion = self.completion
        if completion is not None and getattr(completion, "active", False):
            lines.extend(
                components.completion_menu(
                    theme, completion.candidates, completion.index, width,
                    completion.context.kind if completion.context else "command",
                )
            )
        # With a real terminal cursor the caret blinks, survives a colourless
        # terminal, and — crucially on macOS — anchors the IME candidate window
        # to the text being typed instead of to wherever output last landed.
        # Five columns of chrome: two of margin, the accent edge, two of gutter.
        # Budgeting four put the character just typed under the ellipsis and the
        # caret one column past the terminal's last column.
        body_width = max(8, width - 5)
        # While a recovery or an approval is blocking, every key except the
        # answer keys is swallowed — so the composer must not keep inviting the
        # user to type into it.
        if self.mode == "recovery":
            prompt_key = "ui_composer_recovery"
        elif self.mode == "approval":
            prompt_key = "ui_composer_approval"
        else:
            prompt_key = "ui_composer_placeholder"
        placeholder = "" if self.editor.text else theme.text(prompt_key)
        editor_lines = [theme.style(placeholder, "faint")] if placeholder else self.editor.render(
            body_width,
            cursor_style="" if self.real_cursor else (REVERSE if theme.enabled else ""),
            reset="" if self.real_cursor else (RESET if theme.enabled else ""),
            show_cursor=not self.real_cursor and self.mode == "ready",
        )
        caret_row, caret_column = self.editor.visual_lines(body_width)[1:]
        # +1 for the panel's blank first row; the column accounts for the two
        # space margin, the accent edge and the two space gutter after it.
        self.dock_caret = (len(lines) + 1 + caret_row, 5 + caret_column) if self.mode == "ready" else None
        lines.extend(
            dock_panel(
                theme,
                editor_lines,
                width,
                mode=self.agent_mode,
                model=self.model_name,
                approval=self.approval_mode,
                usage=self.usage_text,
                state=self.task_state,
                spinner=components.spinner(theme, self.spinner_tick) if self.mode == "working" else "",
            )
        )
        lines.append(" " + components.hint_bar(theme, self.dock_hints(width), max(8, width - 1)))
        return lines

    def hints(self) -> list[tuple[str, str]]:
        theme = self.theme
        if self.mode == "approval":
            return [("y", theme.text("ui_hint_allow")), ("a", theme.text("ui_hint_session")), ("n", theme.text("ui_hint_deny"))]
        if self.mode == "recovery":
            # The composer is inert until this is answered, so these are the
            # only keys worth advertising.
            return [("r", theme.text("ui_hint_resume")), ("d", theme.text("ui_hint_discard")), ("f", theme.text("ui_hint_fail")), ("s", theme.text("ui_hint_stop"))]
        base = [("Enter", theme.text("ui_hint_send")), ("Ctrl-N", theme.text("ui_hint_newline")), ("/", theme.text("ui_hint_commands"))]
        if self.tools:
            base.append(("Ctrl-O", theme.text("ui_hint_output")))
        base.append(("Ctrl-C", theme.text("ui_hint_cancel" if self.editor.text or self.mode == "working" else "ui_hint_exit")))
        return base

    def dock_hints(self, width: int) -> list[tuple[str, str]]:
        """Hints plus the ones that only exist at this width."""
        hints = self.hints()
        if self.mode != "ready":
            return hints
        # The palette is where every command is discoverable, and it was the one
        # key never mentioned anywhere on screen.
        if width >= 64:
            hints.insert(-1, ("Ctrl-P", self.theme.text("ui_hint_palette")))
        if sidebar.fits(width) and (self.transcript or self.plan):
            hints.insert(-1, ("Ctrl-T", self.theme.text("ui_hint_sidebar")))
        return hints

    def body_lines(self, width: int, height: int | None = None, with_plan: bool = True, budget: int | None = None) -> list[str]:
        """The scrollable region: the event spine, or the hero when empty.

        ``budget`` is how many rows the caller can actually show.  Rendering the
        whole transcript and then throwing most of it away cost time
        proportional to the *history*, on every repaint — 70 ms a frame at 800
        messages, which is felt as typing lag.  With a budget, only enough items
        from the end are rendered to fill it.
        """
        theme = self.theme
        if not self.transcript and not self.plan and height:
            self.hidden_items = 0
            return hero_block(theme, width, height, self.version, needs_setup=not self.provider_ready)
        visible = self.transcript
        if self.scroll_anchor is not None:
            visible = self.transcript[: self.scroll_anchor]
        if budget is not None and len(visible) > 4:
            visible = self._tail_for(visible, width, budget, with_plan)
        self.hidden_items = max(0, (self.scroll_anchor if self.scroll_anchor is not None else len(self.transcript)) - len(visible))
        spine = Spine(theme, width)
        for index, item in enumerate(visible):
            if index:
                spine.gap()
            self.write_item(spine, item)
        if with_plan and self.show_plan and self.plan:
            spine.gap()
            done = sum(1 for status in self.plan_status if status == "done")
            spine.node("plan", theme.style("Plan", "text", bold=True), f"{done}/{len(self.plan)}")
            spine.body(components.plan_body(theme, self.plan, self.plan_status, spine.body_width))
        return spine.lines

    def rail_visible(self, width: int) -> bool:
        """Whether the right rail is drawn at this width.

        It is suppressed on the empty start screen: that view is a centred
        composition, and a rail reporting "no plan, no events, no background"
        beside it would be a column of absences.
        """
        return bool(self.show_sidebar and sidebar.fits(width) and (self.transcript or self.plan))

    def toggle_sidebar(self) -> bool:
        self.show_sidebar = not self.show_sidebar
        return self.show_sidebar

    def _tail_for(self, items: list[TranscriptItem], width: int, budget: int, with_plan: bool) -> list[TranscriptItem]:
        """The shortest suffix of ``items`` that can fill ``budget`` rows."""
        count = max(4, budget // 4)
        while count < len(items):
            if self._rows_for(items[-count:], width, with_plan) >= budget:
                return items[-count:]
            count *= 2
        return items

    def _rows_for(self, items: list[TranscriptItem], width: int, with_plan: bool) -> int:
        spine = Spine(self.theme, width)
        for index, item in enumerate(items):
            if index:
                spine.gap()
            self.write_item(spine, item)
        return len(spine.lines) + (len(self.plan) + 2 if with_plan and self.show_plan and self.plan else 0)

    def is_idle(self) -> bool:
        """Whether the session has nothing to show but its start screen."""
        return (
            self.mode == "ready"
            and not self.transcript
            and not self.plan
            and not self.background
            and not self.recovery
            and not self.toast
        )

    def _window(self, body: list[str], room: int, width: int) -> list[str]:
        """The visible slice of a body taller than the viewport.

        ``scroll_offset`` is clamped here because only here is the rendered
        height known; a banner names how many rows are above the window, so a
        scrolled view never looks like the whole conversation.
        """
        theme = self.theme
        ceiling = max(0, len(body) - room)
        self.scroll_offset = min(self.scroll_offset, ceiling)
        end = len(body) - self.scroll_offset
        hidden = max(0, end - room) + self.hidden_items
        arrived = len(self.transcript) - self.scroll_anchor if self.scroll_anchor is not None else 0
        if not hidden and not arrived:
            return body[max(0, end - room):end]
        # The banner takes a row of its own.  Overwriting the first visible line
        # with it silently ate a line of the conversation on every scroll.
        label = theme.text("ui_scrolled", count=hidden)
        if arrived:
            label += theme.text("ui_arrived", count=arrived)
        window = body[max(0, end - room + 1):end]
        return [f"  {theme.style(label, 'faint')}"] + window

    def _fit_frame(self, dock: list[str], overlay: list[str], height: int, width: int) -> tuple[list[str], list[str], int]:
        """Divide ``height`` rows between dock, overlay and body.

        The composer is the one thing that must never be cut.  ``room`` used to
        be floored at 1 while the dock kept its full height, so ``compose``
        returned more rows than it was asked for and the frame then discarded
        the surplus from the *bottom* — at height 8 the hint bar and a panel row
        vanished, at height 4 there was no visible input at all.  So the dock is
        served first and trimmed from the top, where the toast and the
        background list live, rather than from the bottom.
        """
        height = max(1, height)
        # Width too, not only row count: the overlay was passed through verbatim,
        # so a wide dialog broke compose's "no line exceeds width" contract and
        # only survived because one downstream writer happened to clip.
        overlay = [truncate(line, width) for line in overlay]
        if len(dock) > height:
            dock = dock[len(dock) - height:]
        spare = height - len(dock)
        if len(overlay) > spare:
            overlay = overlay[:max(0, spare)]
        return dock, overlay, max(0, height - len(dock) - len(overlay))

    def centre_dock(self, dock: list[str], width: int) -> tuple[list[str], int]:
        """Centre the dock block horizontally, returning it and the shift used."""
        block = max((display_width(line) for line in dock), default=0)
        offset = max(0, (width - block) // 2)
        if not offset:
            return dock, 0
        pad = " " * offset
        return [pad + line if line.strip() else line for line in dock], offset

    def compose(self, width: int, height: int, reserved: list[str] | None = None) -> list[str]:
        """Lay out body, any reserved overlay, and the dock into one frame.

        The overlay is given its own rows rather than being painted over the
        transcript: a dialog that erases the content behind it looks like a
        rendering fault, not like a surface.
        """
        width = max(32, width)
        dock = self.dock_lines(width)
        shift = 0
        if not self.transcript and not self.plan:
            # An empty session is a centred composition; a left-aligned input
            # block under a centred wordmark reads as two unrelated screens.
            dock, shift = self.centre_dock(dock, width)
            dock = dock + [""]
        overlay = list(reserved or [])
        dock, overlay, room = self._fit_frame(dock, overlay, height, width)
        if self.rail_visible(width) and room:
            # The rail takes the plan with it rather than duplicating it: two
            # copies of the same list on one screen is worse than either alone.
            column = width - sidebar.rail_width(width) - 3
            body = self.body_lines(column, room, with_plan=False, budget=room + self.scroll_offset + 2)
            if len(body) > room:
                body = self._window(body, room, column)
            else:
                self.scroll_offset = 0
            # Both columns are padded to the full height *before* the join, so
            # the divider is a continuous rule rather than stopping wherever the
            # shorter column happened to end.
            body += [""] * max(0, room - len(body))
            rail = sidebar.rail(self.theme, self, sidebar.rail_width(width), room)
            rail += [""] * max(0, room - len(rail))
            body = sidebar.split(self.theme, body, rail, column, width)
            self.cursor_hint = (
                (len(body) + len(overlay) + self.dock_caret[0], self.dock_caret[1] + shift)
                if self.dock_caret is not None else None
            )
            return body + overlay + dock
        body = self.body_lines(width, room, budget=room + self.scroll_offset + 2) if room else []
        if len(body) > room:
            body = self._window(body, room, width)
        else:
            # Everything fits, so there is nothing above the window; leaving a
            # stale offset would make the view jump the moment it grew.
            self.scroll_offset = 0
            # Short sessions read from the top.  Padding above instead would
            # leave the screen looking empty with the conversation pinned to the
            # bottom edge; the slack belongs between the last event and the input.
            body = body + [""] * max(0, room - len(body))
        # Absolute frame coordinates, so a surface never has to guess which
        # layout the caret was measured against.
        self.cursor_hint = (
            (len(body) + len(overlay) + self.dock_caret[0], self.dock_caret[1] + shift)
            if self.dock_caret is not None else None
        )
        return body + overlay + dock

    def render(self, width: int = 88, height: int | None = None) -> str:
        """Render a full frame.  There is exactly one layout: body over dock."""
        width = max(32, width)
        if height is None:
            body = self.body_lines(width)
            dock = self.dock_lines(width)
            self.cursor_hint = (len(body) + self.dock_caret[0], self.dock_caret[1]) if self.dock_caret else None
            return "\n".join(body + dock)
        return "\n".join(self.compose(width, height))
