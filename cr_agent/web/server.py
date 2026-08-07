"""Web UI API server — provides REST endpoints for the frontend.

Learning focus:
  - FastAPI serving both API and static files
  - How to bridge web UI ↔ agent graph
  - Server-Sent Events (SSE) for streaming review progress

Endpoints:
  GET  /              → Web UI (HTML)
  POST /api/review    → Trigger a review (returns report)
  GET  /api/health    → Health check
  GET  /api/rules     → List deterministic rules
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from cr_agent.observability.logger import logger
from cr_agent.core.rules_engine import DETERMINISTIC_RULES
from cr_agent.core.diff_parser import parse_diff, compute_metrics
from cr_agent.core.models import (
    ReviewReport, DiffMetrics, determine_verdict,
    Finding, Severity, Confidence,
)

app = FastAPI(title="CR Agent Web UI")

WEB_DIR = Path(__file__).parent


class ReviewRequest(BaseModel):
    """Request body for POST /api/review."""

    diff: str
    pr_title: str = "Untitled PR"
    pr_author: str = "unknown"
    use_llm: bool = False
    model: str = "DeepSeek-V4-Flash"


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web UI."""
    html_path = WEB_DIR / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/rules")
async def list_rules():
    """List all deterministic rules (for the UI to display)."""
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
    """Run a code review and return the report.

    If use_llm=False (default), only runs deterministic checks — fast, free.
    If use_llm=True, runs the full LangGraph agent with LLM semantic analysis.
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
        # Full LLM review
        try:
            from cr_agent.agent.graph import build_graph
            from cr_agent.observability.metrics import get_metrics
            model = req.model or os.environ.get("CR_MODEL", "DeepSeek-V4-Flash")
            graph = build_graph(model_name=model)
            result = graph.invoke({"diff": req.diff, "pr_info": pr_info})
            report = ReviewReport(**result["report"])
            review_metrics = get_metrics()
        except Exception as e:
            logger.error("LLM review failed, falling back to deterministic", error=str(e))
            report = _deterministic_only(req.diff, pr_info)
            review_metrics = None
    else:
        report = _deterministic_only(req.diff, pr_info)
        review_metrics = None

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
    }


def _deterministic_only(diff: str, pr_info: dict) -> ReviewReport:
    """Run only deterministic checks (no LLM)."""
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
