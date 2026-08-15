from __future__ import annotations

import argparse
import os
import shlex
import sys

from .config import FunConfig
from .dashboard import serve
from .provider import ModelConfig, OpenAICompatible
from .renderer import TerminalRenderer
from .runtime import Runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fun", description="A safety-first terminal coding agent runtime.")
    parser.add_argument("goal", nargs="?", help="A one-shot task goal")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--approval", choices=("ask", "smart", "auto"), default="smart")
    parser.add_argument("--version", action="version", version="fun 1.0.0a1")
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
    if args.configure:
        if not sys.stdin.isatty():
            print("Configuration requires an interactive terminal.", file=sys.stderr)
            return 2
        saved.base_url = input(f"Base URL [{saved.base_url}]: ").strip() or saved.base_url
        saved.api_key = input("API key [hidden, leave blank to keep]: ").strip() or saved.api_key
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
    runtime = Runtime(args.workspace, args.approval, provider, state_dir=state_dir, approve=approve, telemetry=telemetry, model=model)
    renderer = TerminalRenderer(color=sys.stdout.isatty())
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

    print("FUN HARNESS")
    print("Coding should feel good.")
    print(f"Workspace: {runtime.tools.guard.root}")
    print("Type a task, or Ctrl-C to exit.")

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
            text = input("\n> ").strip()
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
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
            task = runtime.create_task(text)
            run_interactive_task(task)
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
