"""Agent tools — the actions the LLM can choose to call.

Learning focus:
  - @tool decorator: how LangChain converts a function into a tool schema
  - Tool docstrings become the LLM's tool description (be clear and specific!)
  - Return types matter: the LLM sees the return value as a ToolMessage
  - Error handling: return error info, don't raise (agent can recover)

Tools are the agent's hands. The LLM decides which tool to call based on:
  1. The tool's name and docstring (this is what it "sees")
  2. The current context (what it already knows)
  3. What it needs to accomplish next

Key insight: The LLM doesn't "run code" — it emits a structured request to
call a tool, and our code executes it and returns the result.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from cr_agent.core.models import Finding, Severity, Confidence


@tool
def run_lint(command: str, cwd: str = ".") -> str:
    """Run a lint or type-check command and return the output.

    Use this to run project linters like:
    - Python: "ruff check ." or "mypy ."
    - JavaScript: "npx eslint ." or "npx tsc --noEmit"

    Args:
        command: The shell command to execute (e.g. "ruff check . --output-format=json").
        cwd: Working directory for the command (defaults to current directory).
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            return f"Lint passed (exit 0):\n{output[:2000]}"
        else:
            return f"Lint failed (exit {result.returncode}):\n{output[:2000]}"
    except subprocess.TimeoutExpired:
        return "Lint timed out after 30 seconds. Skipped."
    except FileNotFoundError:
        return f"Command not found: {command}. Skipped."
    except Exception as e:
        return f"Lint error: {type(e).__name__}: {e}. Skipped."


@tool
def read_file(path: str, max_lines: int = 200) -> str:
    """Read a file's content for full context beyond what the diff shows.

    Use this ONLY when the diff context is insufficient to understand a change.
    Do NOT read every changed file.

    Args:
        path: Path to the file to read.
        max_lines: Maximum lines to read (defaults to 200 to avoid token explosion).
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        content = p.read_text(errors="replace")
        lines = content.split("\n")[:max_lines]
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading {path}: {type(e).__name__}: {e}"


@tool
def generate_report(
    verdict: str,
    summary: str,
    findings: str,
    files_reviewed: int = 0,
    lines_added: int = 0,
    lines_removed: int = 0,
) -> str:
    """Generate the final code review report.

    Call this when you have completed your analysis.

    Args:
        verdict: One of "approve", "request_changes", "block".
        summary: 1-3 sentence overview of the review.
        findings: JSON array of finding objects. Each finding has:
            rule_id (str), severity (blocker/major/minor/info), file (str),
            line (int), message (str), suggestion (str), confidence (high/medium/low).
        files_reviewed: Number of files reviewed.
        lines_added: Lines added in the diff.
        lines_removed: Lines removed in the diff.
    """
    # The actual report construction happens in the graph node that processes
    # this tool call. Here we just validate and pass through.
    try:
        parsed_findings = json.loads(findings) if isinstance(findings, str) else findings
    except json.JSONDecodeError:
        parsed_findings = []

    report = {
        "verdict": verdict,
        "summary": summary,
        "findings": parsed_findings,
        "metrics": {
            "files_reviewed": files_reviewed,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        },
    }
    return json.dumps(report, ensure_ascii=False)


# Tool list exposed to the LLM
ALL_TOOLS = [run_lint, read_file, generate_report]
