from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path


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


AGENT_MODES: tuple[str, ...] = ("Build", "Plan", "Review")

# Modes are a capability boundary, not a label.  Plan and Review deliberately
# cannot mutate the workspace, so switching into them is a real guarantee rather
# than a hint to the model that it should behave.
READ_ONLY_MODES = frozenset({"Plan", "Review"})
MUTATING_TOOLS = frozenset({"edit", "exec"})


@dataclass
class Policy:
    mode: ApprovalMode = ApprovalMode.SMART
    agent_mode: str = "Build"
    protected_names: tuple[str, ...] = (
        ".env", ".env.*", ".git", "*.pem", "*.key", "*.p12", "*.pfx",
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".ssh", ".aws", ".gnupg",
        ".npmrc", ".pypirc", ".netrc", "credentials", "*.keystore",
    )

    def __post_init__(self) -> None:
        # A typo used to be silently permissive: read_only tests membership in
        # READ_ONLY_MODES, so Policy(agent_mode="Reveiw") granted edit and exec.
        if self.agent_mode not in AGENT_MODES:
            raise PolicyError(f"UNKNOWN_AGENT_MODE: {self.agent_mode}")
        # Accept a string anywhere a mode is expected and normalise it once.
        # Three call sites read ``policy.mode.value``; one wrote a bare string,
        # and the AttributeError surfaced on the UI thread and killed the
        # session.  Coercing here means no caller has to remember.
        self.mode = ApprovalMode(self.mode) if not isinstance(self.mode, ApprovalMode) else self.mode

    def set_mode(self, value: "ApprovalMode | str") -> "ApprovalMode":
        """Set the approval mode from a mode or its name, validating it."""
        self.mode = value if isinstance(value, ApprovalMode) else ApprovalMode(str(value).strip().lower())
        return self.mode

    @property
    def read_only(self) -> bool:
        return self.agent_mode in READ_ONLY_MODES

    def allows(self, tool: str) -> bool:
        """Whether the current agent mode permits this tool at all."""
        return not (self.read_only and tool in MUTATING_TOOLS)

    def risk_for(self, tool: str, *, write: bool = False, destructive: bool = False) -> Risk:
        if destructive:
            return Risk.CRITICAL
        if tool == "exec":
            return Risk.MEDIUM
        if write:
            return Risk.MEDIUM
        return Risk.LOW

    def requires_approval(self, risk: Risk) -> bool:
        """Whether this risk has to be put to the user.

        ``auto`` means "do not ask me about the ordinary things".  It does not
        mean "run anything" — HIGH is reserved for a command this code could not
        recognise, and the right answer to "I do not know what this is" is the
        same in every mode: ask once.  Otherwise every gap in the tool's
        knowledge would fail open in exactly the mode people leave it in.
        """
        if risk in (Risk.CRITICAL, Risk.HIGH):
            return True
        if self.mode == ApprovalMode.ASK:
            return True
        if self.mode == ApprovalMode.AUTO:
            return False
        return risk == Risk.MEDIUM


class WorkspaceGuard:
    def __init__(self, root: str | Path) -> None:
        # Keep both spellings.  ``root`` is canonical containment authority;
        # ``lexical_root`` preserves names before ancestor symlinks such as
        # macOS /var -> /private/var erase them.
        self.lexical_root = Path(root).expanduser().absolute()
        self.root = self.lexical_root.resolve()
        if not self.root.is_dir():
            raise PolicyError(f"Workspace does not exist: {self.root}")

    def resolve(self, path: str | Path) -> Path:
        candidate = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PolicyError("PATH_OUTSIDE_WORKSPACE") from exc
        return candidate

    def check_name(self, path: str | Path, policy: Policy | None = None) -> None:
        """Reject any path component matching the policy's protected patterns.

        The patterns come from the policy rather than being hard-coded here so a
        caller can widen protection (extra secret files, vendored directories)
        without editing the guard.

        Matching is case-insensitive on both sides.  ``fnmatch`` is
        case-sensitive on POSIX, but macOS volumes are case-*insensitive* by
        default: ``read(".ENV")`` matched no pattern and then opened the real
        ``.env``.  A protected-name list that a change of case walks through is
        not a protection, and being case-insensitive on Linux too only ever
        refuses more.
        """
        supplied = Path(path).expanduser()
        lexical = supplied if supplied.is_absolute() else self.lexical_root / supplied
        # Normalise both sides.  On macOS /var is a symlink to /private/var;
        # resolving only the root made an ordinary TemporaryDirectory child look
        # outside its own workspace.
        try:
            lexical_relative = lexical.resolve(strict=False).relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            # Callers catch PolicyError; a bare ValueError from an
            # out-of-workspace path walked straight past every handler.
            raise PolicyError("PATH_OUTSIDE_WORKSPACE") from exc
        patterns = [pattern.lower() for pattern in (policy or Policy()).protected_names]

        # Check the path the caller actually named as well as the canonical
        # target.  Resolving first erased a protected lexical alias such as
        # ``.env -> public.txt`` and allowed the secret-named path through.
        try:
            lexical_parts = lexical.absolute().relative_to(self.lexical_root).parts
        except ValueError:
            # Accept an absolute path spelled through an ancestor symlink only
            # after canonical containment succeeded; preserve its tail by
            # comparing it with the canonical relative path.
            lexical_parts = lexical_relative.parts
        for part in (*lexical_parts, *lexical_relative.parts):
            lowered = part.lower()
            if any(fnmatch(lowered, pattern) for pattern in patterns):
                raise PolicyError(f"PROTECTED_PATH: {lexical_relative}")
