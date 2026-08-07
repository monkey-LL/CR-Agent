"""Sandbox: isolated execution for lint, type-check, and git commands.

Learning focus:
  - subprocess with sanitized environment (no secret leakage)
  - Timeout enforcement (lint hanging = review blocked)
  - Graceful degradation (lint fails → "skipped", not crash)
  - Output truncation (protect token budget)

Production vs Demo:
  Demo: subprocess.run("ruff check .") → if it fails, crash.
  Prod:  env sanitized → timeout set → error caught → return "skipped" → review continues.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from cr_agent.core.models import StaticAnalysisResult
from cr_agent.security.env_sanitizer import build_safe_env


@dataclass
class SandboxConfig:
    """Configuration for sandbox execution."""
    timeout: int = 30
    max_output_chars: int = 2000


def run_command(
    command: str,
    cwd: str = ".",
    config: SandboxConfig | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a shell command in the sandbox with sanitized environment.

    Security: environment variables are filtered. Only safe vars (PATH, HOME, etc.)
    pass through. Secret vars (KEY, TOKEN, etc.) are redacted. Explicit extra_env
    can inject specific vars (e.g., GH_TOKEN for gh CLI).
    """
    config = config or SandboxConfig()
    safe_env = build_safe_env(extra_env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=config.timeout,
            cwd=cwd,
            env=safe_env,
        )
        output = (result.stdout + result.stderr)[:config.max_output_chars]
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {config.timeout}s"
    except FileNotFoundError:
        return -1, "Command not found"
    except Exception as e:
        return -1, f"Error: {type(e).__name__}: {e}"


def run_lint_check(
    tool: str,
    command: str,
    cwd: str = ".",
    config: SandboxConfig | None = None,
) -> StaticAnalysisResult:
    """Run a lint tool and return a structured result.

    Graceful degradation: if the tool isn't installed or fails, return
    status="skipped" so the review continues without that analysis.
    """
    exit_code, output = run_command(command, cwd=cwd, config=config)

    if exit_code == -1:
        return StaticAnalysisResult(
            tool=tool, status="skipped", issues=0, output=output,
        )

    issue_count = 0
    if exit_code != 0:
        if "error" in output.lower():
            issue_count = output.lower().count("error")
        elif "warning" in output.lower():
            issue_count = output.lower().count("warning")
        else:
            issue_count = 1

    return StaticAnalysisResult(
        tool=tool,
        status="pass" if exit_code == 0 else "fail",
        issues=issue_count,
        output=output,
    )
