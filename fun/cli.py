from __future__ import annotations

import argparse
import os
from pathlib import Path
import getpass
import json
import shlex
import sys
import threading
try:
    import termios
    import tty
except ImportError:  # Windows: menu falls back to typed commands
    termios = None
    tty = None
try:
    import readline
except ImportError:
    readline = None

from . import __version__
from .config import FunConfig
from .dashboard import serve
from .provider import ModelConfig, OpenAICompatible, ProviderError
from .i18n import t
from .renderer import TerminalRenderer
from .runtime import Runtime
from .tui import TerminalUI


def _friendly_error(exc: Exception, locale: str) -> str:
    tag = getattr(exc, "error_tag", "")
    keys = {"PROVIDER_AUTH_FAILED": "provider_auth", "PROVIDER_NETWORK_FAILED": "provider_network", "PROVIDER_TIMEOUT": "provider_timeout", "PROVIDER_MALFORMED_EVENT": "provider_bad_response", "PROVIDER_UNEXPECTED_CONTENT_TYPE": "provider_bad_response"}
    return t(locale, keys[tag]) if tag in keys else str(exc)


def _choose_model(base_url: str, api_key: str, current: str = "", locale: str = "en-US") -> str | None:
    try:
        models = OpenAICompatible(ModelConfig(base_url, api_key, current or "models-placeholder")).list_models()
    except Exception as exc:
        print(t(locale, "model_load_failed"))
        try:
            return input(f"Model ID [{current}] (manual fallback, Enter cancels) ❯ ").strip() or current or None
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    if not models:
        print(t(locale, "model_empty"))
        try:
            return input(f"Model ID [{current}] (manual fallback, Enter cancels) ❯ ").strip() or current or None
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    print(t(locale, "choose_model"))
    if termios is None or tty is None or not sys.stdin.isatty():
        for index, model_id in enumerate(models, 1):
            print(f"  [{index}] {model_id}")
        while True:
            choice = input(f"Choose model [1-{len(models)}] ❯ ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(models):
                return models[int(choice) - 1]
            print("Enter a model number, or use Ctrl-C to cancel.")
    index = 0
    while True:
        print("\033[2J\033[H", end="")
        print(t(locale, "choose_model") + "\n")
        for i, model_id in enumerate(models):
            print(f"{'❯' if i == index else ' '} {model_id}")
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            key = sys.stdin.read(1)
            if key in {"\n", "\r"}:
                return models[index]
            if key == "\x1b" and sys.stdin.read(1) == "[":
                code = sys.stdin.read(1)
                if code == "A": index = (index - 1) % len(models)
                elif code == "B": index = (index + 1) % len(models)
            elif key == "k": index = (index - 1) % len(models)
            elif key == "j": index = (index + 1) % len(models)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def resolve_command_prefix(text: str, commands: set[str]) -> tuple[str | None, list[str]]:
    """Resolve an exact or unique slash command without sending it to the model."""
    if not text.startswith("/") or text in commands:
        return text, []
    matches = sorted(command for command in commands if command.startswith(text))
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def _secret_input(prompt: str) -> str | None:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n" + t("en-US", "cancel_status"), file=sys.stderr)
        return None


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
    parser.add_argument("--dashboard", action="store_true", help="Open the local-only usage dashboard")
    parser.add_argument("--dashboard-port", type=int, default=8765)
    parser.add_argument("--telemetry", dest="telemetry", action="store_true", help="Enable configured private telemetry")
    parser.add_argument("--no-telemetry", dest="telemetry", action="store_false", help="Disable telemetry and remove local anonymous ID")
    parser.set_defaults(telemetry=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = os.getenv("FUN_STATE_DIR", str(os.path.expanduser("~/.fun")))
    if args.dashboard:
        serve(os.path.join(state_dir, "events.db"), args.dashboard_port)
        return 0
    config_path = os.path.join(state_dir, "config.json")
    saved = FunConfig.load(config_path)
    locale = args.locale or saved.locale
    if not args.locale and not Path(config_path).exists() and sys.stdin.isatty():
        print("Select language / 选择语言")
        print("  [1] 中文")
        print("  [2] English")
        try:
            locale = "zh-CN" if input("❯ ").strip() == "1" else "en-US"
        except (EOFError, KeyboardInterrupt):
            print(t("en-US", "cancel_status"), file=sys.stderr)
            return 130
        saved.locale = locale
        saved.save(config_path)
    renderer = TerminalRenderer(color=sys.stdout.isatty(), locale=locale)
    if args.configure:
        if not sys.stdin.isatty():
            print("Configuration requires an interactive terminal.", file=sys.stderr)
            return 2
        saved.base_url = input(f"{t(locale, 'base_url')} [{saved.base_url}]: ").strip() or saved.base_url
        env_key = os.getenv("FUN_API_KEY", "")
        if env_key:
            print("Using FUN_API_KEY from environment; no key input needed.")
            saved.api_key = env_key
        else:
            print("API key: paste is supported; input is hidden and will not echo.")
            entered_key = _secret_input(t(locale, "api_key_keep") + ": ")
            if entered_key is None:
                return 130
            saved.api_key = entered_key or saved.api_key
        saved.model = _choose_model(saved.base_url, saved.api_key, saved.model, locale) or saved.model
        if not saved.model:
            print(t(locale, "model_required_cli"), file=sys.stderr)
            return 2
        telemetry_choice = input(f"{t(locale, 'telemetry_prompt')} [{'Y/n' if saved.telemetry else 'y/N'}]: ").strip().lower()
        if telemetry_choice in {"y", "yes"}:
            saved.telemetry = True
            saved.telemetry_endpoint = input(f"{t(locale, 'telemetry_endpoint')} [{saved.telemetry_endpoint}]: ").strip() or saved.telemetry_endpoint
        elif telemetry_choice in {"n", "no"}:
            saved.telemetry = False
            saved.telemetry_endpoint = ""
        saved.save(config_path)
        print(t(locale, "saved_to").format(path=config_path))
        if saved.api_key:
            store = "macOS Keychain" if json.loads(Path(config_path).read_text(encoding="utf-8")).get("api_key_store") == "macos-keychain" else "FUN_API_KEY environment variable"
            print(f"API key stored via {store}; it is not written to config.json.")
        return 0
    base_url = args.base_url or saved.base_url
    api_key = args.api_key or saved.api_key
    model = args.model or saved.model
    approval = args.approval or saved.approval
    provider = None
    if base_url and api_key and model:
        provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
    session_approvals: set[str] = set()
    tui: TerminalUI | None = None
    def approve(name: str, risk: object) -> bool:
        if name in session_approvals:
            return True
        if args.non_interactive or not sys.stdin.isatty():
            return False
        if tui is not None:
            return tui.request_approval(name, risk)
        try:
            print(t(locale, "approval_wait"), flush=True)
            choice = input("? " + t(locale, "approval_prompt").format(name=name, risk=risk)).strip().lower()
            if choice in {"a", "always", "本会话"}:
                session_approvals.add(name)
                print(t(locale, "approval_session").format(name=name), flush=True)
                return True
            return choice in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False
    telemetry_enabled = saved.telemetry if args.telemetry is None else args.telemetry
    if args.telemetry is True:
        from .telemetry import valid_endpoint
        if not valid_endpoint(saved.telemetry_endpoint):
            print("Telemetry requires a private http(s) endpoint. Use --configure first.", file=sys.stderr)
            saved.telemetry = False
            telemetry_enabled = False
        else:
            saved.telemetry = True
    if args.telemetry is False:
        saved.telemetry = False
        saved.telemetry_endpoint = ""
        try:
            os.remove(os.path.join(state_dir, "telemetry_id"))
        except FileNotFoundError:
            pass
        saved.save(config_path)
    telemetry = None
    if telemetry_enabled and saved.telemetry_endpoint:
        from .telemetry import TelemetryClient, load_or_create_install_id
        telemetry = TelemetryClient(enabled=True, endpoint=saved.telemetry_endpoint, install=load_or_create_install_id(state_dir))
    if locale != saved.locale:
        saved.locale = locale
        saved.save(config_path)
    renderer = TerminalRenderer(color=sys.stdout.isatty(), locale=locale)
    if not provider and not args.goal and not args.resume_session and sys.stdin.isatty():
        print(renderer.welcome(False, os.path.abspath(args.workspace)))
        try:
            choice = input("\nSelect [1/2/3/4/q] ❯ ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(t(locale, "cancel_status"), file=sys.stderr)
            return 130
        if choice == "q":
            return 0
        if choice == "1":
            saved.base_url = "https://api.openai.com/v1"
        elif choice == "2":
            saved.base_url = input("Provider base URL ❯ ").strip()
        elif choice == "3":
            if not all(os.getenv(key) for key in ("FUN_API_URL", "FUN_API_KEY", "FUN_MODEL")):
                print(renderer.error("Missing FUN_API_URL, FUN_API_KEY, or FUN_MODEL."), file=sys.stderr)
                return 2
            saved.base_url, saved.api_key, saved.model = os.environ["FUN_API_URL"], os.environ["FUN_API_KEY"], os.environ["FUN_MODEL"]
        elif choice == "4":
            saved.base_url = ""
        else:
            print(renderer.error("Choose 1, 2, 3, 4, or q."), file=sys.stderr)
            return 2
        if choice in {"1", "2"}:
            print(t(locale, "api_key_hint"))
            entered_key = _secret_input("API key ❯ ") if not os.getenv("FUN_API_KEY") else os.getenv("FUN_API_KEY")
            if entered_key is None:
                return 130
            saved.api_key = entered_key
            if not saved.api_key:
                print(renderer.error("API key is required."), file=sys.stderr)
                return 2
            saved.model = _choose_model(saved.base_url, saved.api_key, saved.model, locale) or ""
            if not saved.model:
                return 2
            print("Permission mode: [1] ask  [2] smart (recommended)  [3] auto")
            approval = {"1": "ask", "2": "smart", "3": "auto"}.get(input("❯ ").strip(), approval)
            saved.approval = approval
            os.environ["FUN_API_KEY"] = saved.api_key
            saved.save(config_path)
            base_url, api_key, model = saved.base_url, saved.api_key, saved.model
            provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
            print(renderer.setup_complete())
        elif choice == "3":
            base_url, api_key, model = saved.base_url, saved.api_key, saved.model
            provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
    if args.resume_session:
        try:
            runtime = Runtime.recover(args.workspace, state_dir, args.resume_session, approval=approval, provider=provider, approve=approve)
        except Exception as exc:
            print(f"× could not resume session: {exc}", file=sys.stderr)
            return 2
    else:
        runtime = Runtime(args.workspace, approval, provider, state_dir=state_dir, approve=approve, telemetry=telemetry, model=model, system_prompt=saved.system_prompt)
    if args.goal:
        task = runtime.create_task(args.goal)
        print(f"Fun · {args.workspace}")
        print(renderer.plan(task.plan))
        if provider:
            try:
                output = runtime.run_model_turn(on_text=lambda text: print(text, end="", flush=True))
                runtime.complete(output)
                print()
            except Exception as exc:
                print(f"\n× {_friendly_error(exc, locale)}", file=__import__("sys").stderr)
                runtime.stop()
                return 1
        else:
            print("Model not configured. Set --base-url, --api-key, and --model to run the agent loop.", file=sys.stderr)
            runtime.stop()
            return 2
        runtime.stop()
        return 0

    print(renderer.header(str(runtime.tools.guard.root), provider is not None, runtime.policy.mode.value))
    if runtime.task and runtime.task.status == "recovery_required":
        pending = runtime.recovery_summary() or {}
        print("! " + t(locale, "pending_tool").format(name=pending.get("name", "unknown tool"), call_id=pending.get("call_id", "?")))
        print(t(locale, "recovery_actions"))
    if provider:
        print(renderer.welcome(True))
    else:
        print(renderer.finding(t(locale, "offline")))
        print("输入 /help 查看帮助，/setup 了解配置，或 /quit 退出。" if renderer.zh else "Use /help for commands, /setup to configure later, or /quit to exit.")

    if readline is not None:
        command_names = ["/help", "/config", "/setup", "/model", "/permissions", "/logout", "/status", "/plan", "/usage", "/diff", "/checkpoint", "/clear", "/goal", "/pause", "/resume", "/recover", "/cancel", "/stop", "/exit", "/quit"]
        def complete_command(text: str, state: int) -> str | None:
            line = readline.get_line_buffer() if readline is not None else text
            if line and not line.lstrip().startswith("/"):
                return None
            prefix = line[:readline.get_begidx()] if readline is not None else text
            matches = [item for item in command_names if item.startswith(prefix or text)]
            return matches[state] if state < len(matches) else None
        readline.set_completer(complete_command)
        readline.parse_and_bind('tab: complete')

    command_items = [
        ("/help", t(locale, "cmd_help")), ("/config", t(locale, "cmd_config")),
        ("/model", t(locale, "cmd_model")), ("/permissions", t(locale, "cmd_permissions")),
        ("/logout", t(locale, "cmd_logout")), ("/status", t(locale, "cmd_status")),
        ("/plan", t(locale, "cmd_plan")), ("/usage", t(locale, "cmd_usage")), ("/diff", t(locale, "cmd_diff")),
        ("/checkpoint", t(locale, "cmd_checkpoint")), ("/clear", t(locale, "cmd_clear")), ("/cancel", t(locale, "cmd_cancel")), ("/exit", t(locale, "cmd_exit")),
    ]

    def command_menu() -> str:
        if not sys.stdin.isatty() or termios is None or tty is None:
            return "/help"
        index = 0
        while True:
            print("\033[2J\033[H", end="")
            print(t(locale, "commands_title") + "\n")
            for i, (command, description) in enumerate(command_items):
                marker = "❯" if i == index else " "
                print(f"{marker} {command:<14} {description}")
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                key = sys.stdin.read(1)
                if key in {"\n", "\r"}:
                    return command_items[index][0]
                if key == "\x1b":
                    if sys.stdin.read(1) == "[":
                        code = sys.stdin.read(1)
                        if code == "A": index = (index - 1) % len(command_items)
                        elif code == "B": index = (index + 1) % len(command_items)
                    else:
                        return ""
                elif key == "k": index = (index - 1) % len(command_items)
                elif key == "j": index = (index + 1) % len(command_items)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def reconfigure_current_session() -> None:
        nonlocal provider, base_url, api_key, model
        new_url = input(f"Provider base URL [{base_url}] ❯ ").strip() or base_url
        print(t(locale, "api_key_hint"))
        new_key = os.getenv("FUN_API_KEY") or _secret_input("API key ❯ ")
        if new_key is None:
            return
        if not new_key:
            print(t(locale, "api_key_required"))
            return
        new_model = _choose_model(new_url, new_key, model, locale)
        if not new_model:
            return
        base_url, api_key, model = new_url, new_key, new_model
        saved.base_url, saved.api_key, saved.model = base_url, api_key, model
        os.environ["FUN_API_KEY"] = api_key
        saved.save(config_path)
        provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
        runtime.provider = provider
        runtime.model = model
        print(t(locale, "saved"))

    def run_interactive_task(task: object) -> None:
        print(renderer.plan(task.plan, task.plan_status))
        if provider:
            try:
                print(t(locale, "thinking"), end=" ", flush=True)
                def status(kind: str, payload: dict[str, object]) -> None:
                    if kind in {"tool.started", "tool.executing"}:
                        print(f"\n{t(locale, 'tool_running').format(name=payload.get('name', 'tool'))}", flush=True)
                    elif kind == "approval.pending":
                        print(f"{t(locale, 'approval_details')} · {payload.get('name', 'tool')} · risk={payload.get('risk', '?')} · args={payload.get('arguments', {})}", flush=True)
                    elif kind == "tool.progress":
                        print(f"  {payload.get('name', 'tool')} · {payload.get('elapsed_ms', 0)}ms", flush=True)
                    elif kind == "tool.completed":
                        print(f"✓ ({payload.get('elapsed_ms', 0)}ms)", flush=True)
                    elif kind == "tool.failed":
                        if payload.get("error") == "TimeoutExpired" or "COMMAND_TIMEOUT" in str(payload.get("text", "")):
                            print("× " + t(locale, "tool_timeout").format(elapsed_ms=payload.get("elapsed_ms", 0)), flush=True)
                        else:
                            print(f"× ({payload.get('elapsed_ms', 0)}ms) · use /status for details", flush=True)
                output = runtime.run_model_turn(on_text=lambda chunk: print(chunk, end="", flush=True), on_status=status)
                runtime.complete(output)
                print()
            except Exception as exc:
                print(f"\n× {_friendly_error(exc, locale)}", file=sys.stderr)
                runtime.fail(str(exc))
        else:
            print("Model not configured. Use --configure or set FUN_API_URL, FUN_API_KEY, and FUN_MODEL.")
            runtime.stop()

    if provider and sys.stdin.isatty() and termios is not None and tty is not None:
        tui = TerminalUI(locale=locale, commands=["/help", "/prompt", "/status", "/usage", "/plan", "/pause", "/resume", "/cancel", "/clear", "/stop", "/exit"])
        tui.state.model_name = runtime.model
        tui.state.approval_mode = runtime.policy.mode.value
        tui.state.task_state = runtime.task.status if runtime.task else "idle"
        tui.background_provider = lambda: [
            {"id": item.id, "status": item.status, "goal": item.goal, "result": str(item.result)[:120] if item.result is not None else "", "error": item.error or ""}
            for item in runtime.background.list()
        ]
        if runtime.task and runtime.task.messages:
            tui.state.restore_messages(runtime.task.messages)
        if runtime.task and runtime.task.status == "recovery_required":
            tui.set_recovery(runtime.recovery_summary() or {})
        def tui_submit(text: str) -> None:
            nonlocal provider, model
            if text in {"/quit", "/exit"}:
                tui.post("quit")
                return
            if text == "/help":
                tui.append_assistant(renderer.help())
                return
            if text == "/prompt":
                preview = saved.system_prompt.strip() or "(default Fun safety prompt)"
                tui.append_assistant(f"System prompt: {preview[:500]}")
                return
            if text.startswith("/prompt "):
                value = text.split(" ", 1)[1].strip()
                runtime.system_prompt = runtime.system_prompt.split("\n\nAdditional user preferences", 1)[0].rstrip() + "\n\nAdditional user preferences (follow when they do not conflict with Runtime safety rules):\n" + value[:12000]
                saved.system_prompt = value[:12000]
                if runtime.task and runtime.task.messages and runtime.task.messages[0].get("role") == "system":
                    runtime.task.messages[0]["content"] = runtime.system_prompt
                    runtime.emit("task.message", runtime.task.id, message={"role": "system", "content": runtime.system_prompt})
                saved.save(config_path)
                tui.set_status("system prompt updated")
                return
            if text == "/status":
                tui.set_status(f"task={runtime.task.status if runtime.task else 'idle'} · model={runtime.model}")
                return
            if text == "/permissions":
                modes = ["ask", "smart", "auto"]
                current = runtime.policy.mode.value
                selected = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "smart"
                runtime.policy.mode = selected
                saved.approval = selected
                saved.save(config_path)
                tui.state.approval_mode = selected
                tui.set_status(f"approval={selected}")
                return
            if text in {"/config", "/setup"}:
                def apply_config(values: dict[str, str] | None) -> None:
                    nonlocal base_url, api_key, model, provider
                    if not values:
                        tui.set_status("configuration cancelled")
                        return
                    base_url = values.get("base_url", base_url).strip() or base_url
                    new_key = values.get("api_key", "").strip() or api_key
                    model = values.get("model", model).strip() or model
                    api_key = new_key
                    saved.base_url, saved.model = base_url, model
                    if api_key:
                        saved.api_key = api_key
                        os.environ["FUN_API_KEY"] = api_key
                    saved.save(config_path)
                    if api_key and base_url and model:
                        provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
                        runtime.provider, runtime.model = provider, model
                    tui.state.model_name = model
                    tui.set_status("configuration updated")
                tui.open_modal("Provider configuration", ["base_url", ("api_key", True), "model"], apply_config)
                return
            if text == "/model":
                if not provider:
                    tui.append_assistant(t(locale, "no_provider"))
                    return
                def choose_model_done(selected: str | None) -> None:
                    nonlocal model, provider
                    if selected:
                        model = selected
                        saved.model = selected
                        saved.save(config_path)
                        provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
                        runtime.provider, runtime.model = provider, model
                        tui.state.model_name = model
                        tui.set_status(f"model={model}")
                def load_models() -> None:
                    try:
                        models = provider.list_models()
                        tui.post("model_options", models)
                    except Exception as exc:
                        tui.append_assistant("× " + _friendly_error(exc, locale))
                tui.open_select("Choose model", [model, "(loading models…)"], choose_model_done)
                tui.modal["loading"] = True
                threading.Thread(target=load_models, daemon=True).start()
                return
            if text.startswith("/model "):
                selected = text.split(maxsplit=1)[1].strip()
                if not selected:
                    tui.append_assistant("Usage: /model <model-id>")
                    return
                model = selected
                saved.model = selected
                saved.save(config_path)
                if provider:
                    provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
                    runtime.provider = provider
                runtime.model = model
                tui.state.model_name = model
                tui.set_status(f"model={model}")
                return
            if text == "/clear":
                tui.state.transcript.clear()
                tui.set_status("ready")
                return
            if text == "/usage":
                tui.set_status(runtime.usage.summary())
                return
            if text == "/plan":
                tui.append_assistant(renderer.plan(runtime.task.plan if runtime.task else [], runtime.task.plan_status if runtime.task else []))
                return
            if text == "/pause":
                runtime.pause()
                tui.set_status("paused")
                return
            if text == "/resume":
                runtime.resume()
                tui.set_status("ready")
                return
            if text == "/stop":
                runtime.stop()
                tui.set_status("stopped")
                return
            if text.startswith("/recover"):
                action = text.split(maxsplit=1)[1] if len(text.split()) > 1 else "resume"
                try:
                    runtime.acknowledge_recovery(action)
                    tui.state.mode = "working" if action in {"resume", "discard", "mark_failed"} else "ready"
                    tui.set_status(f"recovery={action}")
                    if provider and action in {"resume", "discard", "mark_failed"} and runtime.task and runtime.task.status == "running":
                        def continue_recovered() -> None:
                            tui.post("status", "working")
                            try:
                                output = runtime.run_model_turn(on_text=lambda chunk: tui.post("assistant", chunk), on_status=lambda kind, payload: tui.post("tool", (kind, payload)))
                                runtime.complete(output)
                                tui.post("status", "ready")
                            except Exception as exc:
                                runtime.fail(str(exc))
                                tui.post("assistant", "× " + _friendly_error(exc, locale))
                                tui.post("status", "failed")
                        threading.Thread(target=continue_recovered, daemon=True).start()
                except RuntimeError as exc:
                    tui.append_assistant("× " + str(exc))
                return
            if text.startswith("/cancel "):
                try:
                    runtime.cancel_background_task(text.split(maxsplit=1)[1].strip())
                    tui.set_status("cancellation requested")
                except RuntimeError as exc:
                    tui.append_assistant("× " + str(exc))
                return
            if text.startswith("/"):
                tui.append_assistant(t(locale, "unknown_command"))
                return
            def worker() -> None:
                tui.post("status", "working")
                try:
                    task = runtime.create_task(text)
                    tui.post("status", t(locale, "thinking"))
                    output = runtime.run_model_turn(
                        on_text=lambda chunk: tui.post("assistant", chunk),
                        on_status=lambda kind, payload: (tui.bind_approval(str(payload.get("call_id", "")), str(payload.get("name", "tool")), dict(payload.get("arguments") or {})) if kind == "approval.pending" else None) or tui.post("tool", (kind, payload)),
                    )
                    runtime.complete(output)
                    tui.post("status", "ready")
                except Exception as exc:
                    runtime.fail(str(exc))
                    tui.post("assistant", "× " + _friendly_error(exc, locale))
                    tui.post("status", "failed")
            threading.Thread(target=worker, daemon=True).start()
        tui.recovery_handler = lambda action: tui_submit(f"/recover {action}")
        try:
            tui.run(tui_submit)
        finally:
            runtime.stop()
        return 0

    try:
        while True:
            text = input(f"\n{renderer.prompt(provider is not None)}").strip()
            if text == "/":
                text = command_menu()
                if text:
                    print(text)
            if not text:
                continue
            if text.startswith("/") and not text.startswith(("/goal ", "/recover ", "/cancel ")):
                known = {item[0] for item in command_items} | {"/setup", "/quit"}
                resolved, matches = resolve_command_prefix(text, known)
                if resolved is not None:
                    text = resolved
                elif matches:
                    print("\n".join(matches))
                    continue
                else:
                    print(t(locale, "unknown_command"), file=sys.stderr)
                    continue
            if text in {"/quit", "/exit"}:
                break
            if text == "/help":
                print(renderer.help())
                continue
            if text in {"/config", "/setup"}:
                reconfigure_current_session()
                continue
            if text == "/logout":
                saved.clear_credentials(config_path)
                provider = None
                runtime.provider = None
                runtime.model = ""
                base_url = api_key = model = ""
                print("\033[2J\033[H", end="")
                print(renderer.header(str(runtime.tools.guard.root), False, runtime.policy.mode.value))
                print(renderer.finding(t(locale, "offline")))
                print("✓ " + t(locale, "removed"))
                continue
            if text == "/permissions":
                print(t(locale, "permission") + ": " + t(locale, "permission_options"))
                try:
                    selected_approval = input("❯ ").strip()
                except (EOFError, KeyboardInterrupt):
                    print(t(locale, "cancel_status"), file=sys.stderr)
                    continue
                approval = {"1": "ask", "2": "smart", "3": "auto"}.get(selected_approval, approval)
                runtime.policy.mode = approval
                saved.approval = approval
                saved.save(config_path)
                print(f"✓ permission mode: {approval}")
                continue
            if text == "/model":
                if not provider:
                    print("Configure a provider first.")
                else:
                    selected = _choose_model(base_url, api_key, model, locale)
                    if selected:
                        model = selected
                        saved.model = selected
                        saved.save(config_path)
                        provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
                        runtime.provider = provider
                        runtime.model = model
                        print(f"✓ model: {model}")
                continue
            if text == "/clear":
                print("\033[2J\033[H", end="")
                continue
            if text.startswith("/cancel"):
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or not parts[1].strip():
                    print("Usage: /cancel <background-task-id>", file=sys.stderr)
                    continue
                try:
                    runtime.cancel_background_task(parts[1].strip())
                    print(f"✓ cancellation requested: {parts[1].strip()}")
                except RuntimeError as exc:
                    print(f"× {exc}", file=sys.stderr)
                continue
            if text == "/goal":
                print(runtime.goal() or "(no active goal)")
                continue
            if text.startswith("/goal"):
                try:
                    parts = shlex.split(text)
                except ValueError as exc:
                    print(f"× invalid goal syntax: {exc}", file=sys.stderr)
                    continue
                goal_text = " ".join(parts[1:]).strip()
                if not goal_text:
                    print("Usage: /goal <what you want Fun to do>", file=sys.stderr)
                    continue
                try:
                    task = runtime.set_goal(goal_text)
                    run_interactive_task(task)
                except Exception as exc:
                    print(f"× {exc}", file=sys.stderr)
                continue
            if text == "/status":
                status = runtime.task.status if runtime.task else "idle"
                agent_state = runtime.task.agent_state if runtime.task else "idle"
                recovery = runtime.task.recovery_reason if runtime.task else None
                print(f"session={runtime.session_id} task={status} agent={agent_state} policy={runtime.policy.mode.value}")
                if runtime.last_model_timing:
                    timing = runtime.last_model_timing
                    print(f"model timing: ttft={timing.get('ttft_ms', '?')}ms · step={timing.get('step_ms', '?')}ms")
                if recovery:
                    pending = runtime.recovery_summary() or {}
                    print("! " + t(locale, "pending_tool").format(name=pending.get("name", "unknown tool"), call_id=pending.get("call_id", "?")))
                    print(f"  args: {pending.get('arguments', {})}")
                    print(t(locale, "recovery_actions"))
                if runtime.task and runtime.task.plan_error:
                    print(f"! plan rejected: {runtime.task.plan_error}")
                    if runtime.task.plan_error_summary:
                        print(f"  proposal: {runtime.task.plan_error_summary}")
                if runtime.task and runtime.task.failure_reason:
                    print("! " + t(locale, "task_failed").format(reason=runtime.task.failure_reason[:240]))
                if runtime.task and runtime.task.result is not None:
                    print(f"result: {runtime.task.result[:240]}")
                background = runtime.background.list()
                if background:
                    print("background:")
                    for item in background:
                        result = str(item.result)[:120] if item.result is not None else ""
                        detail = result or (item.error or "")
                        print(f"  {item.id} {item.status} · {item.goal[:80]}" + (f" · {detail}" if detail else ""))
                print(runtime.usage.summary())
                continue
            if text == "/usage":
                print(runtime.usage.summary())
                continue
            if text == "/diff":
                snapshot = runtime.checkpoint("view")
                print(snapshot["diff"] or "(no working tree diff)")
                continue
            if text == "/plan":
                print(renderer.plan(runtime.task.plan if runtime.task else [], runtime.task.plan_status if runtime.task else []))
                continue
            if text == "/pause":
                runtime.pause()
                print("● paused")
                continue
            if text == "/resume":
                runtime.resume()
                print("● running")
                continue
            if text.startswith("/recover"):
                action = text.split(maxsplit=1)[1] if len(text.split()) > 1 else "resume"
                try:
                    runtime.acknowledge_recovery(action)
                    print(f"● recovery acknowledged; {action}")
                    if provider and action in {"resume", "discard", "mark_failed"} and runtime.task and runtime.task.status == "running":
                        run_interactive_task(runtime.task)
                    elif not provider and action in {"resume", "discard", "mark_failed"}:
                        print(t(locale, "offline"))
                except RuntimeError as exc:
                    print(f"× {exc}", file=sys.stderr)
                continue
            if text == "/stop":
                runtime.stop()
                print("✓ stopped")
                continue
            if text == "/checkpoint":
                runtime.checkpoint()
                print(renderer.success("checkpoint created"))
                continue
            if text.startswith("/restore"):
                print("Restore requires a checkpoint snapshot in the current process.")
                continue
            if not provider:
                print(renderer.finding("离线模式：请先配置 Provider，再开始任务。" if renderer.zh else "Offline mode: configure a provider before starting a task."))
                continue
            task = runtime.create_task(text)
            run_interactive_task(task)
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
