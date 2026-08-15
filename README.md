# Fun Harness

> **Coding should feel good.**

Fun is an open-source, extensible, safety-first terminal coding agent runtime for complex software tasks.

## Status

Fun 1.0 development is starting. The current tree contains the V1 Core runtime foundation: bounded planning, an OpenAI-compatible streaming loop, event persistence, workspace tools, approval boundaries, validation/checkpoint hooks, and a single-column renderer. It is still an alpha and does not yet claim feature-complete 1.0 behavior.

## V1 Core

- One local workspace and one active task.
- OpenAI-compatible provider first.
- `explore`, `read`, `edit`, and `exec` tools.
- Plan + bounded ReAct orchestration.
- Workspace boundary and approval policy.
- Event-based runtime state.
- Diff, validation, checkpoint, stop, and recovery foundations.
- Single-column streaming terminal UI.

V1.x will add Anthropic native support, model discovery, web search, inject/queue, automatic compaction, and richer recovery.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
fun --help
fun "inspect the project"
```

The current bootstrap command initializes the runtime and displays the task boundary. Provider credentials and the full model loop are intentionally being added behind the public contracts in `docs/`.

## Documentation

Start with [`docs/README.md`](docs/README.md). The V1 contract is [`docs/fun-v1-contract.md`](docs/fun-v1-contract.md), and the open-source engineering plan is [`docs/fun-open-source-blueprint.md`](docs/fun-open-source-blueprint.md).

## Safety

Fun is designed to keep all tool execution behind a workspace guard and policy engine. Do not use the current alpha skeleton for unreviewed destructive automation. Report security issues privately according to [`SECURITY.md`](SECURITY.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Every feature must state whether it belongs to Core, V1.x, or Future and update the relevant contract and tests.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
