"""Deterministic rule engine — regex-based code pattern detection.

Learning focus:
  - Rule design: pattern + severity + message + suggestion
  - Only checking ADDED lines (not context or deleted)
  - Line-number tracking as we scan
  - False-positive reduction via pattern specificity

Why deterministic rules?
  LLMs are good at semantic analysis but bad at reliable pattern matching.
  A hardcoded-secret or SQL-injection pattern is better caught by regex:
  - Fast (no LLM call needed)
  - Deterministic (same input → same output)
  - High confidence (regex match = high precision)
  The LLM then focuses on logic, architecture, and subtle issues.
"""

from __future__ import annotations

import re

from cr_agent.core.diff_parser import DiffHunk
from cr_agent.core.models import Confidence, Finding, Severity


# Each rule is a dict with:
#   rule_id: unique identifier (category.specific-issue)
#   pattern: regex to match against added line content (without '+' prefix)
#   severity: Severity level
#   message: human-readable description
#   suggestion: actionable fix
DETERMINISTIC_RULES: list[dict] = [
    {
        "rule_id": "security.hardcoded-secret",
        "pattern": r"(?:password|secret|api_key|apikey|token|access_key)\s*=\s*['\"][^'\"]{8,}",
        "severity": Severity.BLOCKER,
        "message": "Potential hardcoded secret detected.",
        "suggestion": "Use environment variables or a secret manager. Never commit credentials.",
    },
    {
        "rule_id": "security.sql-injection",
        "pattern": r"(?:execute|query|cursor\.execute)\s*\(\s*f['\"][^'\"]*\{.*\}",
        "severity": Severity.BLOCKER,
        "message": "Potential SQL injection via f-string query construction.",
        "suggestion": "Use parameterized queries: cursor.execute('SELECT ... WHERE id = %s', (id,))",
    },
    {
        "rule_id": "security.command-injection",
        "pattern": r"(?:os\.system|subprocess\.(?:call|run|Popen)|eval|exec)\s*\(\s*f['\"]",
        "severity": Severity.BLOCKER,
        "message": "Potential command injection via f-string in exec/system call.",
        "suggestion": "Use shell=False with argument lists; never interpolate untrusted input.",
    },
    {
        "rule_id": "security.eval-usage",
        "pattern": r"\beval\s*\(",
        "severity": Severity.BLOCKER,
        "message": "Use of eval() is dangerous and can execute arbitrary code.",
        "suggestion": "Use ast.literal_eval() for safe literal parsing, or restructure the code.",
    },
    {
        "rule_id": "debug.print-statement",
        "pattern": r"^\s*(print|console\.log)\s*\(",
        "severity": Severity.MINOR,
        "message": "Debug print statement left in code.",
        "suggestion": "Remove debug print or use a proper logging framework (logging.getLogger).",
    },
    {
        "rule_id": "debug.breakpoint",
        "pattern": r"^\s*(breakpoint|pdb\.set_trace|import pdb)\b",
        "severity": Severity.MAJOR,
        "message": "Debugger breakpoint left in code.",
        "suggestion": "Remove the breakpoint before merging.",
    },
    {
        "rule_id": "maintainability.todo-comment",
        "pattern": r"#\s*(TODO|FIXME|HACK|XXX)\b",
        "severity": Severity.INFO,
        "message": "Unresolved TODO/FIXME comment.",
        "suggestion": "Track this in an issue tracker or resolve before merging.",
    },
    {
        "rule_id": "security.weak-hash",
        "pattern": r"hashlib\.(md5|sha1)\s*\(",
        "severity": Severity.MAJOR,
        "message": "Weak hash function (MD5/SHA1) is cryptographically broken.",
        "suggestion": "Use hashlib.sha256 or stronger for security-sensitive contexts.",
    },
]


def run_deterministic_checks(hunks: list[DiffHunk]) -> list[Finding]:
    """Run all deterministic rules against added lines in the diff.

    Only checks lines that were ADDED (start with '+'), not context or deleted
    lines. This prevents flagging issues in code that's being removed.

    Line numbers are tracked by incrementing from new_start for each added or
    context line (matching git diff line numbering).

    Args:
        hunks: Parsed diff hunks from parse_diff().

    Returns:
        List of Finding objects, sorted by severity (blocker first).
    """
    findings: list[Finding] = []

    for hunk in hunks:
        line_number = hunk.new_start
        for raw_line in hunk.lines:
            # Skip file headers
            if raw_line.startswith("+++") or raw_line.startswith("---"):
                continue
            # Only check added lines
            if not raw_line.startswith("+"):
                # Context or deleted line — still increment line number for context lines
                if raw_line.startswith(" ") and line_number is not None:
                    line_number += 1
                continue

            # This is an added line
            content = raw_line[1:]  # Strip the '+' prefix

            # Skip empty added lines but still increment
            if not content.strip():
                if line_number is not None:
                    line_number += 1
                continue

            # Run all rules against this line
            for rule in DETERMINISTIC_RULES:
                if re.search(rule["pattern"], content, re.IGNORECASE):
                    findings.append(
                        Finding(
                            rule_id=rule["rule_id"],
                            severity=rule["severity"],
                            file=hunk.file,
                            line=line_number,
                            message=rule["message"],
                            suggestion=rule["suggestion"],
                            confidence=Confidence.HIGH,
                            source="deterministic",
                        )
                    )

            if line_number is not None:
                line_number += 1

    # Sort by severity (blocker first)
    severity_order = {Severity.BLOCKER: 0, Severity.MAJOR: 1, Severity.MINOR: 2, Severity.INFO: 3}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 99), f.file or "", f.line or 0))
    return findings
