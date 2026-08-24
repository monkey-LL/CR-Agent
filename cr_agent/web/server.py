"""Web UI API 服务器 — 为前端提供 REST 端点。

学习重点:
  - FastAPI 同时提供 API 和静态文件服务
  - 如何桥接 Web UI ↔ agent graph
  - 使用 Server-Sent Events (SSE) 流式传输审查进度

端点:
  GET  /              → Web UI (HTML)
  POST /api/review    → 触发代码审查（返回报告）
  GET  /api/health    → 健康检查
  GET  /api/rules     → 列出确定性规则
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from cr_agent.core.diff_parser import compute_metrics, parse_diff
from cr_agent.core.models import (
    DiffMetrics,
    ReviewReport,
    determine_verdict,
)
from cr_agent.core.rules_engine import DETERMINISTIC_RULES
from cr_agent.observability.logger import logger

app = FastAPI(title="CR Agent Web UI")

WEB_DIR = Path(__file__).parent


class ReviewRequest(BaseModel):
    """POST /api/review 的请求体。"""

    diff: str
    pr_title: str = "Untitled PR"
    pr_author: str = "unknown"
    use_llm: bool = False
    model: str = "DeepSeek-V4-Flash"


@app.get("/", response_class=HTMLResponse)
async def index():
    """提供 Web UI 页面。"""
    html_path = WEB_DIR / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/rules")
async def list_rules():
    """列出所有确定性规则（供 UI 展示）。"""
    return [
        {
            "rule_id": r["rule_id"],
            "severity": r["severity"].value,
            "message": r["message"],
        }
        for r in DETERMINISTIC_RULES
    ]


@app.post("/api/review")
async def review(req: ReviewRequest):
    """执行代码审查并返回报告。

    如果 use_llm=False（默认），仅运行确定性检查 — 快速、免费。
    如果 use_llm=True，运行完整的 LangGraph agent 进行 LLM 语义分析。
    """
    start = time.time()

    if not req.diff.strip():
        return JSONResponse(
            {"error": "Empty diff provided"},
            status_code=400,
        )

    pr_info = {
        "repo": "web-ui",
        "number": 0,
        "title": req.pr_title,
        "author": req.pr_author,
        "base": "",
        "head": "",
    }

    if req.use_llm:
        # 完整 LLM 审查
        try:
            from cr_agent.agent.graph import build_graph
            from cr_agent.observability.metrics import get_metrics
            model = req.model or os.environ.get("CR_MODEL", "DeepSeek-V4-Flash")
            graph = build_graph(model_name=model)
            result = graph.invoke({"diff": req.diff, "pr_info": pr_info, "memory_context": ""})
            report = ReviewReport(**result["report"])
            review_metrics = get_metrics()
            degraded = False
        except Exception as e:
            logger.error("LLM review failed, falling back to deterministic", error=str(e))
            report = _deterministic_only(req.diff, pr_info)
            report.summary += " ⚠️ LLM 审查失败，已降级为纯确定性规则检查，可能遗漏语义级问题。"
            review_metrics = None
            degraded = True
    else:
        report = _deterministic_only(req.diff, pr_info)
        review_metrics = None
        degraded = False

    elapsed = time.time() - start
    logger.info("review.complete", use_llm=req.use_llm, findings=len(report.findings), elapsed=round(elapsed, 2))

    return {
        "verdict": report.verdict.value,
        "summary": report.summary,
        "findings": [f.model_dump() for f in report.findings],
        "metrics": report.metrics.model_dump(),
        "markdown": report.to_markdown(),
        "elapsed_seconds": round(elapsed, 2),
        "review_metrics": review_metrics.to_dict() if review_metrics else None,
        "degraded": degraded,
    }


def _deterministic_only(diff: str, pr_info: dict) -> ReviewReport:
    """仅运行确定性检查（不使用 LLM）。"""
    from cr_agent.core.rules_engine import run_deterministic_checks

    hunks = parse_diff(diff)
    findings = run_deterministic_checks(hunks)
    files_changed, added, removed = compute_metrics(hunks)
    verdict = determine_verdict(findings)

    if not findings:
        summary = "No issues found by deterministic checks."
    else:
        summary = f"Deterministic review found {len(findings)} issue(s)."

    return ReviewReport(
        verdict=verdict,
        summary=summary,
        findings=findings,
        metrics=DiffMetrics(files_changed=files_changed, lines_added=added, lines_removed=removed),
    )
