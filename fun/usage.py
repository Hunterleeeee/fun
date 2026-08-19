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

    def restore(self, snapshot: dict[str, object]) -> None:
        """Adopt a cumulative snapshot as-is.

        ``merge_provider`` accumulates, which is right for a provider's per-call
        report and wrong for a stored total; replay needs assignment.
        """
        for field_name in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "ttft_ms", "generation_ms"):
            value = snapshot.get(field_name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, field_name, value)
        tool_ms = snapshot.get("tool_ms")
        if isinstance(tool_ms, int) and not isinstance(tool_ms, bool):
            self.tool_ms = tool_ms
        precision = snapshot.get("precision")
        if isinstance(precision, str) and precision:
            self.precision = precision

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
        """A compact usage line, omitting anything not measured yet.

        Printing "in ? out ? ttft ?" before the first response is noise dressed
        up as data; an empty string lets the caller drop the segment entirely.
        """
        parts = []
        if self.input_tokens is not None:
            parts.append(f"in {self.input_tokens}")
        if self.output_tokens is not None:
            parts.append(f"out {self.output_tokens}")
        if self.ttft_ms is not None:
            parts.append(f"ttft {self.ttft_ms}ms")
        return " · ".join(parts)
