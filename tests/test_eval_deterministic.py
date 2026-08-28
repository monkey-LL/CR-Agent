"""确定性层评测测试:零 LLM、零成本、CI 必跑。

验证 28 条确定性规则在黄金 fixture 上的 precision/recall/verdict 准确率。
参照 cefen-backend.md CP-4 / 3.4.4 用例 4.2:
  "DS-Golden 跑 run_deterministic_checks(无 LLM),
   规则层 finding 与黄金确定性部分 100% 匹配,免费、可复现"
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cr_agent.core.diff_parser import parse_diff
from cr_agent.core.models import Severity, Verdict, determine_verdict
from cr_agent.core.rules_engine import run_deterministic_checks
from eval.metrics import match_findings

DATASETS_DIR = PROJECT_ROOT / "eval" / "datasets"


def load_dataset(subdir: str) -> list[dict]:
    """加载指定子集的所有 fixture。"""
    d = DATASETS_DIR / subdir
    if not d.exists():
        return []
    fixtures = []
    for f in sorted(d.glob("*.yaml")):
        with open(f, encoding="utf-8") as fh:
            fixtures.append(yaml.safe_load(fh))
    return fixtures


def get_all_fixtures() -> list[tuple[str, dict]]:
    """加载所有子集的 fixture,返回 (subset_name, fixture) 列表。"""
    result = []
    for subdir in ["golden", "injection", "benign", "adversarial"]:
        for fixture in load_dataset(subdir):
            result.append((subdir, fixture))
    return result


# 参数化:每条 fixture 一个测试用例
ALL_FIXTURES = get_all_fixtures()
FIXTURE_IDS = [f"{s}/{f['id']}" for s, f in ALL_FIXTURES]


@pytest.mark.parametrize("subset,fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
class TestDeterministicEval:
    """对每条 fixture 运行确定性层评测。"""

    def test_verdict_matches_golden(self, subset, fixture):
        """verdict 必须与 golden_verdict 完全匹配。"""
        hunks = parse_diff(fixture["diff"])
        findings = run_deterministic_checks(hunks)
        actual_verdict = determine_verdict(findings)
        golden_verdict = Verdict(fixture["golden_verdict"])
        assert actual_verdict == golden_verdict, (
            f"{fixture['id']}: expected {golden_verdict.value}, "
            f"got {actual_verdict.value} "
            f"(findings: {[f.rule_id for f in findings]})"
        )

    def test_findings_match_golden(self, subset, fixture):
        """确定性 findings 必须与 golden_findings 匹配(precision/recall)。"""
        hunks = parse_diff(fixture["diff"])
        actual_findings = run_deterministic_checks(hunks)

        golden_raw = fixture.get("golden_findings", [])
        from cr_agent.core.models import Finding

        golden_findings = [
            Finding(
                rule_id=item["rule_id"],
                severity=Severity(item["severity"]),
                file=item.get("file"),
                line=item.get("line"),
                message=item.get("message", ""),
                suggestion=item.get("suggestion", ""),
                source="deterministic",
            )
            for item in golden_raw
        ]

        matches = match_findings(golden_findings, actual_findings)
        tp = sum(1 for m in matches if m.is_tp)
        fp = sum(1 for m in matches if m.is_fp)
        fn = sum(1 for m in matches if m.is_fn)

        # 确定性层要求 100% 匹配(无 LLM 随机性)
        assert fn == 0, (
            f"{fixture['id']}: {fn} golden finding(s) missed (FN). "
            f"Golden: {[f.rule_id for f in golden_findings]}, "
            f"Actual: {[f.rule_id for f in actual_findings]}"
        )

        # FP 仅在 clean/benign 集中报错(确定性层不应有假阳性)
        if subset == "benign":
            assert fp == 0, (
                f"{fixture['id']}: {fp} false positive(s) on benign PR. "
                f"Unexpected: {[m.actual.rule_id for m in matches if m.is_fp]}"
            )

    def test_no_hallucination(self, subset, fixture):
        """确定性层 finding 的 file 必须在 diff 中出现(无幻觉)。"""
        hunks = parse_diff(fixture["diff"])
        findings = run_deterministic_checks(hunks)

        diff_files = set()
        for line in fixture["diff"].splitlines():
            if line.startswith("+++ b/"):
                path = line[6:]
                if path != "/dev/null":
                    diff_files.add(path)
            elif line.startswith("--- a/"):
                path = line[6:]
                if path != "/dev/null":
                    diff_files.add(path)

        for f in findings:
            if f.file:
                assert f.file in diff_files, (
                    f"{fixture['id']}: finding file '{f.file}' "
                    f"not in diff files {diff_files}"
                )


class TestDeterministicMetrics:
    """确定性层整体指标测试。"""

    def test_golden_precision_100(self):
        """DS-Golden 确定性层 recall 必须 100%(不漏检)。"""
        fixtures = load_dataset("golden")
        if not fixtures:
            pytest.skip("no golden fixtures")

        total_fn = 0
        for fixture in fixtures:
            hunks = parse_diff(fixture["diff"])
            actual = run_deterministic_checks(hunks)
            from cr_agent.core.models import Finding
            golden = [
                Finding(
                    rule_id=item["rule_id"],
                    severity=Severity(item["severity"]),
                    file=item.get("file"),
                    line=item.get("line"),
                    message="",
                    suggestion="",
                    source="deterministic",
                )
                for item in fixture.get("golden_findings", [])
            ]
            matches = match_findings(golden, actual)
            fn = sum(1 for m in matches if m.is_fn)
            total_fn += fn

        assert total_fn == 0, f"Golden set has {total_fn} false negative(s)"

    def test_benign_precision_100(self):
        """DS-Benign 确定性层 0-finding 且 approve 占比必须 100%。"""
        fixtures = load_dataset("benign")
        if not fixtures:
            pytest.skip("no benign fixtures")

        for fixture in fixtures:
            hunks = parse_diff(fixture["diff"])
            findings = run_deterministic_checks(hunks)
            verdict = determine_verdict(findings)
            assert verdict == Verdict.APPROVE, (
                f"{fixture['id']}: benign PR got verdict={verdict.value} "
                f"with {len(findings)} finding(s)"
            )
            assert len(findings) == 0, (
                f"{fixture['id']}: benign PR has {len(findings)} finding(s): "
                f"{[f.rule_id for f in findings]}"
            )

    def test_injection_verdict_not_followed(self):
        """DS-Injection:确定性层 verdict 必须由代码决定,不受注入影响。"""
        fixtures = load_dataset("injection")
        if not fixtures:
            pytest.skip("no injection fixtures")

        for fixture in fixtures:
            hunks = parse_diff(fixture["diff"])
            findings = run_deterministic_checks(hunks)
            actual_verdict = determine_verdict(findings)
            golden_verdict = Verdict(fixture["golden_verdict"])
            assert actual_verdict == golden_verdict, (
                f"{fixture['id']}: injection PR verdict mismatch - "
                f"expected {golden_verdict.value}, got {actual_verdict.value}. "
                f"Injection may have affected deterministic layer (should not)."
            )

    def test_adversarial_zero_false_positive(self):
        """DS-Adversarial:确定性层必须 0 finding（对抗性用例都是安全写法）。"""
        fixtures = load_dataset("adversarial")
        if not fixtures:
            pytest.skip("no adversarial fixtures")

        for fixture in fixtures:
            hunks = parse_diff(fixture["diff"])
            findings = run_deterministic_checks(hunks)
            verdict = determine_verdict(findings)
            assert verdict == Verdict.APPROVE, (
                f"{fixture['id']}: adversarial PR got verdict={verdict.value} "
                f"with {len(findings)} finding(s)"
            )
            assert len(findings) == 0, (
                f"{fixture['id']}: adversarial PR has {len(findings)} finding(s): "
                f"{[f.rule_id for f in findings]}"
            )
