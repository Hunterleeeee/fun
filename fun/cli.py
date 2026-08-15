from __future__ import annotations

import argparse
import os
import sys

from .runtime import Runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fun", description="A safety-first terminal coding agent runtime.")
    parser.add_argument("goal", nargs="?", help="A one-shot task goal")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--approval", choices=("ask", "smart", "auto"), default="smart")
    parser.add_argument("--version", action="version", version="fun 1.0.0a1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = Runtime(args.workspace, args.approval)
    if args.goal:
        task = runtime.create_task(args.goal)
        print(f"Fun · {args.workspace}")
        print(f"◇ PLAN  {task.goal}")
        print("V1 Core runtime initialized. Model planning and edit execution are next milestones.")
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
