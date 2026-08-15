from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy, PolicyError, Risk, WorkspaceGuard


@dataclass
class ToolResult:
    ok: bool
    text: str
    risk: Risk = Risk.LOW
    changed: list[str] | None = None
    exit_code: int | None = None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _apply_unified_patch(old_text: str, patch: str) -> str | None:
    lines = old_text.splitlines(True)
    patch_lines = patch.splitlines(True)
    hunks = [index for index, line in enumerate(patch_lines) if line.startswith("@@")]
    if not hunks:
        return None
    output: list[str] = []
    source_index = 0
    for hunk_index, start in enumerate(hunks):
        header = patch_lines[start]
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
        if not match:
            return None
        target_start = int(match.group(1)) - 1
        if target_start < source_index or target_start > len(lines):
            return None
        output.extend(lines[source_index:target_start])
        end = hunks[hunk_index + 1] if hunk_index + 1 < len(hunks) else len(patch_lines)
        for line in patch_lines[start + 1:end]:
            if line.startswith(" "):
                expected = line[1:]
                if source_index >= len(lines) or lines[source_index] != expected:
                    return None
                output.append(lines[source_index])
                source_index += 1
            elif line.startswith("-"):
                expected = line[1:]
                if source_index >= len(lines) or lines[source_index] != expected:
                    return None
                source_index += 1
            elif line.startswith("+"):
                output.append(line[1:])
            elif line.startswith("\\"):
                continue
            else:
                return None
    output.extend(lines[source_index:])
    return "".join(output)


class Tools:
    def __init__(self, workspace: str | Path, policy: Policy | None = None) -> None:
        self.guard = WorkspaceGuard(workspace)
        self.policy = policy or Policy()

    def explore(self, path: str = ".", limit: int = 100) -> ToolResult:
        root = self.guard.resolve(path)
        rows: list[str] = []
        for item in sorted(root.rglob("*")):
            if any(part in {".git", "node_modules", ".venv", "__pycache__"} for part in item.parts):
                continue
            if len(rows) >= limit:
                break
            rows.append(str(item.relative_to(self.guard.root)))
        return ToolResult(True, "\n".join(rows))

    def read(self, path: str, start: int = 1, end: int | None = None) -> ToolResult:
        target = self.guard.resolve(path)
        self.guard.check_name(target, self.policy)
        if not target.is_file():
            return ToolResult(False, f"Not a file: {path}")
        lines = target.read_text(encoding="utf-8").splitlines()
        selected = lines[max(0, start - 1) : end]
        text = "\n".join(f"{index + start:>5} | {line}" for index, line in enumerate(selected))
        return ToolResult(True, text)

    def edit(self, path: str, expected_hash: str, patch: str) -> ToolResult:
        target = self.guard.resolve(path)
        self.guard.check_name(target, self.policy)
        if not target.exists():
            return ToolResult(False, f"File does not exist: {path}", Risk.MEDIUM)
        if file_hash(target) != expected_hash:
            return ToolResult(False, "FILE_CHANGED_SINCE_READ", Risk.MEDIUM)
        if self.policy.requires_approval(Risk.MEDIUM):
            return ToolResult(False, "APPROVAL_REQUIRED", Risk.MEDIUM)
        old_text = target.read_text(encoding="utf-8")
        new_text = _apply_unified_patch(old_text, patch)
        if new_text is None:
            return ToolResult(False, "PATCH_FAILED", Risk.MEDIUM)
        temporary = target.with_name(f".{target.name}.fun.tmp")
        temporary.write_text(new_text, encoding="utf-8")
        temporary.replace(target)
        diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True), fromfile=path, tofile=path))
        return ToolResult(True, diff, Risk.MEDIUM, changed=[path])

    def exec(self, command: str, timeout: float = 120.0) -> ToolResult:
        risk = self.policy.risk_for("exec")
        if self.policy.mode == self.policy.mode.AUTO and any(token in command for token in ("rm -rf", "git reset --hard", "git clean")):
            return ToolResult(False, "CRITICAL_OPERATION_BLOCKED", Risk.CRITICAL)
        completed = subprocess.run(
            command,
            cwd=self.guard.root,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "PWD": str(self.guard.root)},
        )
        output = (completed.stdout + completed.stderr).strip()
        return ToolResult(completed.returncode == 0, output, risk, exit_code=completed.returncode)
