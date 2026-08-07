"""Environment variable sanitizer — prevent secret leakage to subprocess.

Unhappy path:
  Agent's process has OPENAI_API_KEY and GH_TOKEN in environment.
  LLM says "run bash: env" → subprocess inherits all env vars →
  API key printed in tool output → goes to LLM → might end up in PR comment.

Solution:
  Build a sanitized env dict for subprocess that:
  1. Only passes through whitelisted vars (PATH, HOME, LANG, etc.)
  2. Replaces *KEY*/*SECRET*/*TOKEN* patterns with [REDACTED]
  3. Explicitly passes GH_TOKEN only when needed (not by default)
"""

from __future__ import annotations

import os
import re

# Variables that are safe to pass through to subprocess
SAFE_ENV_VARS = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "TMPDIR", "TMP", "TEMP",
    "SYSTEMROOT", "COMSPEC",  # Windows
}

# Patterns that indicate a secret variable
SECRET_PATTERNS = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE|API)",
    re.IGNORECASE,
)


def build_safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a sanitized environment dict for subprocess execution.

    Only passes through safe variables. Any var matching SECRET_PATTERNS
    is replaced with [REDACTED].

    Args:
        extra: Additional vars to explicitly inject (e.g., {"GH_TOKEN": "xxx"}).
               These are added as-is; the caller decides what to expose.
    """
    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in SAFE_ENV_VARS:
            safe[key] = value
        elif SECRET_PATTERNS.search(key):
            safe[key] = "[REDACTED]"
        # Other vars are simply not passed through

    if extra:
        safe.update(extra)

    return safe
