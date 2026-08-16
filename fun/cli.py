from __future__ import annotations

import argparse
import os
from pathlib import Path
import getpass
import shlex
import sys
try:
    import termios
    import tty
except ImportError:  # Windows: menu falls back to typed commands
    termios = None
    tty = None

from .config import FunConfig
from .dashboard import serve
from .provider import ModelConfig, OpenAICompatible, ProviderError
from .i18n import t
from .renderer import TerminalRenderer
from .runtime import Runtime


def _choose_model(base_url: str, api_key: str, current: str = "", locale: str = "en-US") -> str | None:
    try:
        models = OpenAICompatible(ModelConfig(base_url, api_key, current or "models-placeholder")).list_models()
    except Exception as exc:
        print(t(locale, "model_load_failed"))
        return input(f"Model ID [{current}] (manual fallback) ❯ ").strip() or current or None
    if not models:
        print("Provider returned no models.")
        return input(f"Model ID [{current}] (manual fallback) ❯ ").strip() or current or None
    print(t(locale, "choose_model"))
    for index, model_id in enumerate(models, 1):
        print(f"  [{index}] {model_id}")
    while True:
        choice = input(f"Choose model [1-{len(models)}] ❯ ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        print("Enter a model number, or use Ctrl-C to cancel.")


def _secret_input(prompt: str) -> str | None:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nConfiguration cancelled.", file=sys.stderr)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fun", description="A safety-first terminal coding agent runtime.")
    parser.add_argument("goal", nargs="?", help="A one-shot task goal")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--approval", choices=("ask", "smart", "auto"), default="smart")
    parser.add_argument("--locale", choices=("zh-CN", "en-US"), default=os.getenv("FUN_LOCALE"), help="UI language")
    parser.add_argument("--version", action="version", version="fun 1.0.0a6")
    parser.add_argument("--base-url", default=os.getenv("FUN_API_URL"))
    parser.add_argument("--api-key", default=os.getenv("FUN_API_KEY"))
    parser.add_argument("--model", default=os.getenv("FUN_MODEL"))
    parser.add_argument("--non-interactive", action="store_true", help="Never wait for interactive approval")
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
        locale = "zh-CN" if input("❯ ").strip() == "1" else "en-US"
        saved.locale = locale
        saved.save(config_path)
    renderer = TerminalRenderer(color=sys.stdout.isatty(), locale=locale)
    if args.configure:
        if not sys.stdin.isatty():
            print("Configuration requires an interactive terminal.", file=sys.stderr)
            return 2
        saved.base_url = input(f"Base URL [{saved.base_url}]: ").strip() or saved.base_url
        env_key = os.getenv("FUN_API_KEY", "")
        if env_key:
            print("Using FUN_API_KEY from environment; no key input needed.")
            saved.api_key = env_key
        else:
            print("API key: paste is supported; input is hidden and will not echo.")
            saved.api_key = getpass.getpass("API key [Enter to keep current]: ").strip() or saved.api_key
        saved.model = input(f"Model [{saved.model}]: ").strip() or saved.model
        telemetry_choice = input(f"Enable private telemetry? [{'Y/n' if saved.telemetry else 'y/N'}]: ").strip().lower()
        if telemetry_choice in {"y", "yes"}:
            saved.telemetry = True
            saved.telemetry_endpoint = input(f"Private telemetry endpoint [{saved.telemetry_endpoint}]: ").strip() or saved.telemetry_endpoint
        elif telemetry_choice in {"n", "no"}:
            saved.telemetry = False
            saved.telemetry_endpoint = ""
        saved.save(config_path)
        print(f"Saved provider configuration to {config_path}")
        if saved.api_key:
            print("API key is not stored in the config file; export FUN_API_KEY before running Fun.")
        return 0
    base_url = args.base_url or saved.base_url
    api_key = args.api_key or saved.api_key
    model = args.model or saved.model
    provider = None
    if base_url and api_key and model:
        provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
    def approve(name: str, risk: object) -> bool:
        if args.non_interactive or not sys.stdin.isatty():
            return False
        try:
            return input(f"? Allow {name} ({risk})? [y/N] ").strip().lower() in {"y", "yes"}
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
    if not provider and not args.goal and sys.stdin.isatty():
        print(renderer.welcome(False, os.path.abspath(args.workspace)))
        choice = input("\nSelect [1/2/3/4/q] ❯ ").strip().lower()
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
            saved.api_key = os.getenv("FUN_API_KEY") or getpass.getpass("API key ❯ ").strip()
            if not saved.api_key:
                print(renderer.error("API key is required."), file=sys.stderr)
                return 2
            saved.model = _choose_model(saved.base_url, saved.api_key, saved.model, locale) or ""
            if not saved.model:
                return 2
            print("Permission mode: [1] ask  [2] smart (recommended)  [3] auto")
            args.approval = {"1": "ask", "2": "smart", "3": "auto"}.get(input("❯ ").strip(), "smart")
            os.environ["FUN_API_KEY"] = saved.api_key
            saved.save(config_path)
            base_url, api_key, model = saved.base_url, saved.api_key, saved.model
            provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
            print(renderer.setup_complete())
        elif choice == "3":
            base_url, api_key, model = saved.base_url, saved.api_key, saved.model
            provider = OpenAICompatible(ModelConfig(base_url, api_key, model))
    runtime = Runtime(args.workspace, args.approval, provider, state_dir=state_dir, approve=approve, telemetry=telemetry, model=model)
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
                print(f"\n× {exc}", file=__import__("sys").stderr)
                runtime.stop()
                return 1
        else:
            print("Model not configured. Set --base-url, --api-key, and --model to run the agent loop.")
        runtime.stop()
        return 0

    print(renderer.header(str(runtime.tools.guard.root), provider is not None, runtime.policy.mode.value))
    if provider:
        print(renderer.welcome(True))
    else:
        print(renderer.finding(t(locale, "offline")))
        print("输入 /help 查看帮助，/setup 了解配置，或 /quit 退出。" if renderer.zh else "Use /help for commands, /setup to configure later, or /quit to exit.")

    command_items = [
        ("/help", "Show help"), ("/config", "Configure provider and credentials"),
        ("/model", "Choose a model"), ("/permissions", "Change approval mode"),
        ("/logout", "Remove saved API key and provider"), ("/status", "Show status"),
        ("/plan", "Show plan"), ("/usage", "Show usage"), ("/diff", "Show diff"),
        ("/checkpoint", "Create checkpoint"), ("/clear", "Clear screen"), ("/exit", "Exit"),
    ]

    def command_menu() -> str:
        if not sys.stdin.isatty() or termios is None or tty is None:
            return "/help"
        index = 0
        while True:
            print("\033[2J\033[H", end="")
            print("Commands · ↑↓ select · Enter accept · Esc cancel\n")
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

    def run_interactive_task(task: object) -> None:
        print(renderer.plan(task.plan, task.plan_status))
        if provider:
            try:
                output = runtime.run_model_turn(on_text=lambda chunk: print(chunk, end="", flush=True))
                runtime.complete(output)
                print()
            except Exception as exc:
                print(f"\n× {exc}", file=sys.stderr)
                runtime.fail(str(exc))
        else:
            print("Model not configured. Use --configure or set FUN_API_URL, FUN_API_KEY, and FUN_MODEL.")
            runtime.stop()

    try:
        while True:
            text = input(f"\n{renderer.prompt(provider is not None)}").strip()
            if text == "/":
                text = command_menu()
                if text:
                    print(text)
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            if text == "/help":
                print(renderer.help())
                continue
            if text in {"/config", "/setup"}:
                print("Run `fun --configure` to configure provider, model, and credentials.")
                continue
            if text == "/logout":
                saved.clear_credentials(config_path)
                provider = None
                print("✓ " + t(locale, "removed"))
                continue
            if text == "/permissions":
                print("Permission mode: [1] ask  [2] smart  [3] auto")
                args.approval = {"1": "ask", "2": "smart", "3": "auto"}.get(input("❯ ").strip(), args.approval)
                runtime.policy.mode = args.approval
                print(f"✓ permission mode: {args.approval}")
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
                        print(f"✓ model: {model}")
                continue
            if text == "/clear":
                print("\033[2J\033[H", end="")
                continue
            if text == "/setup":
                print("Run `fun --configure` in a new terminal to configure the provider.")
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
                if recovery:
                    pending = runtime.recovery_summary() or {}
                    print(f"! recovery required: {recovery} · {pending.get('name', 'unknown tool')} · call={pending.get('call_id', '?')}")
                    print(f"  args: {pending.get('arguments', {})}")
                    print("  action: /recover discard | /recover mark_failed | /recover resume | /recover stop")
                if runtime.task and runtime.task.plan_error:
                    print(f"! plan rejected: {runtime.task.plan_error}")
                    if runtime.task.plan_error_summary:
                        print(f"  proposal: {runtime.task.plan_error_summary}")
                if runtime.task and runtime.task.failure_reason:
                    print(f"! failed: {runtime.task.failure_reason[:240]}")
                if runtime.task and runtime.task.result is not None:
                    print(f"result: {runtime.task.result[:240]}")
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
                runtime.acknowledge_recovery(action)
                print(f"● recovery acknowledged; {action}")
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
