"""CLI entry point — review a PR from the command line.

Learning focus:
  - How to wire everything together: diff → rules → LLM → report
  - Local testing without a webhook server
  - Environment variable configuration

Usage:
  # Review a PR by number
  python -m cr_agent.cli --repo owner/repo --pr 42

  # Review from a diff file
  python -m cr_agent.cli --diff-file path/to/diff.patch

  # Review from stdin
  git diff main...HEAD | python -m cr_agent.cli --diff-stdin

Environment:
  OPENAI_API_KEY    Required for LLM calls
  GH_TOKEN          Optional, for GitHub API access
  CR_MODEL          Optional, model name (default: DeepSeek-V4-Flash)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from cr_agent.observability.logger import logger, setup_logging


def main():
    parser = argparse.ArgumentParser(description="CR Agent — Code Review Agent")
    parser.add_argument("--repo", help="GitHub repo (owner/repo)")
    parser.add_argument("--pr", type=int, help="PR number to review")
    parser.add_argument("--diff-file", help="Path to a diff file to review")
    parser.add_argument("--diff-stdin", action="store_true", help="Read diff from stdin")
    parser.add_argument("--model", default=os.environ.get("CR_MODEL", "DeepSeek-V4-Flash"))
    parser.add_argument("--no-llm", action="store_true", help="Run deterministic checks only (no LLM)")
    parser.add_argument("--post-comment", action="store_true", help="Post review as PR comment")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else "INFO")

    # Get diff
    if args.diff_file:
        with open(args.diff_file) as f:
            diff = f.read()
        pr_info = {"repo": "local", "number": 0, "title": "Local diff", "author": "", "base": "", "head": ""}
    elif args.diff_stdin:
        diff = sys.stdin.read()
        pr_info = {"repo": "local", "number": 0, "title": "Stdin diff", "author": "", "base": "", "head": ""}
    elif args.repo and args.pr:
        from cr_agent.github.client import get_pr_diff, get_pr_info, post_pr_comment
        token = os.environ.get("GH_TOKEN")
        diff = get_pr_diff(args.pr, args.repo, token)
        info = get_pr_info(args.pr, args.repo, token)
        pr_info = {
            "repo": args.repo, "number": args.pr,
            "title": info.title, "author": info.author,
            "base": info.base, "head": info.head,
        }
    else:
        parser.error("Provide --repo + --pr, --diff-file, or --diff-stdin")

    if not diff.strip():
        print("Error: empty diff, nothing to review.")
        sys.exit(1)

    start_time = time.time()

    if args.no_llm:
        # Deterministic-only mode: no LLM, just regex rules
        from cr_agent.core.diff_parser import parse_diff, compute_metrics
        from cr_agent.core.rules_engine import run_deterministic_checks
        from cr_agent.core.models import ReviewReport, determine_verdict, DiffMetrics

        hunks = parse_diff(diff)
        findings = run_deterministic_checks(hunks)
        files_changed, added, removed = compute_metrics(hunks)
        verdict = determine_verdict(findings)

        report = ReviewReport(
            verdict=verdict,
            summary=f"Deterministic review: {len(findings)} findings (no LLM analysis).",
            findings=findings,
            metrics=DiffMetrics(files_changed=files_changed, lines_added=added, lines_removed=removed),
        )
    else:
        # Full LLM review
        from cr_agent.agent.graph import build_graph
        graph = build_graph(model_name=args.model)
        result = graph.invoke({"diff": diff, "pr_info": pr_info})
        from cr_agent.core.models import ReviewReport
        report = ReviewReport(**result["report"])

    elapsed = time.time() - start_time

    # Output
    print("\n" + "=" * 60)
    print(report.to_markdown())
    print("=" * 60)
    print(f"\nReview completed in {elapsed:.1f}s")
    print(f"Verdict: {report.verdict.value}")
    print(f"Findings: {len(report.findings)}")

    # Post to GitHub if requested
    if args.post_comment and args.repo and args.pr:
        token = os.environ.get("GH_TOKEN")
        success = post_pr_comment(args.pr, args.repo, report.to_markdown(), token)
        print(f"PR comment: {'posted' if success else 'failed'}")


if __name__ == "__main__":
    main()
