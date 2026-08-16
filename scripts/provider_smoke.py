"""Optional live OpenAI-compatible provider smoke test.

Usage (never runs unless explicitly invoked):
    FUN_API_URL=... FUN_API_KEY=... FUN_MODEL=... python scripts/provider_smoke.py
"""
from __future__ import annotations

import os
import sys

from fun.provider import ModelConfig, OpenAICompatible, ProviderError


def main() -> int:
    required = {name: os.environ.get(name, "") for name in ("FUN_API_URL", "FUN_API_KEY", "FUN_MODEL")}
    missing = [name for name, value in required.items() if not value]
    if missing:
        print("missing environment: " + ", ".join(missing), file=sys.stderr)
        return 2
    provider = OpenAICompatible(ModelConfig(required["FUN_API_URL"], required["FUN_API_KEY"], required["FUN_MODEL"], timeout=30.0))
    try:
        chunks = list(provider.stream([{"role": "user", "content": "Reply with exactly: FUN_SMOKE_OK"}]))
    except ProviderError as exc:
        print(f"provider_error={exc.error_tag}", file=sys.stderr)
        return 1
    if not chunks:
        print("provider returned no stream chunks", file=sys.stderr)
        return 1
    print(f"stream_chunks={len(chunks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
