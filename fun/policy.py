from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from enum import Enum


class ApprovalMode(str, Enum):
    ASK = "ask"
    SMART = "smart"
    AUTO = "auto"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyError(RuntimeError):
    pass


@dataclass
class Policy:
    mode: ApprovalMode = ApprovalMode.SMART
    protected_names: tuple[str, ...] = (".env", ".env.", ".git", "*.pem", "*.key")

    def risk_for(self, tool: str, *, write: bool = False, destructive: bool = False) -> Risk:
        if destructive:
            return Risk.CRITICAL
        if tool == "exec":
            return Risk.MEDIUM
        if write:
            return Risk.MEDIUM
        return Risk.LOW

    def requires_approval(self, risk: Risk) -> bool:
        if risk == Risk.CRITICAL:
            return True
        if self.mode == ApprovalMode.ASK:
            return True
        if self.mode == ApprovalMode.AUTO:
            return False
        return risk in (Risk.HIGH,)


class WorkspaceGuard:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise PolicyError(f"Workspace does not exist: {self.root}")

    def resolve(self, path: str | Path) -> Path:
        candidate = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PolicyError("PATH_OUTSIDE_WORKSPACE") from exc
        return candidate

    def check_name(self, path: Path, policy: Policy) -> None:
        relative = path.relative_to(self.root)
        for part in relative.parts:
            if part == ".git" or part.startswith(".env") or part.endswith((".pem", ".key")):
                raise PolicyError(f"PROTECTED_PATH: {relative}")
