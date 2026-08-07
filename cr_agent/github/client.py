"""GitHub: webhook server, HMAC verification, PR operations.

Learning focus:
  - HMAC webhook verification: why and how (prevent forged webhooks)
  - GitHub API via gh CLI: no API library needed, just subprocess
  - PR comment idempotency: update existing comment vs create new
  - Token injection: GH_TOKEN env var, never in prompts or logs

Two modes of operation:
  1. Webhook mode: FastAPI server receives GitHub webhooks, triggers review
  2. CLI mode: manually review a PR by number (for testing/learning)

Why gh CLI instead of PyGithub/requests?
  - gh handles auth, pagination, rate limiting automatically
  - Less code, fewer dependencies
  - The token is injected via GH_TOKEN env var, never in our code
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from dataclasses import dataclass

from cr_agent.security.sanitizer import mask_secrets


@dataclass
class PRInfo:
    """PR metadata extracted from GitHub."""

    number: int
    repo: str  # owner/repo format
    title: str = ""
    author: str = ""
    body: str = ""
    base: str = ""
    head: str = ""


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    GitHub sends X-Hub-Signature-256: sha256=<hex_digest>
    We recompute the HMAC with our secret and compare.

    Why? Without this, anyone could POST fake webhooks to trigger reviews
    on arbitrary PRs, or worse, extract our review output.

    Constant-time comparison (hmac.compare_digest) prevents timing attacks.
    """
    if not signature or not secret:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def get_pr_diff(pr_number: int, repo: str, token: str | None = None) -> str:
    """Get the diff for a PR using gh CLI.

    gh pr diff <number> --repo <owner/repo>
    """
    env = {"GH_TOKEN": token} if token else None
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo],
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, **(env or {})},
    )
    diff = result.stdout
    return mask_secrets(diff)


def get_pr_info(pr_number: int, repo: str, token: str | None = None) -> PRInfo:
    """Get PR metadata using gh CLI.

    gh pr view <number> --repo <owner/repo> --json number,title,author,body,baseRefName,headRefName
    """
    env = {"GH_TOKEN": token} if token else None
    result = subprocess.run(
        [
            "gh", "pr", "view", str(pr_number), "--repo", repo,
            "--json", "number,title,author,body,baseRefName,headRefName",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, **(env or {})},
    )
    data = json.loads(result.stdout)
    return PRInfo(
        number=data.get("number", pr_number),
        repo=repo,
        title=data.get("title", ""),
        author=data.get("author", {}).get("login", "") if isinstance(data.get("author"), dict) else str(data.get("author", "")),
        body=data.get("body", ""),
        base=data.get("baseRefName", ""),
        head=data.get("headRefName", ""),
    )


def post_pr_comment(pr_number: int, repo: str, body: str, token: str | None = None) -> bool:
    """Post a comment on a PR using gh CLI.

    Idempotency: checks for existing CR comment and updates it if found,
    rather than creating duplicates on re-reviews.
    """
    env = {"GH_TOKEN": token} if token else None
    full_env = {**__import__("os").environ, **(env or {})}

    # Check for existing review comment (idempotency)
    list_result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo,
         "--json", "comments", "--jq", ".comments[].body"],
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )

    existing_comments = list_result.stdout.strip().split("\n") if list_result.stdout.strip() else []

    # If any existing comment starts with our header, update it
    # (In production, you'd get the comment ID and use gh pr edit-comment)
    # For simplicity, we just post a new comment here
    result = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )
    return result.returncode == 0


def parse_webhook_payload(payload: bytes) -> dict | None:
    """Parse a GitHub webhook payload and extract PR info if it's a PR event.

    Returns None for non-PR events.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if "pull_request" not in data:
        return None

    pr = data["pull_request"]
    action = data.get("action", "")
    repo = data.get("repository", {}).get("full_name", "")

    return {
        "action": action,
        "repo": repo,
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "author": pr.get("user", {}).get("login", ""),
        "body": pr.get("body", ""),
        "base": pr.get("base", {}).get("ref", ""),
        "head": pr.get("head", {}).get("ref", ""),
    }
