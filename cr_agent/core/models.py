"""Data models for code review findings, reports, and verdicts.

Learning focus:
  - Pydantic v2 models for structured data
  - Severity hierarchy and verdict rules
  - Schema versioning for contract stability
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Finding severity levels, ordered by impact."""

    BLOCKER = "blocker"  # Must fix: security vuln, data loss, crash
    MAJOR = "major"      # Should fix: logic error, missing error handling
    MINOR = "minor"      # Optional: style, naming, maintainability
    INFO = "info"        # Observation, no action needed


class Verdict(str, Enum):
    """Overall review conclusion."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    BLOCK = "block"


class Confidence(str, Enum):
    """How confident the reviewer is in this finding."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    """A single code review finding."""

    rule_id: str
    severity: Severity
    file: str | None = None
    line: int | None = None
    message: str
    suggestion: str
    confidence: Confidence = Confidence.MEDIUM
    source: Literal["deterministic", "llm"] = "deterministic"

    def markdown(self) -> str:
        """Render as markdown for PR comment."""
        return (
            f"#### [{self.severity.value}] {self.file or 'unknown'}:{self.line or '?'}\n"
            f"**Issue**: {self.message}\n"
            f"**Suggestion**: {self.suggestion}\n"
            f"**Confidence**: {self.confidence.value}\n"
        )


class DiffMetrics(BaseModel):
    """Statistics about the reviewed diff."""

    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0


class StaticAnalysisResult(BaseModel):
    """Result of a single static analysis tool (lint, type-check)."""

    tool: str
    status: Literal["pass", "fail", "skipped"] = "skipped"
    issues: int = 0
    output: str | None = None


class ReviewReport(BaseModel):
    """Complete review report posted as a PR comment."""

    schema_version: str = "cr-agent.report.v1"
    verdict: Verdict
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    static_analysis: list[StaticAnalysisResult] = Field(default_factory=list)
    metrics: DiffMetrics = Field(default_factory=DiffMetrics)

    def to_markdown(self) -> str:
        """Render the full report as markdown."""
        lines = ["## Code Review Report\n", f"**Verdict**: {self.verdict.value}\n"]
        lines.append(f"### Summary\n{self.summary}\n")

        if self.findings:
            lines.append("### Findings\n")
            for f in self.findings:
                lines.append(f.markdown())
        else:
            lines.append("### Findings\nNo issues found.\n")

        if self.static_analysis:
            lines.append("### Static Analysis\n")
            lines.append("| Tool | Status | Issues |")
            lines.append("|------|--------|--------|")
            for sa in self.static_analysis:
                lines.append(f"| {sa.tool} | {sa.status} | {sa.issues} |")
            lines.append("")

        lines.append("### Metrics")
        lines.append(f"- Files reviewed: {self.metrics.files_changed}")
        lines.append(f"- Lines changed: +{self.metrics.lines_added} / -{self.metrics.lines_removed}")
        lines.append(f"- Findings: {len(self.findings)} total")
        return "\n".join(lines)


def determine_verdict(findings: list[Finding]) -> Verdict:
    """Determine the review verdict from findings.

    Learning focus: this is the decision logic that maps findings to actions.
    """
    has_blocker = any(f.severity == Severity.BLOCKER for f in findings)
    has_major = any(f.severity == Severity.MAJOR for f in findings)
    if has_blocker:
        return Verdict.BLOCK
    if has_major:
        return Verdict.REQUEST_CHANGES
    return Verdict.APPROVE
