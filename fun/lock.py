from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class WorkspaceLockError(RuntimeError):
    pass


class WorkspaceLock:
    """Mutual exclusion for one workspace, not for the whole state directory.

    The lock file used to be a single ``workspace.lock`` per state dir — and
    the state dir defaults to ``~/.fun`` for everyone — so opening a second
    project in a second terminal was refused with a message naming the *first*
    project's path.  Naming the file after the workspace keeps the exclusion
    that matters (two sessions editing one tree) and drops the one that never
    did (two sessions editing different trees).
    """

    def __init__(self, workspace: str | Path, state_dir: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        digest = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:16]
        self.path = Path(state_dir).expanduser() / f"workspace-{digest}.lock"
        self.held = False

    def acquire(self, attempts: int = 3) -> None:
        """Take the lock, reclaiming it only from a process that is gone.

        Two processes can find the same lock stale at the same moment, both
        unlink it, and both try to recreate it.  The retry used to sit *inside*
        the ``except FileExistsError`` handler, so the loser's ``os.open`` raised
        a bare ``FileExistsError`` that callers catching ``WorkspaceLockError``
        could not handle — and the winner could not tell it had won.  Retrying
        from the top means whoever loses simply sees a fresh live lock and is
        refused, which is the correct answer.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(max(1, attempts)):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                if not self._stale():
                    raise WorkspaceLockError(f"WORKSPACE_LOCKED: {self.path}") from exc
                if attempt + 1 == attempts:
                    raise WorkspaceLockError(f"WORKSPACE_LOCK_CONTENDED: {self.path}") from exc
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "workspace": str(self.workspace)}, stream)
            self.held = True
            return

    def _stale(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
            return not _pid_is_alive(pid)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return True

    def adopt_if_owned(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("pid", 0)) == os.getpid():
                self.held = True
                return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return False

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False

    def __enter__(self) -> "WorkspaceLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
