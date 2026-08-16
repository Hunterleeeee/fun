from __future__ import annotations

import difflib
import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
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
    MAX_OUTPUT = 256 * 1024

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
        safe_env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        safe_env["PWD"] = str(self.guard.root)
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return ToolResult(False, f"INVALID_COMMAND: {exc}", risk)
        if not argv:
            return ToolResult(False, "INVALID_COMMAND: empty command", risk)
        if Path(argv[0]).name in {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"} and len(argv) >= 3 and argv[1] in {"-c", "/c"}:
            return ToolResult(False, "CRITICAL_WRAPPER_BLOCKED", Risk.CRITICAL)
        if Path(argv[0]).name in {"python", "python3", "python.exe", "python3.exe"} and "-c" in argv[1:]:
            code = argv[argv.index("-c") + 1]
            if any(token in code.lower() for token in ("os.remove", "shutil.rmtree", "subprocess", "unlink", "rmtree")):
                return ToolResult(False, "CRITICAL_SCRIPT_BLOCKED", Risk.CRITICAL)
        if argv[0] == "python3" and sys.platform == "win32":
            argv[0] = sys.executable
        executable = Path(argv[0]).name
        wrapped = argv[1:] if executable in {"env", "command", "xargs"} else argv
        if executable in {"env", "command"}:
            while wrapped and "=" in wrapped[0] and not wrapped[0].startswith("-"):
                wrapped = wrapped[1:]
            executable = Path(wrapped[0]).name if wrapped else executable
        critical = executable in {"sudo", "curl", "wget"}
        critical = critical or (executable == "rm" and any(flag in {"-r", "-R", "-rf", "-fr", "--recursive", "--force"} or flag.startswith("-") and "r" in flag and "f" in flag for flag in wrapped[1:]))
        critical = critical or (executable == "git" and len(wrapped) >= 3 and wrapped[1:3] == ["reset", "--hard"])
        critical = critical or (executable == "git" and len(wrapped) >= 2 and wrapped[1] == "clean")
        if critical:
            if self.policy.mode != self.policy.mode.ASK:
                return ToolResult(False, "CRITICAL_OPERATION_BLOCKED", Risk.CRITICAL)
            return ToolResult(False, "APPROVAL_REQUIRED", Risk.CRITICAL)
        popen_options = {
            "cwd": self.guard.root,
            "shell": False,
            "text": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": safe_env,
        }
        if sys.platform == "win32":
            popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            popen_options["start_new_session"] = True
        try:
            completed = subprocess.Popen(argv, **popen_options)
            try:
                stdout, stderr = completed.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    completed.kill()
                else:
                    os.killpg(completed.pid, signal.SIGKILL)
                stdout, stderr = completed.communicate()
                output = (stdout + stderr).strip()[: self.MAX_OUTPUT]
                return ToolResult(False, f"COMMAND_TIMEOUT\n{output}".strip(), risk, exit_code=None)
        except OSError as exc:
            return ToolResult(False, f"EXEC_FAILED: {exc}", risk)
        output = (stdout + stderr).strip()
        truncated = len(output) > self.MAX_OUTPUT
        output = output[: self.MAX_OUTPUT]
        if truncated:
            output += "\n[OUTPUT_TRUNCATED]"
        return ToolResult(completed.returncode == 0, output, risk, exit_code=completed.returncode)
