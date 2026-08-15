from __future__ import annotations

import json
import os
from pathlib import Path


class WorkspaceLockError(RuntimeError):
    pass


class WorkspaceLock:
    def __init__(self, workspace: str | Path, state_dir: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.path = Path(state_dir).expanduser() / "workspace.lock"
        self.held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkspaceLockError(f"WORKSPACE_LOCKED: {self.path}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "workspace": str(self.workspace)}, stream)
        self.held = True

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
