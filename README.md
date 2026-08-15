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
- Interactive REPL runs the same bounded Agent Loop as one-shot tasks when a provider is configured.

V1.x will add Anthropic native support, model discovery, web search, inject/queue, automatic compaction, and richer recovery.

## Quick start

### One-line installer

Requires Python 3.11+ and Git. The installer creates an isolated environment under `~/.fun`, installs Fun, and links the `fun` command into `~/.local/bin`.

```bash
curl -fsSL https://raw.githubusercontent.com/Hunterleeeee/fun/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
fun
```

If `~/.local/bin` is already on your PATH, the last two commands become simply:

```bash
curl -fsSL https://raw.githubusercontent.com/Hunterleeeee/fun/main/install.sh | sh
fun
```

### Manual install

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
fun --help
fun
fun "inspect the project"
```

Configure an OpenAI-compatible provider interactively:

```bash
fun --configure
```

The current alpha starts a local Runtime and displays the task boundary. Provider credentials and the full model loop are implemented behind the public contracts in `docs/`.

## Documentation

Start with [`docs/README.md`](docs/README.md). The V1 contract is [`docs/fun-v1-contract.md`](docs/fun-v1-contract.md), and the open-source engineering plan is [`docs/fun-open-source-blueprint.md`](docs/fun-open-source-blueprint.md).

## Safety

Fun is designed to keep all tool execution behind a workspace guard and policy engine. Do not use the current alpha skeleton for unreviewed destructive automation. Report security issues privately according to [`SECURITY.md`](SECURITY.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Every feature must state whether it belongs to Core, V1.x, or Future and update the relevant contract and tests.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
