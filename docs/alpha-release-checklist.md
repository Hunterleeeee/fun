# Fun 1.0 Alpha Release Checklist

This checklist is the repeatable release gate for the public Alpha line.

## Current release

- Latest Alpha: `v1.0.0a6`
- Tag commit: verify with `git rev-parse v1.0.0a6^{}`
- Main parity: verify `git rev-parse HEAD` equals `git rev-parse origin/main`

## Local gate

```bash
python3 -m unittest discover -s tests -q
python3 -m compileall -q fun tests
git diff --check
```

Expected test count changes over time; the command must finish with `OK`.

## GitHub Actions gate

The tag workflow must pass all of the following before calling the release usable:

- release ref is a tag
- tagged commit is an ancestor of `origin/main`
- tag version exactly equals `pyproject.toml` metadata
- wheel and sdist metadata exactly match the project version
- `SHA256SUMS` is generated and self-verified
- only wheel, sdist, and checksum artifacts are uploaded
- the GitHub prerelease is marked as an Alpha prerelease

The main branch CI must pass Python 3.11 and 3.12 tests, build, checksum, wheel install, and sdist-to-wheel install smoke checks.

## Artifact gate

For a downloaded release:

```bash
sha256sum -c SHA256SUMS
python3 -m pip install --no-deps fun_harness-<version>-py3-none-any.whl
```

The runtime requires Python 3.11 or newer and has no third-party runtime dependencies. Do not treat a source checkout on `main` as equivalent to a tagged release artifact.

## Release discipline

- Never move or overwrite an existing Alpha tag.
- Increment the PEP 440 pre-release version for fixes after a published tag.
- Update `pyproject.toml`, `fun/cli.py`, `README.md`, and `CHANGELOG.md` together.
- Keep release permissions minimal and do not enable PyPI publishing implicitly.
