from __future__ import annotations

import difflib
import math
import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable

from .policy import Policy, PolicyError, Risk, WorkspaceGuard
from .schema import MAX_EXEC_TIMEOUT


@dataclass
class ToolResult:
    ok: bool
    text: str
    risk: Risk = Risk.LOW
    changed: list[str] | None = None
    exit_code: int | None = None


# --------------------------------------------------------------- exec analysis
#
# Two rounds of this check were bypassed, and both times for the same reason:
# it was a *denylist*.  The first asked "is argv[1] exactly -c?" and lost to
# ``bash -lc``.  The second refused a named set of programs and lost to ``awk
# 'BEGIN{system("id")}'`` — because "programs that can run another program" is
# an unbounded set, and every one I forget fails open.
#
# So the default is inverted.  A command is auto-runnable only if its resolved
# program is on a short, explicit list of things that do not launch other
# programs.  Everything else is CRITICAL: it goes to the approval gate rather
# than running silently.  Forgetting a program now costs a prompt, not a shell.
#
# The denylists below are still worth keeping — a shell should be refused
# outright rather than offered for approval — but they are no longer what makes
# this safe.
#
# Honest limit: this reasons about argv only.  A path a program builds for
# itself is invisible here.  exec is a supervised capability, not a sandbox.

SHELLS = frozenset({
    "bash", "sh", "dash", "ash", "zsh", "ksh", "csh", "tcsh", "fish",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
})

# Programs whose entire purpose is to run another program the caller names.
# Blocking them is what stops the wrapper check from being a flag-parsing game.
INDIRECTION = frozenset({
    "xargs", "eval", "exec", "source", "watch", "script", "expect",
    "parallel", "rush", "ssh", "scp", "docker", "kubectl", "podman", "nsenter",
    # Text processors with a system()/shell escape.  awk is the one that got
    # through the previous round.
    "awk", "gawk", "mawk", "nawk", "busybox",
    # Editors, debuggers and pagers all shell out.
    "vi", "vim", "nvim", "emacs", "nano", "ed", "less", "more", "man",
    "gdb", "lldb", "strace", "ltrace",
    # Archive tools with an exec hook (tar --checkpoint-action=exec=...).
    "tar", "cpio", "rsync", "zip", "unzip",
    # These take a positional value before the command (a duration, a priority
    # class), or create a new namespace and then run something.
    "timeout", "chrt", "taskset", "ionice", "flock", "unshare", "setarch",
    "runuser", "su", "pkexec",
})

# Runners that execute project-controlled scripts; these were already refused.
SCRIPT_RUNNERS = frozenset({
    "npm", "npm.cmd", "npx", "npx.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd",
    "make", "gmake", "just", "node", "node.exe", "deno", "bun", "ruby", "perl", "php",
})

# Wrappers that are *transparent*: they run the rest of argv unchanged, so the
# real command is further along and each layer must be re-examined.
TRANSPARENT = frozenset({"env", "command", "nohup", "nice", "setsid", "stdbuf", "time"})

# What this code can and cannot decide.
#
# It cannot decide "does this program execute arbitrary code".  The previous
# attempt tried, with a hand-written list of build and test tools, and that list
# had no criterion behind it: `pytest` runs whatever is in the repo, `pip` runs
# setup.py, `gcc` compiles and can be told to run.  A list with no rule is a list
# that will be wrong again next time.
#
# It *can* decide two things from argv alone:
#
#   * whether the command only reads and reports — a short, closed set, each
#     entry checkable by hand, and every path argument still checked for escape;
#   * whether the command is one of a small number of *irreversible* operations —
#     deleting recursively, discarding work, escalating privilege, fetching and
#     running from the network.
#
# So those are the two things it decides, and everything else is admitted to be
# unknown.  An unknown program is asked about once, in every approval mode, and
# may then be remembered for the session.  An irreversible one is asked about
# every time and never remembered.  The gap between "I know this is safe" and "I
# know this is dangerous" is where the prompt belongs, not where a guess does.

BENIGN = frozenset({
    "ls", "cat", "head", "tail", "wc", "echo", "printf", "pwd", "date", "seq",
    "basename", "dirname", "realpath", "stat", "file", "du", "df", "tree",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "diff", "cmp", "comm",
    "sort", "uniq", "cut", "paste", "tr", "rev", "fold", "column", "jq", "yq",
    "md5sum", "sha1sum", "sha256sum", "shasum", "cksum", "xxd", "od", "strings",
    "which", "whoami", "uname", "hostname", "true", "false", "sleep",
})

# Flags of the transparent wrappers that consume the following argument.  Being
# wrong here means mistaking an option's value for the command name, so the
# unknown-flag case is treated as unresolvable rather than guessed at.
TRANSPARENT_VALUE_FLAGS = {"-u", "--unset", "-n", "--adjustment", "-o", "-e", "-w"}

# Flags on a transparent wrapper that move the child somewhere else.  Consuming
# them silently is what let ``env -C / cat etc/hostname`` read outside the
# workspace: the path became an option value and never reached the path check.
RELOCATING_FLAGS = ("-C", "--chdir", "-i", "--ignore-environment", "--default-signal")

# A program name is not a number, not a bare duration, and not an operator.
# Anything else after a wrapper means this code does not understand the
# wrapper's grammar, and guessing is what the old check did.
PROGRAM_NAME = re.compile(r"^[A-Za-z0-9_.+/\\:@-]*[A-Za-z_][A-Za-z0-9_.+/\\:@-]*$")

SKIP_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".tox"})

PYTHON_NAMES = frozenset({"python", "python3", "python.exe", "python3.exe", "py", "py.exe"})
PYTHON_CODE_FLAGS = frozenset({"-c", "-m"})


def _program(token: str) -> str:
    """The bare program name of an argv token, case-folded."""
    return PurePath(token.replace("\\", "/")).name.lower()


def resolve_command(argv: list[str]) -> tuple[str, list[str], str]:
    """Peel wrapper layers off ``argv``.

    Returns ``(program, remaining_argv, refusal)``.  ``refusal`` is empty when
    the command resolved to something runnable; otherwise it is the reason to
    report, and the caller must not run anything.  Every layer is checked as it
    is peeled, so ``env sudo rm -rf /`` is refused at the ``sudo`` layer rather
    than being unwrapped into invisibility.
    """
    remaining = list(argv)
    for _ in range(8):  # a wrapper chain deeper than this is not a real command
        if not remaining:
            return "", [], "INVALID_COMMAND: empty command"
        program = _program(remaining[0])
        if program in SHELLS:
            return program, remaining, "CRITICAL_WRAPPER_BLOCKED"
        if program in INDIRECTION or program in SCRIPT_RUNNERS:
            return program, remaining, "CRITICAL_WRAPPER_BLOCKED"
        if program not in TRANSPARENT:
            return program, remaining, ""
        rest = remaining[1:]
        while rest:
            token = rest[0]
            if any(token == flag or token.startswith(f"{flag}=") for flag in RELOCATING_FLAGS):
                return program, remaining, "CRITICAL_WRAPPER_BLOCKED"
            if "=" in token and not token.startswith("-"):
                rest = rest[1:]          # env VAR=value
            elif token in TRANSPARENT_VALUE_FLAGS:
                rest = rest[2:]          # flag plus its value
            elif token.startswith("-"):
                rest = rest[1:]          # a valueless flag
            else:
                break
        if not rest or not PROGRAM_NAME.match(rest[0]) or _program(rest[0]) in {"", "."}:
            # The wrapper had no command after it, or the next token is not a
            # program name (a duration, a priority) — meaning this code does not
            # model that wrapper.  Refusing beats guessing which token runs.
            return program, remaining, "CRITICAL_WRAPPER_BLOCKED"
        remaining = rest
    return _program(remaining[0]), remaining, "CRITICAL_WRAPPER_BLOCKED"


def _rm_is_recursive_force(flags: list[str]) -> bool:
    """Whether these ``rm`` flags mean recursive and/or forced.

    Case-folded: ``rm`` accepts ``-R`` as well as ``-r``, and the combined form
    ``-Rf`` walked past a case-sensitive membership test and deleted the
    directory.
    """
    for raw in flags:
        flag = raw.lower()
        if flag in {"-r", "-rf", "-fr", "--recursive", "--force"}:
            return True
        if flag.startswith("--"):
            continue
        if flag.startswith("-") and ("r" in flag[1:] or "f" in flag[1:]):
            return True
    return False


def is_critical(program: str, argv: list[str]) -> bool:
    """Whether this resolved command needs the critical-operation gate."""
    tail = argv[1:]
    if program in {"sudo", "su", "curl", "wget", "nc", "ncat", "telnet", "chown", "chmod", "dd", "mkfs", "shutdown", "reboot"}:
        return True
    if program == "rm" and _rm_is_recursive_force(tail):
        return True
    if program == "find" and any(flag in {"-exec", "-execdir", "-delete", "-ok", "-okdir"} for flag in tail):
        return True
    if program == "git" and tail[:2] == ["reset", "--hard"]:
        return True
    if program == "git" and tail[:1] == ["clean"]:
        return True
    if program == "git" and tail[:1] == ["push"] and any(flag in {"-f", "--force", "--force-with-lease"} for flag in tail):
        return True
    return False


@dataclass(frozen=True)
class CommandPlan:
    """What running this command would mean, decided before anything runs."""

    argv: list[str]
    program: str
    risk: Risk
    refusal: str = ""


def classify_command(command: str, root: Path) -> CommandPlan:
    """Resolve ``command`` and decide what gate it belongs behind.

    One place produces this, and both the Runtime's approval gate and the tool
    itself read it, so the risk the user is asked to approve is the risk that
    was actually assessed.  Previously the Runtime asked about a flat "medium"
    for every exec and the tool then refused the dangerous ones anyway — the
    user could approve a command and still be told APPROVAL_REQUIRED.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return CommandPlan([], "", Risk.MEDIUM, f"INVALID_COMMAND: {exc}")
    if not argv:
        return CommandPlan([], "", Risk.MEDIUM, "INVALID_COMMAND: empty command")
    program, resolved, refusal = resolve_command(argv)
    if refusal:
        return CommandPlan(argv, program, Risk.CRITICAL, refusal)
    if program in PYTHON_NAMES and any(flag in PYTHON_CODE_FLAGS for flag in resolved[1:]):
        return CommandPlan(argv, program, Risk.CRITICAL, "CRITICAL_SCRIPT_BLOCKED")
    if is_critical(program, resolved) or escapes_workspace(root, resolved):
        return CommandPlan(argv, program, Risk.CRITICAL)
    if program in BENIGN:
        return CommandPlan(argv, program, Risk.LOW)
    # Unknown, not unsafe.  HIGH asks once in every mode and can be remembered
    # for the session; CRITICAL is reserved for what is provably irreversible.
    return CommandPlan(argv, program, Risk.HIGH)


def escapes_workspace(root: Path, argv: list[str]) -> bool:
    """Whether any argument obviously points outside the workspace.

    A heuristic on purpose, and only ever used to *raise* the gate: an argument
    that looks like a path and resolves outside the root turns the call into an
    approval, it does not silently allow anything.  It cannot see paths built
    inside the program it launches, which is why exec stays supervised.
    """
    for token in argv[1:]:
        if token.startswith("-") or not token:
            continue
        if not (token.startswith(("/", "~", "./", "../")) or "/" in token):
            continue
        try:
            candidate = Path(token).expanduser()
            resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        except (OSError, RuntimeError, ValueError):
            return True
        if resolved != root and root not in resolved.parents:
            return True
    return False


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
        """List the workspace, lazily and without naming protected files.

        ``read`` refuses ``.env`` and private keys, but ``explore`` listed them,
        and a filename is itself information — which keys exist, which
        environments are configured.  It also enumerated and sorted the *entire*
        tree before applying ``limit``, so a small listing of a large repository
        cost minutes.  This walks level by level, prunes as it goes, and stops
        at the limit.
        """
        root = self.guard.resolve(path)
        rows: list[str] = []
        truncated = False
        stack = [root]
        while stack:
            current = stack.pop(0)
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            children: list[Path] = []
            for item in entries:
                if item.name in SKIP_DIRECTORIES:
                    continue
                try:
                    self.guard.check_name(item, self.policy)
                except PolicyError:
                    continue
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append(str(item.relative_to(self.guard.root)))
                if item.is_dir() and not item.is_symlink():
                    children.append(item)
            if truncated:
                break
            stack.extend(children)
        text = "\n".join(rows)
        if truncated:
            text += f"\n[LISTING_TRUNCATED at {limit} entries]"
        return ToolResult(True, text)

    def read(self, path: str, start: int = 1, end: int | None = None) -> ToolResult:
        # Check the lexical name before resolve() follows symlinks; otherwise a
        # protected alias such as `.env -> public.txt` is checked only under the
        # harmless target name.
        self.guard.check_name(path, self.policy)
        target = self.guard.resolve(path)
        self.guard.check_name(target, self.policy)
        if not target.is_file():
            return ToolResult(False, f"Not a file: {path}")
        lines = target.read_text(encoding="utf-8").splitlines()
        selected = lines[max(0, start - 1) : end]
        text = "\n".join(f"{index + start:>5} | {line}" for index, line in enumerate(selected))
        return ToolResult(True, text)

    def edit(self, path: str, expected_hash: str, patch: str) -> ToolResult:
        self.guard.check_name(path, self.policy)
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

    def exec(self, command: str, timeout: float = 120.0, on_progress: Callable[[float], None] | None = None, approved: bool = False) -> ToolResult:
        """Run a command in the workspace.

        ``approved`` says the Runtime already put this exact call through the
        approval gate at the risk :func:`classify_command` assessed, so a
        critical command the user allowed actually runs instead of being refused
        a second time by this function.
        """
        # Belt and braces: the schema bounds what the model can ask for, and
        # this bounds what any caller can pass.  An infinite deadline turns the
        # poll loop into a spin that never returns.
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            return ToolResult(False, "INVALID_TIMEOUT", Risk.MEDIUM)
        timeout = min(float(timeout), MAX_EXEC_TIMEOUT)
        risk = self.policy.risk_for("exec")
        safe_env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        safe_env["PWD"] = str(self.guard.root)
        python_dir = str(Path(sys.executable).parent)
        if python_dir not in safe_env.get("PATH", "").split(os.pathsep):
            safe_env["PATH"] = python_dir + os.pathsep + safe_env.get("PATH", "")
        plan = classify_command(command, self.guard.root)
        if plan.refusal:
            return ToolResult(False, plan.refusal, plan.risk)
        argv = list(plan.argv)
        if argv[0] == "python3" and sys.platform == "win32":
            argv[0] = sys.executable
        if not approved and self.policy.requires_approval(plan.risk):
            # Defence in depth.  run_tool is the gate, but Tools.exec is a
            # public entry point too, and it must not be the looser of the two.
            if plan.risk == Risk.CRITICAL and self.policy.mode != self.policy.mode.ASK:
                return ToolResult(False, "CRITICAL_OPERATION_BLOCKED", plan.risk)
            return ToolResult(False, "APPROVAL_REQUIRED", plan.risk)
        risk = plan.risk
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
                if timeout < 1.0:
                    stdout, stderr = completed.communicate(timeout=timeout)
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        remaining = max(0.0, deadline - time.monotonic())
                        try:
                            stdout, stderr = completed.communicate(timeout=min(1.0, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            if on_progress is not None:
                                on_progress(time.monotonic() - (deadline - timeout))
                            if remaining <= 0:
                                raise
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
