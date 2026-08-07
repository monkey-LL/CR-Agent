"""Review memory — learn from past reviews to improve future ones.

Unhappy path (without memory):
  Every PR review starts from scratch. Agent doesn't know:
  - This repo always has SQL injection issues in the auth module
  - The team's convention is to use logging, not print
  - Last review of this PR already found 3 issues

With memory:
  Before reviewing PR #42, load history:
  - "Previous reviews of this repo found: SQL injection (3x), hardcoded secrets (2x)"
  - "Last review of PR #42: 3 findings (1 blocker, 2 major)"
  This context goes into the LLM prompt, improving focus and consistency.

Storage (simplified):
  JSON file per repo: {repo}_{pr_number}.json
  Production: use a database (Postgres) with vector search for semantic recall.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(os.environ.get("CR_MEMORY_DIR", ".cr_agent_memory"))


def save_review_memory(repo: str, pr_number: int, findings: list[dict], verdict: str):
    """Save review results to memory for future reference."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory_file = MEMORY_DIR / f"{repo.replace('/', '_')}_{pr_number}.json"

    record = {
        "repo": repo,
        "pr_number": pr_number,
        "verdict": verdict,
        "findings_count": len(findings),
        "finding_types": list({f.get("rule_id", "unknown") for f in findings}),
        "severities": list({f.get("severity", "info") for f in findings}),
        "reviewed_at": datetime.now().isoformat(),
    }

    # Append to history list
    history: list[dict] = []
    if memory_file.exists():
        try:
            data = json.loads(memory_file.read_text())
            history = data.get("history", [])
        except (json.JSONDecodeError, KeyError):
            pass

    history.append(record)
    memory_file.write_text(json.dumps({"history": history}, indent=2, ensure_ascii=False))
    logger.info("Memory saved: %s/%d (%d findings, verdict=%s)", repo, pr_number, len(findings), verdict)


def load_review_memory(repo: str, pr_number: int | None = None) -> dict | None:
    """Load review memory for a repo (and optionally a specific PR)."""
    if pr_number is not None:
        memory_file = MEMORY_DIR / f"{repo.replace('/', '_')}_{pr_number}.json"
    else:
        # Load all memory for this repo
        memory_file = MEMORY_DIR / f"{repo.replace('/', '_')}_all.json"

    if not memory_file.exists():
        return None

    try:
        return json.loads(memory_file.read_text())
    except json.JSONDecodeError:
        return None


def build_memory_context(repo: str) -> str:
    """Build a context string from past reviews for the LLM prompt.

    Scans all memory files for this repo and summarizes patterns:
    "Previous reviews of this repo commonly found: SQL injection (3x), hardcoded secrets (2x).
     Focus on these patterns."
    """
    pattern_counts: dict[str, int] = {}
    total_reviews = 0

    for memory_file in MEMORY_DIR.glob(f"{repo.replace('/', '_')}*.json"):
        try:
            data = json.loads(memory_file.read_text())
            for record in data.get("history", []):
                total_reviews += 1
                for finding_type in record.get("finding_types", []):
                    pattern_counts[finding_type] = pattern_counts.get(finding_type, 0) + 1
        except (json.JSONDecodeError, KeyError):
            continue

    if not pattern_counts:
        return ""

    # Top 5 most common finding types
    top_patterns = sorted(pattern_counts.items(), key=lambda x: -x[1])[:5]
    patterns_text = ", ".join(f"{name} ({count}x)" for name, count in top_patterns)

    return (
        f"\n## Repository Memory ({total_reviews} past reviews)\n"
        f"Common issues found in this repo: {patterns_text}\n"
        f"Pay extra attention to these patterns.\n"
    )
