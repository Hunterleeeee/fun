<div align="center">

# Fun

### Coding should feel good.

**A safety-first terminal coding agent for real software work.**

[![Status: Alpha](https://img.shields.io/badge/status-alpha-f5b642.svg)](https://github.com/Hunterleeeee/fun)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)

[Install](#install) · [What Fun does](#what-fun-does) · [Architecture](#how-it-works) · [Roadmap](#roadmap) · [Contributing](#contributing)

</div>

---

## The idea

Most coding agents optimize for *answering*. Fun is being built to optimize for **finishing work safely**.

It keeps the model in its proper role—proposing the next action—and puts the Runtime in charge of facts, permissions, tool execution, recovery, and the final truth.

```text
You describe the work
        ↓
Fun makes a small plan
        ↓
Fun explores, reads, edits, and runs checks
        ↓
You see the evidence, diff, and approval boundary
        ↓
Fun stops when the work is verified
```

> Fun is early. This repository is an honest, runnable Alpha—not a promise that 1.0 is finished.

## Install

### One command

Requires **Python 3.11+** and Git:

```bash
curl -fsSL https://raw.githubusercontent.com/Hunterleeeee/fun/main/install.sh | sh
```

Then start Fun:

```bash
export PATH="$HOME/.local/bin:$PATH"  # only needed once if ~/.local/bin is not already on PATH
fun
```

The installer creates an isolated environment in `~/.fun`, installs the latest GitHub version, and exposes the `fun` command. Review `install.sh` before piping it to a shell if you prefer a manual install.

### From source

```bash
git clone https://github.com/Hunterleeeee/fun.git
cd fun
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
fun
```

Configure an OpenAI-compatible provider:

```bash
fun --configure
```

Or use environment variables:

```bash
export FUN_API_URL="https://api.openai.com/v1"
export FUN_API_KEY="your-api-key"
export FUN_MODEL="your-model"
fun "inspect the project"
```

## A small example

```text
$ fun

FUN HARNESS
Coding should feel good.
Workspace: ~/src/my-project

> fix the failing login test

◇ PLAN
  ○ inspect workspace
  ○ locate relevant code
  ○ apply a minimal change
  ○ run focused validation

◌ model.requested
◌ read
✓ tool.completed
◌ edit
? Allow edit (medium)? [y/N]
✓ validation.completed

> /diff
--- tests/test_login.py
+++ tests/test_login.py
```

Useful controls:

| Command | Purpose |
| --- | --- |
| `/status` | Show task, agent, policy, and usage state |
| `/plan` | Show the current PlanStep statuses |
| `/diff` | Inspect the current working-tree diff |
| `/usage` | Show token and TTFT metrics |
| `/pause` | Pause at the next safe Runtime boundary |
| `/resume` | Continue a paused task |
| `/checkpoint` | Save a local checkpoint |
| `/stop` | Stop the current task and release the workspace lock |
| `/quit` | Leave the REPL |

## What Fun does

### V1 Core today

- **One workspace, one active task** — a small scope that is easy to reason about.
- **Plan + bounded Agent Loop** — no unbounded tool retries.
- **Four practical tools** — `explore`, `read`, `edit`, and `exec`.
- **Hash-checked editing** — edits refuse to overwrite a file that changed since it was read.
- **Approval modes** — `ask`, `smart`, and `auto`, with critical operations still blocked.
- **Workspace safety** — path boundaries, protected names, command timeout, output limits, and process-group termination.
- **Event-sourced facts** — Runtime state can be replayed from SQLite events.
- **Checkpoint and restore foundations** — inspect diffs and recover a Git workspace snapshot.
- **Pause/resume/stop** — controls are enforced at Agent node boundaries.
- **Single-column terminal UI** — readable in a normal 80-column terminal.

### Not promised yet

These are deliberately staged for V1.x or later:

- Anthropic-native provider
- Automatic model discovery
- Web search
- `/inject` and `/queue`
- Automatic context compaction
- Cross-session memory
- Multi-agent execution
- Complete recovery of unknown external side effects

## How it works

Fun does not treat the model transcript as the source of truth:

```text
Provider
   │ proposes
   ▼
Runtime State Machine ──► Event Store ──► Replay / Recovery
   │
   ├── Policy ──► approval and risk boundary
   ├── Tool Executor ──► workspace side effects
   ├── PlanSteps ──► evidence and validation
   └── Renderer ──► single-column terminal projection
```

The core rule is simple:

> **The model may propose an action. Only the Runtime may authorize and execute it.**

Fun intentionally does **not** depend on LangChain or LangGraph. Their useful ideas—state, nodes, interrupts, checkpoints, and durable execution—are implemented as small, inspectable Fun primitives so that safety and recovery remain under Fun's control.

## Safety notes

Fun is an Alpha. Do not use it for unattended destructive automation.

The current safety boundary includes:

- workspace realpath checks
- protected `.git`, `.env`, `.pem`, and `.key` paths
- expected-hash file edits
- approval before medium/high-risk operations
- critical command blocking
- command timeout and process-group cleanup
- bounded output
- workspace locking
- auditable lifecycle events

Shell execution is still a capability with inherent risk. Review commands and use `ask` or `smart` approval while evaluating Fun.

Report security issues privately using [`SECURITY.md`](SECURITY.md).

## Project status

Fun is being developed in public. The current Alpha has a working Runtime foundation and test suite, but it is not feature-complete 1.0.

The authoritative contract and design documents are:

1. [`docs/fun-v1-contract.md`](docs/fun-v1-contract.md)
2. [`docs/fun-runtime-spec.md`](docs/fun-runtime-spec.md)
3. [`docs/fun-complete-design.md`](docs/fun-complete-design.md)
4. [`docs/fun-open-source-blueprint.md`](docs/fun-open-source-blueprint.md)

## Roadmap

### Now — V1 Core

- [x] Event replay and local Runtime recovery
- [x] OpenAI-compatible streaming Agent Loop
- [x] PlanStep evidence and bounded validation/repair
- [x] Approval and explicit Tool lifecycle events
- [x] Safe Exec baseline and workspace lock
- [x] Interactive REPL execution
- [ ] Stronger ChangeSet restore and user-change conflict detection
- [ ] Full context manifest and manual `/compact`

### Next — V1.x

- [ ] Anthropic-native provider
- [ ] `/inject` and `/queue`
- [ ] Evidence-based workspace memory
- [ ] Automatic compaction
- [ ] Provider capability discovery
- [ ] Better recovery UI for unknown side effects

### Later

- [ ] Web search and external capabilities
- [ ] Plugin and Tool SDK
- [ ] Optional LangGraph adapter (never the safety authority)
- [ ] Multi-agent workflows

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q fun tests
python3 -m fun --help
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for project conventions, security expectations, and the Core/V1.x/Future boundary.

## License

Fun is released under the [MIT License](LICENSE).
