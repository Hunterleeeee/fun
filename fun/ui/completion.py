"""Inline completion for slash commands and ``@`` file references.

Completion is driven entirely off the editor buffer and cursor, so it stays
correct when the cursor is in the middle of a line rather than at the end —
typing ``@src`` in front of existing text completes the ``@src`` token, not the
whole buffer.

The matcher is a small subsequence scorer in the spirit of fzf: any candidate
containing the query's characters in order is a match, ranked by how *tight*
and how *well-anchored* the match is.  It is deliberately forgiving — the cost
of an extra candidate is one row of screen, while the cost of missing the file
someone meant is that the feature feels broken.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRECTORIES = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".tox", ".idea", ".fun"}
MAX_INDEXED_FILES = 4000
WORD_BOUNDARIES = "/_-. "


@dataclass
class Candidate:
    value: str
    detail: str = ""
    score: int = 0


def score(query: str, candidate: str) -> int | None:
    """Rank ``candidate`` against ``query``, or return None if it does not match.

    Higher is better.  Consecutive characters, matches right after a word
    boundary, and a match at position zero all earn bonuses, so ``uis`` ranks
    ``fun/ui/state.py`` above ``fun/build/units.py``.
    """
    if not query:
        return 0
    needle, haystack = query.lower(), candidate.lower()
    total = 0
    index = 0
    previous = -2
    for char in needle:
        found = haystack.find(char, index)
        if found < 0:
            return None
        if found == previous + 1:
            total += 8  # consecutive run
        if found == 0:
            total += 12  # anchored at the start
        elif haystack[found - 1] in WORD_BOUNDARIES:
            total += 6  # start of a path segment or word
        previous = found
        index = found + 1
    # Prefer shorter candidates, and reward covering more of the candidate.
    total += max(0, 40 - len(candidate))
    if haystack.startswith(needle):
        total += 20
    return total


def rank(query: str, candidates: list[Candidate], limit: int = 8) -> list[Candidate]:
    """Score and sort candidates, keeping the best ``limit``."""
    scored: list[Candidate] = []
    for candidate in candidates:
        value = score(query, candidate.value)
        if value is None:
            continue
        scored.append(Candidate(candidate.value, candidate.detail, value))
    scored.sort(key=lambda item: (-item.score, len(item.value), item.value))
    return scored[:limit]


@dataclass
class Context:
    """Where in the buffer completion applies, and what is being completed."""

    kind: str  # "command" | "file"
    query: str
    start: int
    end: int


def detect(text: str, cursor: int) -> Context | None:
    """Find the completion context around ``cursor``, if any."""
    cursor = max(0, min(len(text), cursor))
    line_start = text.rfind("\n", 0, cursor) + 1
    prefix = text[line_start:cursor]

    # A slash command only completes as the very first token of the buffer, and
    # only with the cursor *inside* the command.  With the cursor at offset 0 —
    # after Home, or four Lefts — the span was [0, 0) and accepting a candidate
    # spliced a second command in front of the first ("/help /hel").
    if text.startswith("/") and line_start == 0 and cursor > 0 and " " not in prefix and "\n" not in prefix:
        end = cursor
        while end < len(text) and text[end] not in " \t\n":
            end += 1
        return Context("command", text[1:cursor], 0, end)

    start = cursor
    while start > line_start and text[start - 1] not in " \t":
        start -= 1
    token = text[start:cursor]
    if token.startswith("@"):
        return Context("file", token[1:], start, cursor)
    return None


class FileIndex:
    """A lazily built, bounded list of workspace-relative file paths."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._paths: list[str] | None = None

    def refresh(self) -> list[str]:
        paths: list[str] = []
        for directory, subdirectories, files in os.walk(self.root):
            subdirectories[:] = [name for name in subdirectories if name not in SKIP_DIRECTORIES and not name.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                full = Path(directory) / name
                try:
                    paths.append(str(full.relative_to(self.root)))
                except ValueError:
                    continue
                if len(paths) >= MAX_INDEXED_FILES:
                    self._paths = sorted(paths)
                    return self._paths
        self._paths = sorted(paths)
        return self._paths

    def paths(self) -> list[str]:
        if self._paths is None:
            try:
                return self.refresh()
            except OSError:
                self._paths = []
        return self._paths or []

    def invalidate(self) -> None:
        self._paths = None


@dataclass
class Completer:
    """Produces ranked candidates for a detected context."""

    commands: dict[str, str] = field(default_factory=dict)
    files: FileIndex | None = None
    limit: int = 8

    def candidates(self, context: Context) -> list[Candidate]:
        if context.kind == "command":
            pool = [Candidate(name, summary) for name, summary in self.commands.items()]
            return rank(context.query, pool, self.limit)
        if context.kind == "file" and self.files is not None:
            pool = [Candidate(path) for path in self.files.paths()]
            return rank(context.query, pool, self.limit)
        return []

    def apply(self, text: str, context: Context, choice: str) -> tuple[str, int]:
        """Splice ``choice`` into ``text``, returning the new text and cursor.

        A trailing space is added only when one is not already there, so
        completing a token that sits mid-sentence does not leave a double space
        behind it.
        """
        replacement = choice if context.kind == "command" else f"@{choice}"
        rest = text[context.end :]
        suffix = "" if rest[:1].isspace() else " "
        new_text = text[: context.start] + replacement + suffix + rest
        return new_text, context.start + len(replacement) + len(suffix)


@dataclass
class CompletionState:
    """The live popup: candidates plus which one is highlighted."""

    context: Context | None = None
    candidates: list[Candidate] = field(default_factory=list)
    index: int = 0

    @property
    def active(self) -> bool:
        return bool(self.context and self.candidates)

    def clear(self) -> None:
        self.context = None
        self.candidates = []
        self.index = 0

    def move(self, delta: int) -> None:
        if self.candidates:
            self.index = (self.index + delta) % len(self.candidates)

    def selected(self) -> str | None:
        if not self.active:
            return None
        return self.candidates[self.index].value

    def refresh(self, completer: Completer, text: str, cursor: int) -> None:
        context = detect(text, cursor)
        if context is None:
            self.clear()
            return
        candidates = completer.candidates(context)
        if not candidates:
            self.clear()
            return
        # Keep the highlight on the same entry while the list is being narrowed.
        previous = self.selected()
        self.context = context
        self.candidates = candidates
        self.index = next((i for i, item in enumerate(candidates) if item.value == previous), 0)
