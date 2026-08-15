# Contributing to Fun

Thanks for helping build Fun.

## Before opening a change

1. Read [`docs/README.md`](docs/README.md) and [`docs/fun-v1-contract.md`](docs/fun-v1-contract.md).
2. Classify the change as Core, V1.x, or Future.
3. For protocol, state-machine, security, or default-behavior changes, add or update an ADR and the relevant contract.
4. Add tests for behavior changes.
5. Never include API keys, private code, customer data, or raw provider credentials in fixtures or logs.

## Pull requests

Please include:

- user problem and proposed behavior;
- affected package or Runtime layer;
- security and recovery impact;
- tests run;
- documentation and changelog updates;
- whether the default behavior changes.

## Commits

Use Conventional Commits when practical, for example:

```text
feat(runtime): add task step state
fix(workspace): reject symlink escape
test(recovery): cover unknown exec
```

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m pytest
```

Keep Runtime facts in events. Do not make the terminal renderer infer state from model prose, and do not let tools bypass the workspace guard or policy engine.
