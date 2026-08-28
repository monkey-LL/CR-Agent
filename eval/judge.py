"""LLM-as-Judge 模块:评估 soft 维度(可操作性、清晰度、完整性)。

参照 cefen-backend.md 3.4.3:
  "soft 维度(可操作性/清晰度)用 LLM-as-judge(强模型评分,需交叉校验防偏差)"

参照 Anthropic 方法论:
  "让 LLM-as-judge 值得信任,需要与人类专家密切校准。
   先设计清晰、结构化的 rubric,再让彼此隔离的 LLM-as-judge 分别对每个维度评分。
   给 LLM 一个退路:如果信息不足,返回 Unknown。"

用法:
  python -m eval.judge --report eval/reports/latest.json --model DeepSeek-V4-Flash
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cr_agent.core.models import Finding, ReviewReport


@dataclass
class JudgeScore:
    """单条 finding 的 LLM-as-judge 评分结果。"""

    fixture_id: str
    finding_index: int
    rule_id: str
    operability: float  # 0-1: 修复建议是否可操作
    clarity: float  # 0-1: 问题描述是否清晰
    completeness: float  # 0-1: 是否遗漏了重要问题
    overall: float  # 三个维度的加权平均
    reasoning: str = ""


@dataclass
class JudgeReport:
    """LLM-as-judge 汇总报告。"""

    scores: list[JudgeScore] = field(default_factory=list)
    avg_operability: float = 0.0
    avg_clarity: float = 0.0
    avg_completeness: float = 0.0
    avg_overall: float = 0.0
    total_judged: int = 0

    def to_dict(self) -> dict:
        return {
            "total_judged": self.total_judged,
            "avg_operability": round(self.avg_operability, 3),
            "avg_clarity": round(self.avg_clarity, 3),
            "avg_completeness": round(self.avg_completeness, 3),
            "avg_overall": round(self.avg_overall, 3),
            "scores": [
                {
                    "fixture_id": s.fixture_id,
                    "finding_index": s.finding_index,
                    "rule_id": s.rule_id,
                    "operability": s.operability,
                    "clarity": s.clarity,
                    "completeness": s.completeness,
                    "overall": s.overall,
                    "reasoning": s.reasoning,
                }
                for s in self.scores
            ],
        }


JUDGE_PROMPT_TEMPLATE = """You are an expert code reviewer evaluator. Your task is to judge the quality of a code review finding.

## Context
- File: {file}
- Line: {line}
- Rule ID: {rule_id}
- Severity: {severity}
- Message: {message}
- Suggestion: {suggestion}

## Scoring Rubric (score 0.0 to 1.0 for each dimension)

### Operability (可操作性)
- 1.0: Suggestion is specific, actionable, and directly fixes the issue
- 0.5: Suggestion is generic but somewhat useful
- 0.0: Suggestion is missing, vague, or incorrect

### Clarity (清晰度)
- 1.0: Message clearly explains what the problem is and why it matters
- 0.5: Message identifies the problem but lacks explanation
- 0.0: Message is unclear or misleading

### Completeness (完整性)
- 1.0: Finding captures all important aspects of the issue
- 0.5: Finding captures the main issue but misses some nuances
- 0.0: Finding misses critical aspects

## Output Format
Respond with a JSON object:
```json
{{
  "operability": <float 0-1>,
  "clarity": <float 0-1>,
  "completeness": <float 0-1>,
  "reasoning": "<one sentence explanation>"
}}
```

If the finding is too vague to evaluate, return all scores as 0.0 and reasoning as "Unknown".
"""


def judge_finding(
    finding: Finding,
    fixture_id: str,
    finding_index: int,
    model_name: str | None = None,
) -> JudgeScore:
    """用 LLM 评判单条 finding 的质量。"""
    from langchain_openai import ChatOpenAI

    model = model_name or os.environ.get("CR_MODEL", "DeepSeek-V4-Flash")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("XITA_API_KEY")
    if not api_key:
        return JudgeScore(
            fixture_id=fixture_id,
            finding_index=finding_index,
            rule_id=finding.rule_id,
            operability=0.0,
            clarity=0.0,
            completeness=0.0,
            overall=0.0,
            reasoning="No API key available for LLM-as-judge",
        )

    llm = ChatOpenAI(
        model=model,
        temperature=0.0,
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=api_key,
    )

    prompt = JUDGE_PROMPT_TEMPLATE.format(
        file=finding.file or "unknown",
        line=finding.line or "unknown",
        rule_id=finding.rule_id,
        severity=finding.severity.value,
        message=finding.message,
        suggestion=finding.suggestion,
    )

    response = llm.invoke(prompt)
    content = response.content if hasattr(response, "content") else str(response)

    # 解析 JSON
    import re

    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if not json_match:
        first = content.find("{")
        last = content.rfind("}")
        if first != -1 and last != -1:
            json_str = content[first:last + 1]
        else:
            json_str = "{}"
    else:
        json_str = json_match.group(1)

    try:
        data = json.loads(json_str)
        operability = float(data.get("operability", 0))
        clarity = float(data.get("clarity", 0))
        completeness = float(data.get("completeness", 0))
        reasoning = data.get("reasoning", "")
    except (json.JSONDecodeError, ValueError):
        operability = clarity = completeness = 0.0
        reasoning = "Failed to parse LLM judge response"

    overall = 0.4 * operability + 0.3 * clarity + 0.3 * completeness

    return JudgeScore(
        fixture_id=fixture_id,
        finding_index=finding_index,
        rule_id=finding.rule_id,
        operability=min(1.0, max(0.0, operability)),
        clarity=min(1.0, max(0.0, clarity)),
        completeness=min(1.0, max(0.0, completeness)),
        overall=overall,
        reasoning=reasoning,
    )


def judge_report(
    report_data: dict,
    model_name: str | None = None,
) -> JudgeReport:
    """评判整个评测报告中所有 finding 的质量。"""
    judge_report = JudgeReport()

    # 从评测报告中提取 findings
    results = report_data.get("results", [])
    total_findings = 0

    for result in results:
        fixture_id = result.get("fixture_id", "unknown")
        # 这里我们只评判实际产出的 findings(需要从评测报告中获取)
        # 目前从 per-fixture results 中没有完整的 finding 列表
        # 实际使用时需要从 graph.invoke 的完整输出中提取
        pass

    # 如果直接有 ReviewReport,评判其中的 findings
    if "findings" in report_data:
        for i, f_data in enumerate(report_data["findings"]):
            finding = Finding(**f_data) if isinstance(f_data, dict) else f_data
            score = judge_finding(finding, report_data.get("id", "unknown"), i, model_name)
            judge_report.scores.append(score)
            total_findings += 1

    # 计算平均值
    if judge_report.scores:
        n = len(judge_report.scores)
        judge_report.avg_operability = sum(s.operability for s in judge_report.scores) / n
        judge_report.avg_clarity = sum(s.clarity for s in judge_report.scores) / n
        judge_report.avg_completeness = sum(s.completeness for s in judge_report.scores) / n
        judge_report.avg_overall = sum(s.overall for s in judge_report.scores) / n
        judge_report.total_judged = n

    return judge_report


def main():
    parser = argparse.ArgumentParser(description="LLM-as-Judge for CR Agent")
    parser.add_argument("--report", required=True, help="评测报告 JSON 文件路径")
    parser.add_argument("--model", default=None, help="评判模型名称")
    parser.add_argument("--output", default=None, help="输出路径")

    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report_data = json.load(f)

    print(f"Running LLM-as-judge on {args.report}...")
    result = judge_report(report_data, model_name=args.model)

    output_path = args.output or args.report.replace(".json", "_judged.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)

    print(f"\nJudge Report:")
    print(f"  Total judged: {result.total_judged}")
    print(f"  Avg Operability: {result.avg_operability:.3f}")
    print(f"  Avg Clarity: {result.avg_clarity:.3f}")
    print(f"  Avg Completeness: {result.avg_completeness:.3f}")
    print(f"  Avg Overall: {result.avg_overall:.3f}")
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
