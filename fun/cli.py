from __future__ import annotations

import argparse
import os
import sys

from .config import FunConfig
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = os.getenv("FUN_STATE_DIR", str(os.path.expanduser("~/.fun")))
    config_path = os.path.join(state_dir, "config.json")
    saved = FunConfig.load(config_path)
    if args.configure:
        if not sys.stdin.isatty():
            print("Configuration requires an interactive terminal.", file=sys.stderr)
            return 2
        saved.base_url = input(f"Base URL [{saved.base_url}]: ").strip() or saved.base_url
        saved.api_key = input("API key [hidden, leave blank to keep]: ").strip() or saved.api_key
        saved.model = input(f"Model [{saved.model}]: ").strip() or saved.model
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
    runtime = Runtime(args.workspace, args.approval, provider, state_dir=state_dir, approve=approve)
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
    try:
        while True:
            text = input("\n> ").strip()
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            if text == "/status":
                status = runtime.task.status if runtime.task else "idle"
                print(f"session={runtime.session_id} task={status} policy={runtime.policy.mode.value}")
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
                print(renderer.plan(runtime.task.plan if runtime.task else []))
                continue
            if text == "/pause":
                runtime.pause()
                print("● paused")
                continue
            if text == "/resume":
                runtime.resume()
                print("● running")
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
            print(renderer.plan(task.plan))
            print("V1 Core runtime initialized. Use /status, /plan, /pause, /stop, or /quit.")
            runtime.stop()
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
