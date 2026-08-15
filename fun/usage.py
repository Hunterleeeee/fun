from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    precision: str = "unknown"
    ttft_ms: int | None = None
    generation_ms: int | None = None
    tool_ms: int = 0

    def merge_provider(self, raw: dict[str, object], ttft_ms: int | None = None) -> None:
        prompt = raw.get("prompt_tokens")
        completion = raw.get("completion_tokens")
        total = raw.get("total_tokens")
        if isinstance(prompt, int):
            self.input_tokens = (self.input_tokens or 0) + prompt
        if isinstance(completion, int):
            self.output_tokens = (self.output_tokens or 0) + completion
        if isinstance(total, int):
            self.total_tokens = (self.total_tokens or 0) + total
        elif isinstance(prompt, int) or isinstance(completion, int):
            self.total_tokens = (self.input_tokens or 0) + (self.output_tokens or 0)
        self.precision = "exact" if any(isinstance(value, int) for value in (prompt, completion, total)) else "estimated"
        if ttft_ms is not None:
            self.ttft_ms = ttft_ms

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()

    def summary(self) -> str:
        input_text = str(self.input_tokens if self.input_tokens is not None else "?")
        output_text = str(self.output_tokens if self.output_tokens is not None else "?")
        ttft_text = f"{self.ttft_ms}ms" if self.ttft_ms is not None else "?"
        return f"in {input_text} · out {output_text} · ttft {ttft_text}"
