"""Unified diff parser — extracts structured hunks from raw diff text.

Learning focus:
  - Text parsing with regex (unified diff format)
  - Line-number tracking (old_start / new_start)
  - Handling edge cases (dev/null, no newline, binary files)

The unified diff format:
  --- a/file.py          (old file header)
  +++ b/file.py          (new file header)
  @@ -old_start,old_len +new_start,new_len @@  (hunk header)
   context line          (leading space)
  -removed line          (leading -)
  +added line            (leading +)
  \\ No newline at end    (metadata, skip)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@",
    re.MULTILINE,
)


@dataclass
class DiffHunk:
    """One hunk from a unified diff."""

    file: str | None
    old_start: int | None
    new_start: int | None
    lines: list[str] = field(default_factory=list)

    @property
    def added_lines(self) -> list[str]:
        """Lines that were added (start with '+', not '+++')."""
        return [l[1:] for l in self.lines if l.startswith("+") and not l.startswith("+++")]

    @property
    def removed_lines(self) -> list[str]:
        """Lines that were removed (start with '-', not '---')."""
        return [l[1:] for l in self.lines if l.startswith("-") and not l.startswith("---")]


def parse_diff(diff_text: str) -> list[DiffHunk]:
    """Parse a unified diff string into structured hunks.

    Args:
        diff_text: Raw unified diff output (e.g. from `git diff` or `gh pr diff`).

    Returns:
        List of DiffHunk objects, each containing file path, line numbers, and
        the raw lines (with +/-/space prefixes preserved).
    """
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    current_hunk: DiffHunk | None = None

    for raw_line in diff_text.split("\n"):
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            if current_file in ("/dev/null", ""):
                current_file = None
        elif raw_line.startswith("+++ /dev/null"):
            # File deletion — no new file
            current_file = None
        elif raw_line.startswith("--- "):
            pass  # Old file header, we already track new file from +++
        elif raw_line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            match = HUNK_PATTERN.match(raw_line)
            current_hunk = DiffHunk(
                file=current_file,
                old_start=int(match.group("old_start")) if match else None,
                new_start=int(match.group("new_start")) if match else None,
            )
        elif current_hunk and (
            raw_line.startswith("+")
            or raw_line.startswith("-")
            or raw_line.startswith(" ")
        ):
            current_hunk.lines.append(raw_line)
        # Lines starting with "\" (no newline marker) or anything else are skipped

    if current_hunk:
        hunks.append(current_hunk)

    return hunks


def compute_metrics(hunks: list[DiffHunk]) -> tuple[int, int, int]:
    """Compute (files_changed, lines_added, lines_removed) from hunks."""
    files: set[str | None] = set()
    added = 0
    removed = 0
    for hunk in hunks:
        files.add(hunk.file)
        for line in hunk.lines:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return len(files), added, removed
