"""CLI 入口 — 从命令行审查 PR。

学习重点:
  - 如何将各环节串联起来: diff → 规则 → LLM → 报告
  - 无需 webhook 服务器即可进行本地测试
  - 环境变量配置

用法:
  # 通过 PR 编号审查
  python -m cr_agent.cli --repo owner/repo --pr 42

  # 从 diff 文件审查
  python -m cr_agent.cli --diff-file path/to/diff.patch

  # 从 stdin 审查
  git diff main...HEAD | python -m cr_agent.cli --diff-stdin

环境变量:
  OPENAI_API_KEY    LLM 调用所需（必填）
  GH_TOKEN          可选，用于 GitHub API 访问
  CR_MODEL          可选，模型名称（默认: DeepSeek-V4-Flash）
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from cr_agent.observability.logger import setup_logging


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

    # 获取 diff
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
        # 仅确定性模式: 不使用 LLM，仅使用正则规则
        from cr_agent.core.diff_parser import compute_metrics, parse_diff
        from cr_agent.core.models import DiffMetrics, ReviewReport, determine_verdict
        from cr_agent.core.rules_engine import run_deterministic_checks

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
        # 完整 LLM 审查
        from cr_agent.agent.graph import build_graph
        graph = build_graph(model_name=args.model)
        result = graph.invoke({"diff": diff, "pr_info": pr_info, "memory_context": ""})
        from cr_agent.core.models import ReviewReport
        report = ReviewReport(**result["report"])

    elapsed = time.time() - start_time

    # 输出
    print("\n" + "=" * 60)
    print(report.to_markdown())
    print("=" * 60)
    print(f"\nReview completed in {elapsed:.1f}s")
    print(f"Verdict: {report.verdict.value}")
    print(f"Findings: {len(report.findings)}")

    # 如果请求则发布到 GitHub
    if args.post_comment and args.repo and args.pr:
        token = os.environ.get("GH_TOKEN")
        success = post_pr_comment(args.pr, args.repo, report.to_markdown(), token)
        print(f"PR comment: {'posted' if success else 'failed'}")


if __name__ == "__main__":
    main()
