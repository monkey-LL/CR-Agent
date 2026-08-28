"""CR Agent 评测器:加载 YAML fixture -> 运行审查 -> 计算指标 -> 生成报告。

用法:
  # 确定性层评测(无 LLM,零成本,秒级)
  python -m eval.run_eval --mode deterministic --datasets eval/datasets/golden eval/datasets/benign

  # LLM 层评测(需 API key,调 graph.invoke)
  python -m eval.run_eval --mode llm --datasets eval/datasets/golden eval/datasets/injection

  # 指定 trial 次数(仅 LLM 模式有效,默认 1)
  python -m eval.run_eval --mode llm --trials 5 --datasets eval/datasets/golden

  # 输出报告到文件
  python -m eval.run_eval --mode deterministic --output eval/reports/report.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cr_agent.core.diff_parser import parse_diff
from cr_agent.core.models import (
    Finding,
    ReviewReport,
    Severity,
    Verdict,
    determine_verdict,
)
from cr_agent.core.rules_engine import run_deterministic_checks

from eval.metrics import (
    EvalReport,
    EvalResult,
    TrialStats,
    count_hallucinations,
    match_findings,
)


def load_fixtures(dataset_dirs: list[Path]) -> list[dict[str, Any]]:
    """从目录列表加载所有 YAML fixture。"""
    fixtures = []
    for d in dataset_dirs:
        if not d.exists():
            print(f"WARNING: dataset dir {d} does not exist, skipping")
            continue
        for f in sorted(d.glob("*.yaml")):
            with open(f, encoding="utf-8") as fh:
                fixture = yaml.safe_load(fh)
                fixture["_filepath"] = str(f)
                fixtures.append(fixture)
    print(f"Loaded {len(fixtures)} fixtures from {len(dataset_dirs)} dirs")
    return fixtures


def parse_golden_findings(raw: list[dict]) -> list[Finding]:
    """将 YAML 中的 golden_findings 列表转为 Finding 对象。"""
    findings = []
    for item in raw or []:
        findings.append(
            Finding(
                rule_id=item["rule_id"],
                severity=Severity(item["severity"]),
                file=item.get("file"),
                line=item.get("line"),
                message=item.get("message", ""),
                suggestion=item.get("suggestion", ""),
                source="deterministic",
            )
        )
    return findings


def run_deterministic(diff_text: str) -> ReviewReport:
    """确定性层审查:parse_diff -> run_deterministic_checks -> determine_verdict。

    无 LLM 调用,零成本,可复现。
    """
    hunks = parse_diff(diff_text)
    findings = run_deterministic_checks(hunks)
    verdict = determine_verdict(findings)

    from cr_agent.core.diff_parser import compute_metrics
    from cr_agent.core.models import DiffMetrics

    files_changed, lines_added, lines_removed = compute_metrics(hunks)
    return ReviewReport(
        verdict=verdict,
        summary=f"Deterministic review found {len(findings)} issue(s).",
        findings=findings,
        metrics=DiffMetrics(
            files_changed=files_changed,
            lines_added=lines_added,
            lines_removed=lines_removed,
        ),
    )


def run_llm(
    diff_text: str,
    pr_info: dict,
    memory_context: str = "",
    model_name: str | None = None,
) -> ReviewReport:
    """LLM 层审查:build_graph -> graph.invoke。"""
    from cr_agent.agent.graph import build_graph

    model = model_name or os.environ.get("CR_MODEL", "DeepSeek-V4-Flash")
    graph = build_graph(model_name=model)
    result = graph.invoke({
        "diff": diff_text,
        "pr_info": pr_info,
        "memory_context": memory_context,
    })
    return ReviewReport(**result["report"])


def get_diff_files(diff_text: str) -> set[str]:
    """从 diff 文本中提取所有涉及的文件路径。"""
    files = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            if path != "/dev/null":
                files.add(path)
        elif line.startswith("--- a/"):
            path = line[6:]
            if path != "/dev/null":
                files.add(path)
    return files


def evaluate_single(
    fixture: dict[str, Any],
    mode: str = "deterministic",
    model_name: str | None = None,
) -> EvalResult:
    """对单条 fixture 运行评测。"""
    fid = fixture["id"]
    golden_verdict = Verdict(fixture["golden_verdict"])
    golden_findings = parse_golden_findings(fixture.get("golden_findings", []))
    diff_text = fixture["diff"]
    diff_files = get_diff_files(diff_text)

    try:
        if mode == "deterministic":
            report = run_deterministic(diff_text)
        elif mode == "llm":
            pr_info = fixture.get("pr_info", {})
            report = run_llm(diff_text, pr_info, model_name=model_name)
        else:
            return EvalResult(
                fixture_id=fid,
                golden_verdict=golden_verdict,
                actual_verdict=Verdict.APPROVE,
                verdict_correct=False,
                error=f"unknown mode: {mode}",
            )
    except Exception as e:
        return EvalResult(
            fixture_id=fid,
            golden_verdict=golden_verdict,
            actual_verdict=Verdict.APPROVE,
            verdict_correct=False,
            error=str(e),
        )

    actual_findings = report.findings
    actual_verdict = report.verdict

    # finding 匹配
    matches = match_findings(golden_findings, actual_findings)

    # 幻觉检测
    hallucinated = count_hallucinations(actual_findings, diff_files)

    # 注入抗性:如果 fixture 标记为 injection,检查 verdict 是否被注入影响
    injection_followed = False
    if fixture.get("injection"):
        # 注入"成功"= verdict 不等于 golden_verdict（说明被注入影响了）
        injection_followed = actual_verdict != golden_verdict

    return EvalResult(
        fixture_id=fid,
        golden_verdict=golden_verdict,
        actual_verdict=actual_verdict,
        verdict_correct=actual_verdict == golden_verdict,
        finding_matches=matches,
        hallucinated_findings=hallucinated,
        injection_followed=injection_followed,
    )


def run_eval_suite(
    fixtures: list[dict[str, Any]],
    mode: str = "deterministic",
    model_name: str | None = None,
    trials: int = 1,
) -> EvalReport:
    """运行整个评测 suite。

    trials > 1 时，每条 fixture 跑多 trial，报告统计分布：
      - pass@k / pass^k
      - mean precision / recall (macro-avg)
      - verdict consistency
    同时保留 best trial 用于单 trial 兼容的指标累加。
    """
    report = EvalReport()
    report.trials_per_fixture = trials

    for fixture in fixtures:
        fid = fixture["id"]
        trial_results: list[EvalResult] = []

        for trial_n in range(trials):
            if trials > 1:
                print(f"  [{fid}] trial {trial_n + 1}/{trials}...")
            result = evaluate_single(fixture, mode=mode, model_name=model_name)
            trial_results.append(result)

        # 构建 TrialStats
        ts = TrialStats(fixture_id=fid, trials=trial_results)
        report.trial_stats.append(ts)

        # 取最优 trial 用于单 trial 兼容的指标累加
        best = ts.best_trial()
        report.results.append(best)
        report.total += 1

        if best.error:
            report.errored += 1
            print(f"  [{fid}] ERROR: {best.error}")
        elif best.passed:
            report.passed += 1
            print(f"  [{fid}] PASS (verdict={best.actual_verdict.value})")
        else:
            report.failed += 1
            print(
                f"  [{fid}] FAIL "
                f"(verdict={best.actual_verdict.value} golden={best.golden_verdict.value} "
                f"TP={best.tp} FP={best.fp} FN={best.fn})"
            )

        # multi-trial 额外打印
        if trials > 1:
            print(
                f"  [{fid}] trials: {ts.pass_count}/{ts.trial_count} passed, "
                f"consistency={ts.verdict_consistency:.2f}"
            )

        # 累加指标（用 best trial）
        report.total_tp += best.tp
        report.total_fp += best.fp
        report.total_fn += best.fn
        report.verdict_matrix[(best.golden_verdict.value, best.actual_verdict.value)] += 1
        report.total_hallucinated += best.hallucinated_findings
        report.total_actual_findings += len(best.finding_matches)

        if fixture.get("injection"):
            report.injection_total += 1
            if best.injection_followed:
                report.injection_followed_count += 1

    return report


def main():
    parser = argparse.ArgumentParser(description="CR Agent Evaluation Runner")
    parser.add_argument(
        "--mode",
        choices=["deterministic", "llm"],
        default="deterministic",
        help="评测模式: deterministic(无LLM) | llm(调 graph.invoke)",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=["eval/datasets/golden"],
        help="数据集目录列表(可传多个)",
    )
    parser.add_argument("--trials", type=int, default=1, help="每条 fixture 的 trial 次数(LLM 模式)")
    parser.add_argument("--model", default=None, help="LLM 模型名称(覆盖 CR_MODEL 环境变量)")
    parser.add_argument("--output", default=None, help="报告输出路径(markdown)")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON 格式报告")
    parser.add_argument(
        "--baseline-key",
        default=None,
        help="baseline.json 中的键名(默认用 mode)。用于区分不同模型的多 trial 基线，如 llm_pro_5trial",
    )

    args = parser.parse_args()

    dataset_dirs = [Path(d) for d in args.datasets]
    fixtures = load_fixtures(dataset_dirs)

    if not fixtures:
        print("ERROR: no fixtures found")
        sys.exit(1)

    print(f"\nRunning {args.mode} eval on {len(fixtures)} fixtures (trials={args.trials})\n")

    start_time = time.time()
    report = run_eval_suite(
        fixtures,
        mode=args.mode,
        model_name=args.model,
        trials=args.trials if args.mode == "llm" else 1,
    )
    elapsed = time.time() - start_time

    # 输出报告
    md_report = report.to_markdown()
    print(f"\n{'=' * 60}")
    print(md_report)
    print(f"\n{'=' * 60}")
    print(f"Eval completed in {elapsed:.1f}s")

    # 保存报告
    reports_dir = PROJECT_ROOT / "eval" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    md_path = args.output or str(reports_dir / f"eval_{args.mode}_{timestamp}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"\nReport saved to: {md_path}")

    if args.json:
        json_path = md_path.replace(".md", ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"JSON report saved to: {json_path}")

    # 写入/更新 baseline.json
    baseline_path = reports_dir / "baseline.json"
    if not baseline_path.exists():
        baseline = {"deterministic": {}, "llm": {}, "history": []}
    else:
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)

    baseline_key = args.baseline_key or args.mode
    entry = report.to_dict()
    entry["timestamp"] = timestamp
    entry["fixtures"] = len(fixtures)
    entry["elapsed_seconds"] = round(elapsed, 1)
    entry["model"] = args.model or os.environ.get("CR_MODEL", "unknown")
    baseline[baseline_key] = entry

    # 追加历史记录（用于趋势监控）
    if "history" not in baseline:
        baseline["history"] = []
    baseline["history"].append({
        "key": baseline_key,
        "timestamp": timestamp,
        "precision": entry.get("precision", 0),
        "recall": entry.get("recall", 0),
        "f1": entry.get("f1", 0),
        "verdict_accuracy": entry.get("verdict_accuracy", 0),
        "trials_per_fixture": entry.get("trials_per_fixture", 1),
        "pass_at_k": entry.get("pass_at_k"),
        "pass_pow_k": entry.get("pass_pow_k"),
    })
    # 只保留最近 50 条历史
    baseline["history"] = baseline["history"][-50:]

    with open(baseline_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)
    print(f"Baseline updated: {baseline_path}")

    # 退出码门禁
    if report.errored > 0:
        sys.exit(1)
    if args.mode == "deterministic":
        # 确定性层：verdict accuracy 必须 100%
        if report.verdict_accuracy < 1.0:
            sys.exit(1)
    else:
        # LLM 层：multi-trial 时用 pass^k 做稳定性门禁
        if args.trials > 1 and report.pass_pow_k < 0.6:
            print(f"FAIL: pass^{args.trials} = {report.pass_pow_k:.2f} < 0.60 threshold")
            sys.exit(1)
        elif report.verdict_accuracy < 0.9:
            sys.exit(1)


if __name__ == "__main__":
    main()
