"""One command registry shared by every frontend.

Slash commands used to be implemented twice: once inside the TUI's submit
handler and once inside the plain REPL loop.  The two drifted — ``/logout``,
``/diff``, ``/checkpoint`` and ``/goal`` only ever existed in the REPL — and any
new command had to be written, and kept correct, in both places.

Here a command is registered once against a :class:`Frontend` protocol.  The
frontend decides *how* to talk to the user (modal overlay versus ``input()``),
the command decides *what* to ask.  Adding a command means adding one function.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .config import FunConfig
from .i18n import key_location_message, saved_message, t
from .provider import ModelConfig, OpenAICompatible
from .runtime import Runtime, build_system_prompt


class Frontend(Protocol):
    """How a command talks to the user, independent of the rendering style."""

    locale: str

    def say(self, text: str) -> None:
        """Show a block of text."""

    def notify(self, text: str) -> None:
        """Show a transient confirmation."""

    def status(self, text: str) -> None:
        """Update the status indicator."""

    def clear(self) -> None:
        """Drop the visible transcript and per-task view state."""

    def form(self, title: str, fields: Sequence[Any], callback: Callable[[dict[str, str] | None], None]) -> None:
        """Collect several values, masking any field marked secret."""

    def select(self, title: str, options: Sequence[str], callback: Callable[[Any], None], loader: Callable[[], list[str]] | None = None, multi: bool = False, chosen: Sequence[str] = ()) -> None:
        """Let the user pick, optionally several, from a list loaded in the background."""

    def edit(self, title: str, initial: str, callback: Callable[[str | None], None]) -> None:
        """Edit a multi-line value."""

    def quit(self) -> None:
        """Leave the session."""


@dataclass
class Session:
    """Mutable wiring a command may read or replace."""

    runtime: Runtime
    config: FunConfig
    config_path: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    models: list[str] = field(default_factory=list)

    @property
    def provider(self) -> OpenAICompatible | None:
        return self.runtime.provider

    def apply_provider(self) -> bool:
        """Rebuild the provider from the current credentials and persist them.

        Returns whether the key reached durable storage, so the caller can tell
        the user the truth rather than a fixed reassurance.
        """
        self.config.base_url, self.config.model = self.base_url, self.model
        self.config.models = list(self.models)
        if self.api_key:
            self.config.api_key = self.api_key
            self.config.keychain_unreadable = False
        written, durable = self.config.save(self.config_path)
        if self.base_url and self.api_key and self.model:
            self.runtime.provider = OpenAICompatible(ModelConfig(self.base_url, self.api_key, self.model))
            self.runtime.model = self.model
        return durable or not written

    def key_location(self) -> str:
        """Where the key ended up on disk: keychain, config-file, environment, none."""
        return self.config.storage(self.config_path)


@dataclass
class CommandContext:
    session: Session
    frontend: Frontend
    argument: str = ""

    @property
    def runtime(self) -> Runtime:
        return self.session.runtime

    @property
    def locale(self) -> str:
        return self.frontend.locale

    def say(self, text: str) -> None:
        self.frontend.say(text)

    def notify(self, text: str) -> None:
        self.frontend.notify(text)


Handler = Callable[[CommandContext], None]


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    handler: Handler
    takes_argument: bool = False
    group: str = "other"
    key: str = ""

    def describe(self, locale: str) -> str:
        """The localised one-line summary, falling back to the English literal.

        The ``cmd_*`` keys existed in both locale tables and were referenced
        from nowhere, so the palette and /help printed English at every locale.
        """
        key = f"cmd_{self.name.lstrip('/')}"
        translated = t(locale, key)
        return self.summary if translated == key else translated


# Aliases stay dispatchable but never appear twice in the palette.
ALIAS = "alias"

# Order matters: the palette lists groups in this sequence, most-reached first.
# Identifiers, not display text: the palette translates them at draw time.
GROUP_ORDER = ("session", "task", "provider", "system")

REGISTRY: dict[str, Command] = {}


def register(name: str, summary: str, takes_argument: bool = False, group: str = "other", key: str = "") -> Callable[[Handler], Handler]:
    def decorate(handler: Handler) -> Handler:
        REGISTRY[name] = Command(name, summary, handler, takes_argument, group, key)
        return handler

    return decorate


def grouped_commands() -> list[tuple[str, list[Command]]]:
    """Commands bucketed for the palette, in a deliberate reading order."""
    buckets: dict[str, list[Command]] = {}
    for command in REGISTRY.values():
        if command.group == ALIAS:
            continue
        buckets.setdefault(command.group, []).append(command)
    ordered = [name for name in GROUP_ORDER if name in buckets]
    ordered += sorted(name for name in buckets if name not in GROUP_ORDER)
    return [(name, sorted(buckets[name], key=lambda item: item.name)) for name in ordered]


def command_names() -> list[str]:
    return sorted(REGISTRY)


def resolve_command_prefix(text: str, commands: set[str]) -> tuple[str | None, list[str]]:
    """Resolve an exact or unambiguous command prefix without asking the model."""
    if not text.startswith("/") or text in commands:
        return text, []
    matches = sorted(command for command in commands if command.startswith(text))
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def dispatch(text: str, session: Session, frontend: Frontend) -> bool:
    """Run ``text`` as a command.  Returns False when it is not a command."""
    if not text.startswith("/"):
        return False
    head, _, argument = text.partition(" ")
    resolved, matches = resolve_command_prefix(head, set(REGISTRY))
    if resolved is None:
        frontend.say("\n".join(matches) if matches else t(frontend.locale, "unknown_command"))
        return True
    command = REGISTRY.get(resolved)
    if command is None:
        frontend.say(t(frontend.locale, "unknown_command"))
        return True
    command.handler(CommandContext(session, frontend, argument.strip()))
    return True


# --------------------------------------------------------------------- session


@register("/help", "show the command reference", group="system")
def _help(ctx: CommandContext) -> None:
    width = max(len(name) for name in REGISTRY)
    lines = [f"  {name:<{width}}  {command.describe(ctx.locale)}" for name, command in sorted(REGISTRY.items())]
    ctx.say("\n".join(lines))


@register("/exit", "leave the session", group="system", key="ctrl+d")
def _exit(ctx: CommandContext) -> None:
    ctx.frontend.quit()


REGISTRY["/quit"] = Command("/quit", "leave the session", _exit, group=ALIAS)  # summary comes from cmd_quit


@register("/clear", "clear the transcript", group="session")
def _clear(ctx: CommandContext) -> None:
    ctx.frontend.clear()
    ctx.frontend.status("cleared")


# -------------------------------------------------------------------- provider


def choose_models(ctx: CommandContext, then: Callable[[], None] | None = None) -> None:
    """Open the model picker against the live provider.

    The model used to be a third text field in the credentials form: you had to
    know the exact id, type it by hand, and could name only one.  The provider
    can list them, so it lists them — filterable, and multi-select so a second
    model stays one ``/model`` away instead of another round of typing.
    """
    session = ctx.session
    provider = session.provider or _probe_provider(session)

    def picked(value: Any) -> None:
        names = [str(item) for item in value] if isinstance(value, list) else ([str(value)] if value else [])
        if names:
            session.models = names
            session.model = names[0]
            session.apply_provider()
            ctx.frontend.status(f"model={session.model}" + (f" (+{len(names) - 1})" if len(names) > 1 else ""))
        if then is not None:
            then()

    ctx.frontend.select(
        t(ctx.locale, "choose_model"),
        session.models or ([session.model] if session.model else []),
        picked,
        loader=provider.list_models if provider else None,
        multi=True,
        chosen=list(session.models),
    )


def _probe_provider(session: Session) -> OpenAICompatible | None:
    """A provider good enough to *list* models, before one has been chosen."""
    if not (session.base_url and session.api_key):
        return None
    return OpenAICompatible(ModelConfig(session.base_url, session.api_key, session.model or "models-placeholder"))


@register("/config", "configure the provider, key and model", group="provider")
def _config(ctx: CommandContext) -> None:
    session = ctx.session

    def apply(values: dict[str, str] | None) -> None:
        if not values:
            ctx.frontend.status("configuration cancelled")
            return
        session.base_url = values.get("base_url", "").strip() or session.base_url
        session.api_key = values.get("api_key", "").strip() or session.api_key
        session.apply_provider()
        # Say what actually happened.  "Stored securely" was printed on every
        # machine without a keychain too, where the key lived only in this
        # process and the next launch started with no credentials at all.
        ctx.notify(saved_message(ctx.locale, session.key_location(), session.config_path))
        # The endpoint and key are what the model list needs, so ask for the
        # model straight after them rather than making the user run /model.
        choose_models(ctx)

    ctx.frontend.form("Provider configuration", ["base_url", ("api_key", True)], apply)


REGISTRY["/setup"] = Command("/setup", "configure the provider, key and model", _config, group=ALIAS)


@register("/model", "switch model", takes_argument=True, group="provider")
def _model(ctx: CommandContext) -> None:
    session = ctx.session
    if ctx.argument:
        session.model = ctx.argument
        if ctx.argument not in session.models:
            session.models = [ctx.argument] + session.models
        session.apply_provider()
        ctx.frontend.status(f"model={session.model}")
        return
    if session.provider is None and _probe_provider(session) is None:
        ctx.say(t(ctx.locale, "no_provider"))
        return
    choose_models(ctx)


@register("/permissions", "change the approval mode", takes_argument=True, group="provider")
def _permissions(ctx: CommandContext) -> None:
    modes = ["ask", "smart", "auto"]
    session = ctx.session
    if ctx.argument in modes:
        selected = ctx.argument
        _set_mode(ctx, selected)
        return

    def chosen(value: str | None) -> None:
        if value in modes:
            _set_mode(ctx, value)

    ctx.frontend.select("Approval mode", modes, chosen)


def _set_mode(ctx: CommandContext, mode: str) -> None:
    session = ctx.session
    try:
        resolved = session.runtime.policy.set_mode(mode)
    except ValueError:
        ctx.say(t(ctx.locale, "unknown_command"))
        return
    session.config.approval = resolved.value
    session.config.save(session.config_path)
    ctx.frontend.status(f"approval={resolved.value}")


@register("/mode", "switch agent mode: Build, Plan or Review", takes_argument=True, group="session", key="tab")
def _mode(ctx: CommandContext) -> None:
    from .policy import AGENT_MODES

    session = ctx.session
    if ctx.argument:
        selected = ctx.argument.strip().capitalize()
        if selected not in AGENT_MODES:
            ctx.say(f"× unknown mode: {ctx.argument} (expected {', '.join(AGENT_MODES)})")
            return
        session.runtime.policy.agent_mode = selected
        ctx.frontend.status(f"mode={selected}")
        return

    def chosen(value: str | None) -> None:
        if value in AGENT_MODES:
            session.runtime.policy.agent_mode = value
            ctx.frontend.status(f"mode={value}")

    ctx.frontend.select("Agent mode", list(AGENT_MODES), chosen)


@register("/theme", "switch the colour theme", takes_argument=True, group="system")
def _theme(ctx: CommandContext) -> None:
    from .ui.theme import theme_names

    session = ctx.session
    names = theme_names()

    def apply(value: str | None) -> None:
        if value not in names:
            return
        session.config.theme = value
        session.config.save(session.config_path)
        applied = getattr(ctx.frontend, "apply_theme", None)
        if callable(applied):
            applied(value)
        ctx.frontend.status(f"theme={value}")

    if ctx.argument:
        if ctx.argument.strip() not in names:
            ctx.say(f"× unknown theme: {ctx.argument} (available: {', '.join(names)})")
            return
        apply(ctx.argument.strip())
        return
    ctx.frontend.select("Theme", names, apply)


@register("/logout", "delete stored credentials and go offline", group="provider")
def _logout(ctx: CommandContext) -> None:
    session = ctx.session
    deleted = session.config.clear_credentials(session.config_path)
    session.base_url = session.api_key = session.model = ""
    session.runtime.provider = None
    session.runtime.model = ""
    ctx.notify(t(ctx.locale, "removed" if deleted else "remove_failed"))
    ctx.say(t(ctx.locale, "offline"))


@register("/prompt", "view or set custom system prompt preferences", takes_argument=True, group="system")
def _prompt(ctx: CommandContext) -> None:
    session = ctx.session

    def apply(value: str | None) -> None:
        if value is None:
            ctx.frontend.status("prompt edit cancelled")
            return
        preference = value.strip()[:12000]
        runtime = session.runtime
        runtime.system_prompt = build_system_prompt(preference)
        session.config.system_prompt = preference
        session.config.save(session.config_path)
        task = runtime.task
        if task and task.messages and task.messages[0].get("role") == "system":
            task.messages[0]["content"] = runtime.system_prompt
            if not getattr(runtime, "_closed", False):
                runtime.emit("task.message", task.id, message={"role": "system", "content": runtime.system_prompt})
        ctx.notify("System prompt updated")

    if ctx.argument:
        apply(ctx.argument)
        return
    ctx.frontend.edit("System prompt preferences", session.config.system_prompt.strip(), apply)


# ------------------------------------------------------------------------ task


@register("/goal", "show or set the current goal", takes_argument=True, group="task")
def _goal(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    if not ctx.argument:
        ctx.say(runtime.goal() or "(no active goal)")
        return
    try:
        runtime.set_goal(ctx.argument)
        ctx.frontend.status("working")
    except Exception as exc:  # surfaced, not swallowed
        ctx.say(f"× {exc}")


@register("/plan", "show the current plan", group="task")
def _plan(ctx: CommandContext) -> None:
    task = ctx.runtime.task
    if not task or not task.plan:
        ctx.say("(no plan yet)")
        return
    markers = {"done": "✓", "active": "●", "blocked": "×", "pending": "○"}
    rows = []
    for index, step in enumerate(task.plan):
        status = task.plan_status[index] if index < len(task.plan_status) else "pending"
        rows.append(f"  {markers.get(status, '○')} {step}")
    ctx.say("\n".join(rows))


@register("/status", "show task, agent, usage and recovery state", group="task")
def _status(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    task = runtime.task
    lines = [f"session={runtime.session_id} task={task.status if task else 'idle'} agent={task.agent_state if task else 'idle'} policy={runtime.policy.mode.value}"]
    if runtime.last_model_timing:
        timing = runtime.last_model_timing
        lines.append(f"model timing: ttft={timing.get('ttft_ms', '?')}ms · step={timing.get('step_ms', '?')}ms")
    if task and task.recovery_reason:
        pending = runtime.recovery_summary() or {}
        lines.append("! " + t(ctx.locale, "pending_tool").format(name=pending.get("name", "unknown tool"), call_id=pending.get("call_id", "?")))
        lines.append(f"  args: {pending.get('arguments', {})}")
        lines.append(t(ctx.locale, "recovery_actions"))
    if task and task.plan_error:
        lines.append(f"! plan rejected: {task.plan_error}")
        if task.plan_error_summary:
            lines.append(f"  proposal: {task.plan_error_summary}")
    if task and task.failure_reason:
        lines.append("! " + t(ctx.locale, "task_failed").format(reason=task.failure_reason[:240]))
    if task and task.result is not None:
        lines.append(f"result: {task.result[:240]}")
    for item in runtime.background.list():
        detail = (str(item.result)[:120] if item.result is not None else "") or (item.error or "")
        lines.append(f"  {item.id} {item.status} · {item.goal[:80]}" + (f" · {detail}" if detail else ""))
    lines.append(key_location_message(ctx.locale, ctx.session.key_location(), ctx.session.config_path))
    lines.append(runtime.usage.summary())
    ctx.say("\n".join(lines))


@register("/usage", "show token usage and latency", group="task")
def _usage(ctx: CommandContext) -> None:
    ctx.say(ctx.runtime.usage.summary())


@register("/diff", "show the working tree diff", group="task")
def _diff(ctx: CommandContext) -> None:
    try:
        snapshot = ctx.runtime.checkpoint("view")
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.say(str(snapshot.get("diff") or "(no working tree diff)"))


@register("/checkpoint", "create a workspace checkpoint", group="task")
def _checkpoint(ctx: CommandContext) -> None:
    try:
        ctx.runtime.checkpoint()
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.notify("checkpoint created")


def _lifecycle_error(ctx: CommandContext, expected: set[str]) -> bool:
    task = ctx.runtime.task
    if task is None:
        ctx.say("× NO_ACTIVE_TASK")
        return True
    if task.status not in expected:
        ctx.say(f"× INVALID_TASK_TRANSITION: {task.status}")
        return True
    return False


@register("/pause", "pause the running task", group="task")
def _pause(ctx: CommandContext) -> None:
    if _lifecycle_error(ctx, {"running"}):
        return
    try:
        ctx.runtime.pause()
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.frontend.status("paused")


@register("/resume", "resume a paused task", group="task")
def _resume(ctx: CommandContext) -> None:
    if _lifecycle_error(ctx, {"paused"}):
        return
    try:
        ctx.runtime.resume()
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.frontend.status("ready")


@register("/stop", "stop the current task", group="task")
def _stop(ctx: CommandContext) -> None:
    if _lifecycle_error(ctx, {"running", "paused", "recovery_required"}):
        return
    try:
        ctx.runtime.stop()
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.frontend.status("stopped")


@register("/recover", "acknowledge a pending recovery", takes_argument=True, group="task")
def _recover(ctx: CommandContext) -> None:
    action = ctx.argument or "resume"
    try:
        ctx.runtime.acknowledge_recovery(action)
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.frontend.status("working" if action in {"resume", "discard", "mark_failed"} else "stopped")


@register("/agent", "ask a read-only sub-agent a question in the background", takes_argument=True, group="task")
def _agent(ctx: CommandContext) -> None:
    if not ctx.argument:
        ctx.say(t(ctx.locale, "agent_usage"))
        return
    try:
        task = ctx.runtime.spawn_research(ctx.argument)
    except ValueError:
        ctx.say(t(ctx.locale, "agent_usage"))
        return
    except RuntimeError as exc:
        # Report what actually went wrong.  Collapsing every RuntimeError into
        # "configure a provider" told correctly-configured users their provider
        # was missing whenever the store happened to be closed.
        ctx.say(t(ctx.locale, "no_provider") if str(exc) == "PROVIDER_NOT_CONFIGURED" else f"× {exc}")
        return
    ctx.notify(t(ctx.locale, "agent_started").format(id=task.id))


@register("/cancel", "cancel a background task by id", takes_argument=True, group="task")
def _cancel(ctx: CommandContext) -> None:
    if not ctx.argument:
        ctx.say(t(ctx.locale, "cancel_usage"))
        return
    try:
        ctx.runtime.cancel_background_task(ctx.argument)
    except RuntimeError as exc:
        ctx.say(f"× {exc}")
        return
    ctx.notify(f"cancellation requested: {ctx.argument}")
