"""代码审查数据模型——发现、报告、结论。

学习重点：
  - Pydantic v2 模型用于结构化数据
  - 严重度层级与审查结论的映射规则
  - Schema 版本化，保证契约稳定性
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """发现严重度级别，按影响从高到低排列。"""

    BLOCKER = "blocker"  # 必须修复：安全漏洞、数据丢失、崩溃
    MAJOR = "major"      # 应该修复：逻辑错误、缺失异常处理
    MINOR = "minor"      # 可选修复：风格、命名、可维护性
    INFO = "info"        # 观察记录，无需操作


class Verdict(str, Enum):
    """整体审查结论。"""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    BLOCK = "block"


class Confidence(str, Enum):
    """审查者对此发现的置信度。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    """单条代码审查发现。"""

    rule_id: str
    severity: Severity
    file: str | None = None
    line: int | None = None
    message: str
    suggestion: str
    confidence: Confidence = Confidence.MEDIUM
    source: Literal["deterministic", "llm"] = "deterministic"

    def markdown(self) -> str:
        """渲染为 markdown 格式，用于 PR 评论。"""
        return (
            f"#### [{self.severity.value}] {self.file or 'unknown'}:{self.line or '?'}\n"
            f"**Issue**: {self.message}\n"
            f"**Suggestion**: {self.suggestion}\n"
            f"**Confidence**: {self.confidence.value}\n"
        )


class DiffMetrics(BaseModel):
    """diff 统计信息。"""

    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0


class StaticAnalysisResult(BaseModel):
    """单次静态分析工具（lint、type-check）的运行结果。"""

    tool: str
    status: Literal["pass", "fail", "skipped"] = "skipped"
    issues: int = 0
    output: str | None = None


class ReviewReport(BaseModel):
    """完整审查报告，会作为 PR 评论发布。"""

    schema_version: str = "cr-agent.report.v1"
    verdict: Verdict
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    static_analysis: list[StaticAnalysisResult] = Field(default_factory=list)
    metrics: DiffMetrics = Field(default_factory=DiffMetrics)

    def to_markdown(self) -> str:
        """将完整报告渲染为 markdown。"""
        lines = ["## Code Review Report\n", f"**Verdict**: {self.verdict.value}\n"]
        lines.append(f"### Summary\n{self.summary}\n")

        if self.findings:
            lines.append("### Findings\n")
            for f in self.findings:
                lines.append(f.markdown())
        else:
            lines.append("### Findings\nNo issues found.\n")

        if self.static_analysis:
            lines.append("### Static Analysis\n")
            lines.append("| Tool | Status | Issues |")
            lines.append("|------|--------|--------|")
            for sa in self.static_analysis:
                lines.append(f"| {sa.tool} | {sa.status} | {sa.issues} |")
            lines.append("")

        lines.append("### Metrics")
        lines.append(f"- Files reviewed: {self.metrics.files_changed}")
        lines.append(f"- Lines changed: +{self.metrics.lines_added} / -{self.metrics.lines_removed}")
        lines.append(f"- Findings: {len(self.findings)} total")
        return "\n".join(lines)


def determine_verdict(findings: list[Finding]) -> Verdict:
    """根据发现列表判定审查结论。

    学习重点：这是将发现映射到行动的决策逻辑。

    verdict 由代码根据 findings 的严重度自动判定，不由 LLM 输出。
    确定性来源（source="deterministic"）的 finding 可信度高，1 条即触发。
    LLM 来源（source="llm"）的 finding 可能存在假阳性，blocker/major 需 ≥2 条才触发。
    """
    det_blockers = [f for f in findings if f.severity == Severity.BLOCKER and f.source == "deterministic"]
    llm_blockers = [f for f in findings if f.severity == Severity.BLOCKER and f.source == "llm"]
    det_majors = [f for f in findings if f.severity == Severity.MAJOR and f.source == "deterministic"]
    llm_majors = [f for f in findings if f.severity == Severity.MAJOR and f.source == "llm"]

    if det_blockers:
        return Verdict.BLOCK
    if len(llm_blockers) >= 2:
        return Verdict.BLOCK
    if det_majors:
        return Verdict.REQUEST_CHANGES
    if len(llm_majors) >= 2:
        return Verdict.REQUEST_CHANGES
    return Verdict.APPROVE
