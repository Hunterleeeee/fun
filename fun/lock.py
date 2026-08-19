from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
        self.guard_path = self.path.with_suffix(self.path.suffix + ".guard")
        self.owner = uuid.uuid4().hex
        self.held = False

    @contextmanager
    def _guard(self) -> Iterator[None]:
        """Serialise lock-file inspection and replacement across processes.

        The guard file is persistent; ownership is an OS advisory lock, so a
        process crash releases it automatically without a second stale-file
        protocol and without exposing half-written metadata.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.guard_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if stream.read(1) == b"":
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as exc:
                    raise WorkspaceLockError(f"WORKSPACE_LOCK_CONTENDED: {self.path}") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    raise WorkspaceLockError(f"WORKSPACE_LOCK_CONTENDED: {self.path}") from exc
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            stream.close()

    def _metadata(self) -> dict[str, object] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def acquire(self, attempts: int = 3) -> None:
        """Take the lock, reclaiming it only from a process that is gone.

        Inspection, stale removal and replacement are one guarded transaction.
        The metadata is written to a private temporary file and atomically linked
        into place, so another contender can never mistake a half-written live
        lock for stale.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._guard():
                if self.path.exists():
                    data = self._metadata()
                    # Invalid metadata is not proof that the owner is dead.  Fail
                    # closed rather than deleting a live lock observed mid-write.
                    if data is None:
                        raise WorkspaceLockError(f"WORKSPACE_LOCKED: {self.path}")
                    try:
                        pid = int(data.get("pid", 0))
                    except (ValueError, TypeError):
                        raise WorkspaceLockError(f"WORKSPACE_LOCKED: {self.path}")
                    if _pid_is_alive(pid):
                        raise WorkspaceLockError(f"WORKSPACE_LOCKED: {self.path}")
                    self.path.unlink()
                temporary = self.path.with_name(f".{self.path.name}.{self.owner}.tmp")
                try:
                    with temporary.open("x", encoding="utf-8") as stream:
                        json.dump({"pid": os.getpid(), "workspace": str(self.workspace), "owner": self.owner}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, self.path)
                except FileExistsError as exc:
                    raise WorkspaceLockError(f"WORKSPACE_LOCK_CONTENDED: {self.path}") from exc
                finally:
                    temporary.unlink(missing_ok=True)
                self.held = True
        except WorkspaceLockError:
            raise

    def adopt_if_owned(self) -> bool:
        data = self._metadata()
        if data is None:
            return False
        try:
            if int(data.get("pid", 0)) == os.getpid():
                # Older locks had no owner token.  Record a fresh token while
                # holding the guard so release can prove ownership later.
                with self._guard():
                    current = self._metadata()
                    if current != data:
                        return False
                    current["owner"] = self.owner
                    temporary = self.path.with_name(f".{self.path.name}.{self.owner}.tmp")
                    with temporary.open("x", encoding="utf-8") as stream:
                        json.dump(current, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                self.held = True
                return True
        except (OSError, ValueError, TypeError, WorkspaceLockError):
            return False
        return False

    def release(self) -> None:
        if not self.held:
            return
        try:
            with self._guard():
                data = self._metadata()
                if data is not None and data.get("owner") == self.owner:
                    self.path.unlink(missing_ok=True)
        except WorkspaceLockError:
            # Never delete without proving ownership; deleting a replacement
            # lock would violate mutual exclusion.
            return
        finally:
            self.held = False

    def __enter__(self) -> "WorkspaceLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
