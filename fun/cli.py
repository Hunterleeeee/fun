from __future__ import annotations

import argparse
import os

from .provider import ModelConfig, OpenAICompatible
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = None
    if args.base_url and args.api_key and args.model:
        provider = OpenAICompatible(ModelConfig(args.base_url, args.api_key, args.model))
    runtime = Runtime(args.workspace, args.approval, provider)
    if args.goal:
        task = runtime.create_task(args.goal)
        print(f"Fun · {args.workspace}")
        print(f"◇ PLAN  {task.goal}")
        if provider:
            try:
                runtime.run_model_turn(on_text=lambda text: print(text, end="", flush=True))
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
                print(f"session={runtime.session_id} task={runtime.task.status if runtime.task else 'idle'}")
                continue
            task = runtime.create_task(text)
            print(f"◇ PLAN  {task.goal}")
            print("V1 Core runtime initialized. Use /status or /quit.")
            runtime.stop()
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
