"""Command-line entry point: parse arguments, wire a session, hand off.

Everything this module used to do inline now lives behind a seam:

* slash commands   -> :mod:`fun.commands`
* user interaction -> :mod:`fun.frontends`
* drawing          -> :mod:`fun.ui`

What is left here is argument parsing, first-run configuration, and choosing
which frontend a given invocation should get.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .commands import REGISTRY, Session, command_names, dispatch, resolve_command_prefix
from .config import FunConfig
from .dashboard import serve
from .frontends import AppFrontend, PlainFrontend, friendly_error, run_goal
from .i18n import t
from .provider import ModelConfig, OpenAICompatible
from .runtime import SMALL_TALK_PLAN, Runtime
from .ui import input as keys
from .ui.app import App
from .ui.completion import FileIndex
from .ui.components import banner
from .ui.fullscreen import FullscreenSurface
from .ui.stream import StreamSurface
from .ui.theme import Theme

__all__ = ["build_parser", "main", "resolve_command_prefix"]


def _approval_allowed(answer: str) -> bool:
    """Translate the TUI's semantic approval answer without truthiness bugs."""
    return answer in {"yes", "always"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fun", description="A safety-first terminal coding agent runtime.")
    parser.add_argument("goal", nargs="?", help="A one-shot task goal")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--approval", choices=("ask", "smart", "auto"), default=None)
    parser.add_argument("--locale", choices=("zh-CN", "en-US"), default=os.getenv("FUN_LOCALE"), help="UI language")
    parser.add_argument("--version", action="version", version=f"fun {__version__}")
    parser.add_argument("--base-url", default=os.getenv("FUN_API_URL"))
    parser.add_argument("--api-key", default=os.getenv("FUN_API_KEY"))
    parser.add_argument("--model", default=os.getenv("FUN_MODEL"))
    parser.add_argument("--non-interactive", action="store_true", help="Never wait for interactive approval")
    parser.add_argument("--resume-session", help="Resume a persisted session by ID")
    parser.add_argument("--configure", action="store_true", help="Save provider settings interactively")
    parser.add_argument("--fullscreen", action="store_true", help="Take over the terminal (default)")
    parser.add_argument("--stream", action="store_true", help="Keep output in the shell's scrollback instead of taking over the terminal")
    parser.add_argument("--no-color", action="store_true", help="Disable colour output")
    parser.add_argument("--theme", choices=("sky", "dawn", "ember", "mono"), default=None, help="Colour theme")
    parser.add_argument("--dashboard", action="store_true", help="Open the local-only usage dashboard")
    parser.add_argument("--dashboard-port", type=int, default=8765)
    parser.add_argument("--telemetry", dest="telemetry", action="store_true", help="Enable configured private telemetry")
    parser.add_argument("--no-telemetry", dest="telemetry", action="store_false", help="Disable telemetry and remove local anonymous ID")
    parser.set_defaults(telemetry=None)
    return parser


def _theme(args: argparse.Namespace, name: str = "sky", locale: str = "en-US") -> Theme:
    chosen = args.theme or name
    if args.no_color:
        return Theme(mode="none", unicode=Theme.detect().unicode, name=chosen, locale=locale)
    detected = Theme.detect(is_tty=sys.stdout.isatty(), name=chosen)
    return Theme(detected.mode, detected.unicode, detected.name, locale)


def _choose_locale(saved: FunConfig, args: argparse.Namespace, config_path: str) -> str | None:
    if args.locale:
        return args.locale
    if Path(config_path).exists() or not sys.stdin.isatty():
        return saved.locale
    print("Select language / 选择语言")
    print("  [1] 中文")
    print("  [2] English")
    try:
        locale = "zh-CN" if input("❯ ").strip() == "1" else "en-US"
    except (EOFError, KeyboardInterrupt):
        return None
    saved.locale = locale
    saved.save(config_path)
    return locale


def _configure(saved: FunConfig, config_path: str, locale: str, theme: Theme) -> int:
    """The ``--configure`` flow: provider, key, model and telemetry consent."""
    if not sys.stdin.isatty():
        print("Configuration requires an interactive terminal.", file=sys.stderr)
        return 2
    frontend = PlainFrontend(locale, theme)
    saved.base_url = input(f"{t(locale, 'base_url')} [{saved.base_url}]: ").strip() or saved.base_url
    env_key = os.getenv("FUN_API_KEY", "")
    if env_key:
        print("Using FUN_API_KEY from environment; no key input needed.")
        saved.api_key = env_key
        saved.from_env = True
    else:
        print("API key: paste is supported; input is hidden and will not echo.")
        entered = frontend._ask(t(locale, "api_key_keep") + ": ", secret=True)
        if entered is None:
            return 130
        if entered:
            saved.api_key = entered
            saved.from_env = False
    picked: list[str | None] = [None]
    provider = OpenAICompatible(ModelConfig(saved.base_url, saved.api_key, saved.model or "models-placeholder")) if saved.base_url and saved.api_key else None
    frontend.select("Choose model", [saved.model] if saved.model else [], lambda value: picked.__setitem__(0, value), loader=provider.list_models if provider else None)
    saved.model = picked[0] or saved.model
    if not saved.model:
        print(t(locale, "model_required_cli"), file=sys.stderr)
        return 2
    choice = input(f"{t(locale, 'telemetry_prompt')} [{'Y/n' if saved.telemetry else 'y/N'}]: ").strip().lower()
    if choice in {"y", "yes"}:
        saved.telemetry = True
        saved.telemetry_endpoint = input(f"{t(locale, 'telemetry_endpoint')} [{saved.telemetry_endpoint}]: ").strip() or saved.telemetry_endpoint
    elif choice in {"n", "no"}:
        saved.telemetry, saved.telemetry_endpoint = False, ""
    saved.save(config_path)
    print(t(locale, "saved_to").format(path=config_path))
    return 0


def _telemetry_client(args: argparse.Namespace, saved: FunConfig, state_dir: str, config_path: str) -> Any:
    enabled = saved.telemetry if args.telemetry is None else args.telemetry
    if args.telemetry is True:
        from .telemetry import valid_endpoint

        if not valid_endpoint(saved.telemetry_endpoint):
            print("Telemetry requires a private http(s) endpoint. Use --configure first.", file=sys.stderr)
            saved.telemetry, enabled = False, False
        else:
            saved.telemetry = True
    if args.telemetry is False:
        saved.telemetry, saved.telemetry_endpoint = False, ""
        try:
            os.remove(os.path.join(state_dir, "telemetry_id"))
        except FileNotFoundError:
            pass
        saved.save(config_path)
    if not (enabled and saved.telemetry_endpoint):
        return None
    from .telemetry import TelemetryClient, load_or_create_install_id

    return TelemetryClient(enabled=True, endpoint=saved.telemetry_endpoint, install=load_or_create_install_id(state_dir))


def _run_interactive_app(session: Session, app: App, locale: str, theme: Theme) -> int:
    """The full terminal experience: streaming by default, fullscreen on request."""
    frontend = AppFrontend(app, locale)
    runtime = session.runtime
    app.completer.commands = {name: command.summary for name, command in sorted(REGISTRY.items())}
    app.completer.files = FileIndex(runtime.tools.guard.root)
    app.state.agent_mode = runtime.policy.agent_mode

    def set_mode(name: str) -> None:
        runtime.policy.agent_mode = name

    app.mode_handler = set_mode
    app.state.model_name = runtime.model
    app.state.approval_mode = runtime.policy.mode.value
    app.state.workspace = str(runtime.tools.guard.root)
    app.state.version = f"v{__version__}"
    app.state.session_label = runtime.session_id
    app.state.task_state = runtime.task.status if runtime.task else "idle"
    if runtime.task:
        app.state.restore_messages(runtime.task.messages)
        app.state.set_plan(runtime.task.plan, runtime.task.plan_status)
    if runtime.task and runtime.task.status == "recovery_required":
        app.state.set_recovery(runtime.recovery_summary() or {})
    app.background_provider = lambda: [
        # The rail truncates for its own column; the answer must arrive whole,
        # because the transcript report is built from this same dict and 120
        # characters cut a six-sentence answer mid-word with no ellipsis.
        {"id": item.id, "status": item.status, "goal": item.goal, "result": str(item.result) if item.result is not None else "", "error": item.error or ""}
        for item in runtime.background.list()
    ]
    app.recovery_handler = lambda action: submit(f"/recover {action}")

    def interrupt() -> bool:
        """Stop an in-flight task so Ctrl-C interrupts work before it exits.

        ``run_model_turn`` re-checks task status between stream chunks and
        between tool calls, so flipping the task out of ``running`` unwinds the
        worker thread at the next safe point rather than killing it mid-write.
        """
        task = runtime.task
        if task is None or task.status not in {"running", "paused"}:
            return False
        try:
            runtime.stop()
        except RuntimeError:
            return False
        return True

    app.interrupt_handler = interrupt

    def turn_footer(elapsed: float) -> str:
        """The one-line receipt under a finished reply: mode, model, cost, time.

        Only facts that were actually measured appear; a missing token count is
        omitted rather than printed as a zero.
        """
        parts = [app.state.agent_mode, runtime.model or "model"]
        tokens = runtime.usage.output_tokens
        if isinstance(tokens, int) and tokens > 0:
            parts.append(f"{tokens} tok")
        parts.append(f"{elapsed:.1f}s")
        return "  ·  ".join(parts)

    def on_plan(steps: list[str], statuses: list[str]) -> None:
        trivial = tuple(steps) == SMALL_TALK_PLAN
        app.post("plan", ([], []) if trivial else (steps, statuses))

    runtime.on_plan = on_plan

    def on_status(kind: str, payload: dict[str, Any]) -> None:
        app.post("tool", (kind, payload))

    def worker(text: str) -> None:
        started = time.monotonic()
        run_goal(session, frontend, text, on_text=lambda chunk: app.post("assistant", chunk), on_status=on_status)
        app.post("turn", turn_footer(time.monotonic() - started))
        task = runtime.task
        if task:
            # "understand the request / respond" as a 0/2 progress bar tells a
            # reader nothing except that something is unfinished, which is
            # exactly the wrong thing to say after a greeting.
            trivial = tuple(task.plan) == SMALL_TALK_PLAN
            app.post("plan", ([], []) if trivial else (task.plan, task.plan_status))
        app.post("usage", runtime.usage.summary())
        app.state.model_name = runtime.model
        app.state.approval_mode = runtime.policy.mode.value

    def submit(text: str) -> None:
        if dispatch(text, session, frontend):
            # No command names here.  Matching the raw text meant "/cle" — which
            # dispatch resolves to /clear and reports as cleared — silently left
            # the transcript in place; the side effect belongs to the handler.
            app.state.model_name = runtime.model
            app.state.approval_mode = runtime.policy.mode.value
            return
        app.state.goal = text
        threading.Thread(target=worker, args=(text,), daemon=True).start()

    try:
        app.run(submit)
    finally:
        runtime.shutdown()
    return 0


def _run_plain(session: Session, locale: str, theme: Theme) -> int:
    """Fallback loop for pipes, dumb terminals and Windows consoles."""
    frontend = PlainFrontend(locale, theme)
    runtime = session.runtime
    for line in banner(theme, 72, f"v{__version__}"):
        print(line)
    print()
    if runtime.provider is None:
        frontend.say(t(locale, "offline"))
    try:
        while not frontend.stopped:
            try:
                text = input("fun ❯ ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if dispatch(text, session, frontend):
                continue
            run_goal(session, frontend, text, on_text=lambda chunk: print(chunk, end="", flush=True))
            print()
    finally:
        runtime.shutdown()
    return 0


def _prepare_paths(workspace: str, state_dir: str) -> str:
    """Validate the workspace and state directory before anything uses them.

    Both used to reach their consumers unchecked, so a mistyped ``--workspace``
    surfaced as a raw ``PolicyError`` traceback and a ``FUN_STATE_DIR`` pointing
    at a file as ``FileExistsError`` — a Python stack trace for a typo.
    """
    target = Path(workspace).expanduser()
    if not target.exists():
        return f"× workspace does not exist: {target}"
    if not target.is_dir():
        return f"× workspace is not a directory: {target}"
    if not os.access(target, os.R_OK | os.W_OK):
        return f"× workspace is not readable and writable: {target}"
    state = Path(state_dir).expanduser()
    if state.exists() and not state.is_dir():
        return f"× state directory is not a directory: {state}"
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"× cannot create state directory {state}: {exc}"
    return ""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = os.getenv("FUN_STATE_DIR", str(os.path.expanduser("~/.fun")))
    problem = _prepare_paths(args.workspace, state_dir)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if args.dashboard:
        serve(os.path.join(state_dir, "events.db"), args.dashboard_port)
        return 0
    config_path = os.path.join(state_dir, "config.json")
    saved = FunConfig.load(config_path)
    locale = _choose_locale(saved, args, config_path)
    theme = _theme(args, saved.theme, locale or saved.locale or "en-US")
    if locale is None:
        print(t("en-US", "cancel_status"), file=sys.stderr)
        return 130
    if args.configure:
        return _configure(saved, config_path, locale, theme)
    if locale != saved.locale:
        saved.locale = locale
        saved.save(config_path)

    base_url = args.base_url or saved.base_url
    api_key = args.api_key or saved.api_key
    model = args.model or saved.model
    approval = args.approval or saved.approval
    provider = OpenAICompatible(ModelConfig(base_url, api_key, model)) if base_url and api_key and model else None
    telemetry = _telemetry_client(args, saved, state_dir, config_path)

    session_approvals: set[str] = set()
    app_holder: dict[str, App] = {}

    def approve(name: str, risk: object) -> bool:
        # "Always allow" never covers a critical operation.  Approving
        # `rm -rf build` once used to remember `exec:rm` for the session, so the
        # next `rm -rf` — of anything — ran with no prompt at all.
        critical = str(getattr(risk, "value", risk)) == "critical"
        if not critical and name in session_approvals:
            return True
        if args.non_interactive or not sys.stdin.isatty():
            return False
        app = app_holder.get("app")
        if app is not None:
            answer = app.request_approval(name, risk)
            if answer == "always":
                # The UI offers "always allow in this session"; only the plain
                # input() fallback ever recorded it, so in the real frontend the
                # choice allowed one call and then asked again immediately.
                if not critical:
                    session_approvals.add(name)
                return True
            # request_approval returns semantic strings.  In particular,
            # ``"no"`` is non-empty and therefore truthy, so bool(answer)
            # silently turned an explicit rejection into permission.
            return _approval_allowed(answer)
        try:
            choice = input("? " + t(locale, "approval_prompt").format(name=name, risk=risk)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if choice in {"a", "always"}:
            if not critical:
                session_approvals.add(name)
            return True
        return choice in {"y", "yes"}

    if args.resume_session:
        try:
            runtime = Runtime.recover(
                args.workspace, state_dir, args.resume_session, approval=approval, provider=provider,
                approve=approve, telemetry=telemetry, model=model, system_prompt=saved.system_prompt,
            )
        except RuntimeError as exc:
            if str(exc).startswith("UNKNOWN_SESSION"):
                print(f"× no such session in {state_dir}: {args.resume_session}", file=sys.stderr)
                return 2
            print(f"× could not resume session: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"× could not resume session: {exc}", file=sys.stderr)
            return 2
    else:
        runtime = Runtime(args.workspace, approval, provider, state_dir=state_dir, approve=approve, telemetry=telemetry, model=model, system_prompt=saved.system_prompt)

    session = Session(runtime, saved, config_path, base_url, api_key, model)

    if args.goal:
        if provider is None:
            print("Model not configured. Set --base-url, --api-key, and --model to run the agent loop.", file=sys.stderr)
            runtime.shutdown()
            return 2
        frontend = PlainFrontend(locale, theme)
        run_goal(session, frontend, args.goal, on_text=lambda chunk: print(chunk, end="", flush=True))
        print()
        failed = runtime.task is not None and runtime.task.status not in {"completed"}
        runtime.shutdown()
        return 1 if failed else 0

    # A missing provider is not a reason to withhold the interface.  Offline you
    # can still browse the session, run commands and configure a provider from
    # inside the app; only submitting a goal needs a model, and `run_goal` says
    # so plainly when one is absent.
    interactive = keys.supports_raw_mode() and sys.stdout.isatty()
    if not interactive:
        return _run_plain(session, locale, theme)

    # Fullscreen is the default: the session reads as an application rather than
    # as a command that scrolled past.  `--stream` opts back into scrollback for
    # anyone who wants to select and copy history with the mouse, or pipe it.
    surface = StreamSurface() if args.stream and not args.fullscreen else FullscreenSurface(theme=theme)
    app = App(surface, theme=theme, locale=locale, commands=command_names())
    app_holder["app"] = app
    return _run_interactive_app(session, app, locale, theme)


if __name__ == "__main__":
    raise SystemExit(main())
