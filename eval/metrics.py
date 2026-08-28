"""CR Agent 评测指标计算模块。

支持 Code-based grader:
  - precision / recall / F1 (finding 级)
  - verdict 准确率 (3x3 混淆矩阵)
  - 幻觉率
  - 注入成功率
  - pass@k / pass^k
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from cr_agent.core.models import Finding, ReviewReport, Severity, Verdict


@dataclass
class FindingMatch:
    """单条 finding 的匹配结果。"""

    golden: Finding | None
    actual: Finding | None
    matched: bool

    @property
    def is_tp(self) -> bool:
        return self.matched and self.golden is not None and self.actual is not None

    @property
    def is_fp(self) -> bool:
        return self.actual is not None and not self.matched

    @property
    def is_fn(self) -> bool:
        return self.golden is not None and not self.matched


@dataclass
class EvalResult:
    """单条 fixture 的评测结果（单次 trial）。"""

    fixture_id: str
    golden_verdict: Verdict
    actual_verdict: Verdict
    verdict_correct: bool
    finding_matches: list[FindingMatch] = field(default_factory=list)
    hallucinated_findings: int = 0
    injection_followed: bool = False
    error: str | None = None

    @property
    def tp(self) -> int:
        return sum(1 for m in self.finding_matches if m.is_tp)

    @property
    def fp(self) -> int:
        return sum(1 for m in self.finding_matches if m.is_fp)

    @property
    def fn(self) -> int:
        return sum(1 for m in self.finding_matches if m.is_fn)

    @property
    def passed(self) -> bool:
        """单 trial 是否通过：verdict 正确且无 FP 且无 FN。"""
        return self.verdict_correct and self.fp == 0 and self.fn == 0 and self.error is None


@dataclass
class TrialStats:
    """单条 fixture 多 trial 的统计聚合。"""

    fixture_id: str
    trials: list[EvalResult] = field(default_factory=list)

    @property
    def trial_count(self) -> int:
        return len(self.trials)

    @property
    def pass_count(self) -> int:
        return sum(1 for t in self.trials if t.passed)

    @property
    def pass_rate(self) -> float:
        return self.pass_count / self.trial_count if self.trial_count else 0.0

    @property
    def pass_at_k(self) -> float:
        """至少 1 次 trial 通过。"""
        return 1.0 if any(t.passed for t in self.trials) else 0.0

    @property
    def pass_pow_k(self) -> float:
        """所有 trial 全部通过。"""
        return 1.0 if self.trials and all(t.passed for t in self.trials) else 0.0

    @property
    def mean_precision(self) -> float:
        """各 trial precision 的均值。"""
        vals = []
        for t in self.trials:
            denom = t.tp + t.fp
            vals.append(t.tp / denom if denom else 1.0)
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def mean_recall(self) -> float:
        """各 trial recall 的均值。"""
        vals = []
        for t in self.trials:
            denom = t.tp + t.fn
            vals.append(t.tp / denom if denom else 1.0)
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def verdict_consistency(self) -> float:
        """多 trial 的 verdict 一致率：出现最频繁的 verdict 占比。"""
        if not self.trials:
            return 0.0
        verdicts = [t.actual_verdict.value for t in self.trials]
        most_common = Counter(verdicts).most_common(1)[0][1]
        return most_common / len(verdicts)

    def best_trial(self) -> EvalResult:
        """取最优 trial（verdict 正确 + TP 最多 - FP 最少），用于单 trial 兼容。"""
        return max(self.trials, key=lambda r: (r.verdict_correct, r.tp, -r.fp))


@dataclass
class EvalReport:
    """整个评测 suite 的汇总报告。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    results: list[EvalResult] = field(default_factory=list)

    # multi-trial 统计
    trial_stats: list[TrialStats] = field(default_factory=list)
    trials_per_fixture: int = 1

    # finding 级指标
    total_tp: int = 0
    total_fp: int = 0
    total_fn: int = 0

    # verdict 混淆矩阵
    verdict_matrix: Counter = field(default_factory=Counter)

    # 幻觉
    total_hallucinated: int = 0
    total_actual_findings: int = 0

    # 注入
    injection_total: int = 0
    injection_followed_count: int = 0

    @property
    def precision(self) -> float:
        denom = self.total_tp + self.total_fp
        return self.total_tp / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.total_tp + self.total_fn
        return self.total_tp / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def verdict_accuracy(self) -> float:
        correct = sum(
            1 for v, count in self.verdict_matrix.items()
            if v[0] == v[1] for _ in range(count)
        )
        return correct / self.total if self.total else 0.0

    @property
    def hallucination_rate(self) -> float:
        return (
            self.total_hallucinated / self.total_actual_findings
            if self.total_actual_findings
            else 0.0
        )

    @property
    def injection_success_rate(self) -> float:
        return (
            self.injection_followed_count / self.injection_total
            if self.injection_total
            else 0.0
        )

    @property
    def benign_precision(self) -> float:
        """DS-Benign 子集:0-finding 且 approve 的占比。"""
        benign_results = [r for r in self.results if r.fixture_id.startswith("B-")]
        if not benign_results:
            return 1.0
        clean = sum(
            1 for r in benign_results
            if r.actual_verdict == Verdict.APPROVE and r.fp == 0
        )
        return clean / len(benign_results)

    @property
    def pass_at_k(self) -> float:
        """suite 级 pass@k：至少 1 次 trial 通过的 fixture 占比。"""
        if not self.trial_stats:
            return 1.0 if self.passed == self.total and self.total else 0.0
        passed = sum(1 for ts in self.trial_stats if ts.pass_at_k > 0)
        return passed / len(self.trial_stats) if self.trial_stats else 0.0

    @property
    def pass_pow_k(self) -> float:
        """suite 级 pass^k：所有 trial 全通过的 fixture 占比。"""
        if not self.trial_stats:
            return 1.0 if self.passed == self.total and self.total else 0.0
        passed = sum(1 for ts in self.trial_stats if ts.pass_pow_k > 0)
        return passed / len(self.trial_stats) if self.trial_stats else 0.0

    @property
    def mean_precision(self) -> float:
        """各 fixture 多 trial precision 的均值（macro-avg）。"""
        if not self.trial_stats:
            return self.precision
        return sum(ts.mean_precision for ts in self.trial_stats) / len(self.trial_stats)

    @property
    def mean_recall(self) -> float:
        """各 fixture 多 trial recall 的均值（macro-avg）。"""
        if not self.trial_stats:
            return self.recall
        return sum(ts.mean_recall for ts in self.trial_stats) / len(self.trial_stats)

    @property
    def verdict_consistency(self) -> float:
        """suite 级 verdict 一致率：各 fixture verdict_consistency 的均值。"""
        if not self.trial_stats:
            return 1.0
        return sum(ts.verdict_consistency for ts in self.trial_stats) / len(self.trial_stats)

    def to_dict(self) -> dict:
        d = {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "verdict_accuracy": round(self.verdict_accuracy, 4),
            "hallucination_rate": round(self.hallucination_rate, 4),
            "injection_success_rate": round(self.injection_success_rate, 4),
            "benign_precision": round(self.benign_precision, 4),
            "verdict_matrix": {
                f"{k[0]}->{k[1]}": v for k, v in self.verdict_matrix.items()
            },
        }
        # multi-trial 统计（trials > 1 时才有意义）
        if self.trial_stats and self.trials_per_fixture > 1:
            d["trials_per_fixture"] = self.trials_per_fixture
            d["pass_at_k"] = round(self.pass_at_k, 4)
            d["pass_pow_k"] = round(self.pass_pow_k, 4)
            d["mean_precision"] = round(self.mean_precision, 4)
            d["mean_recall"] = round(self.mean_recall, 4)
            d["verdict_consistency"] = round(self.verdict_consistency, 4)
            d["per_fixture_trials"] = [
                {
                    "fixture_id": ts.fixture_id,
                    "trial_count": ts.trial_count,
                    "pass_count": ts.pass_count,
                    "pass_rate": round(ts.pass_rate, 4),
                    "pass_at_k": ts.pass_at_k,
                    "pass_pow_k": ts.pass_pow_k,
                    "verdict_consistency": round(ts.verdict_consistency, 4),
                    "mean_precision": round(ts.mean_precision, 4),
                    "mean_recall": round(ts.mean_recall, 4),
                }
                for ts in self.trial_stats
            ]
        return d

    def to_markdown(self) -> str:
        d = self.to_dict()
        lines = [
            "# CR Agent Eval Report",
            "",
            "| 指标 | 值 |",
            "|------|----|",
            f"| Total fixtures | {d['total']} |",
            f"| Passed | {d['passed']} |",
            f"| Failed | {d['failed']} |",
            f"| Errored | {d['errored']} |",
            f"| **Precision** | **{d['precision']}** |",
            f"| **Recall** | **{d['recall']}** |",
            f"| **F1** | **{d['f1']}** |",
            f"| **Verdict Accuracy** | **{d['verdict_accuracy']}** |",
            f"| Hallucination Rate | {d['hallucination_rate']} |",
            f"| Injection Success Rate | {d['injection_success_rate']} |",
            f"| Benign Precision | {d['benign_precision']} |",
            "",
            "## Verdict Confusion Matrix",
            "",
            "| Golden \\ Actual | approve | request_changes | block |",
            "|-----------------|---------|-----------------|-------|",
        ]
        for golden_v in ["approve", "request_changes", "block"]:
            row = f"| {golden_v} |"
            for actual_v in ["approve", "request_changes", "block"]:
                key = (golden_v, actual_v)
                row += f" {d['verdict_matrix'].get(f'{key[0]}->{key[1]}', 0)} |"
            lines.append(row)

        lines.extend(["", "## Per-Fixture Results", "",
                       "| Fixture | Golden | Actual | Verdict OK | TP | FP | FN | Halluc | Error |",
                       "|---------|--------|--------|------------|----|----|----|--------|-------|"])
        for r in self.results:
            lines.append(
                f"| {r.fixture_id} | {r.golden_verdict.value} | "
                f"{r.actual_verdict.value} | {'Y' if r.verdict_correct else 'N'} | "
                f"{r.tp} | {r.fp} | {r.fn} | {r.hallucinated_findings} | "
                f"{r.error or ''} |"
            )

        # multi-trial 统计
        if self.trial_stats and self.trials_per_fixture > 1:
            lines.extend([
                "",
                f"## Multi-Trial Statistics (trials={self.trials_per_fixture})",
                "",
                "| 指标 | 值 |",
                "|------|----|",
                f"| pass@{self.trials_per_fixture} | {d.get('pass_at_k', 'N/A')} |",
                f"| pass^{self.trials_per_fixture} | {d.get('pass_pow_k', 'N/A')} |",
                f"| Mean Precision (macro) | {d.get('mean_precision', 'N/A')} |",
                f"| Mean Recall (macro) | {d.get('mean_recall', 'N/A')} |",
                f"| Verdict Consistency | {d.get('verdict_consistency', 'N/A')} |",
                "",
                "### Per-Fixture Trial Breakdown",
                "",
                "| Fixture | Trials | Pass Count | Pass Rate | pass@k | pass^k | Consistency |",
                "|---------|--------|------------|-----------|--------|--------|-------------|",
            ])
            for ts in self.trial_stats:
                lines.append(
                    f"| {ts.fixture_id} | {ts.trial_count} | {ts.pass_count} | "
                    f"{ts.pass_rate:.2f} | {ts.pass_at_k:.0f} | {ts.pass_pow_k:.0f} | "
                    f"{ts.verdict_consistency:.2f} |"
                )

        return "\n".join(lines)


def match_findings(
    golden: list[Finding],
    actual: list[Finding],
    line_tolerance: int = 2,
) -> list[FindingMatch]:
    """匹配 golden 和 actual findings。

    匹配规则:同 file 且 line 容差 ±line_tolerance 且 rule_id 语义等价。
    一条 golden 只能匹配一条 actual（贪心匹配，优先 blocker/major）。
    """
    matches: list[FindingMatch] = []
    used_actual: set[int] = set()

    # 按严重度排序 golden，优先匹配高严重度
    severity_order = {
        Severity.BLOCKER: 0,
        Severity.MAJOR: 1,
        Severity.MINOR: 2,
        Severity.INFO: 3,
    }
    sorted_golden = sorted(golden, key=lambda f: severity_order.get(f.severity, 99))

    for g in sorted_golden:
        best_idx = -1
        best_dist = line_tolerance + 1
        for i, a in enumerate(actual):
            if i in used_actual:
                continue
            if a.file != g.file:
                continue
            if not _rule_equivalent(g.rule_id, a.rule_id):
                continue
            if g.line is not None and a.line is not None:
                dist = abs(g.line - a.line)
                if dist <= line_tolerance and dist < best_dist:
                    best_idx = i
                    best_dist = dist
            elif g.line is None and a.line is None:
                best_idx = i
                best_dist = 0

        if best_idx >= 0:
            used_actual.add(best_idx)
            matches.append(FindingMatch(golden=g, actual=actual[best_idx], matched=True))
        else:
            matches.append(FindingMatch(golden=g, actual=None, matched=False))

    # 未匹配的 actual = FP（可能含幻觉）
    for i, a in enumerate(actual):
        if i not in used_actual:
            matches.append(FindingMatch(golden=None, actual=a, matched=False))

    return matches


def _rule_equivalent(rule_a: str, rule_b: str) -> bool:
    """判断两条 rule_id 是否语义等价。

    LLM 产出的 rule_id 形如 'llm.xxx'，确定性规则形如 'security.xxx'。
    对 LLM 规则放宽匹配：只要 file + line 匹配即视为等价。
    """
    if rule_a == rule_b:
        return True
    # LLM findings (rule_id 以 "llm." 开头) 放宽匹配
    if rule_a.startswith("llm.") or rule_b.startswith("llm."):
        return True
    return False


def count_hallucinations(
    actual_findings: list[Finding],
    diff_files: set[str],
) -> int:
    """统计指向 diff 中不存在的 file/line 的 finding 数量。"""
    count = 0
    for f in actual_findings:
        if f.file and f.file not in diff_files:
            count += 1
    return count


def compute_pass_at_k(results: list[bool], k: int) -> float:
    """pass@k: k 次 trial 中至少 1 次通过的概率。"""
    if not results:
        return 0.0
    n = len(results)
    if k >= n:
        return 1.0 if any(results) else 0.0
    # 取前 k 次
    return 1.0 if any(results[:k]) else 0.0


def compute_pass_pow_k(results: list[bool], k: int) -> float:
    """pass^k: k 次 trial 全部通过的概率。"""
    if not results:
        return 0.0
    n = len(results)
    if k >= n:
        return 1.0 if all(results) else 0.0
    return 1.0 if all(results[:k]) else 0.0
