"""FastAPI webhook server with idempotency, trace ID, and memory integration.

Production features:
  - HMAC verification (prevent forged webhooks)
  - Idempotency store (prevent duplicate reviews from webhook redelivery)
  - Trace ID (follow one review across all log lines)
  - Memory system (learn from past reviews)
  - Background tasks (return 200 fast, review runs async)
"""

from __future__ import annotations

import os
import time

from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks

from cr_agent.observability.logger import logger
from cr_agent.observability.tracing import new_trace_id, get_trace_id
from cr_agent.observability.idempotency import get_idempotency_store
from cr_agent.github.client import (
    verify_webhook_signature, parse_webhook_payload,
    get_pr_diff, get_pr_info, post_pr_comment,
)
from cr_agent.agent.memory import save_review_memory, build_memory_context

app = FastAPI(title="CR Agent Webhook Server")

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")
_processed_deliveries: set[str] = set()
_MAX_DEDUP = 1000


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
):
    body = await request.body()

    # 1. HMAC verification
    if not verify_webhook_signature(body, x_hub_signature_256, WEBHOOK_SECRET):
        logger.warning("webhook.signature_failed", delivery=x_github_delivery)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Deduplication by delivery ID
    if x_github_delivery in _processed_deliveries:
        logger.info("webhook.duplicate", delivery=x_github_delivery)
        return {"status": "duplicate", "delivery": x_github_delivery}
    _processed_deliveries.add(x_github_delivery)
    if len(_processed_deliveries) > _MAX_DEDUP:
        _processed_deliveries.clear()
        _processed_deliveries.add(x_github_delivery)

    # 3. Parse payload
    pr_data = parse_webhook_payload(body)
    if pr_data is None:
        return {"status": "ignored", "event": x_github_event}
    if pr_data["action"] not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "action": pr_data["action"]}

    repo = pr_data["repo"]
    pr_number = pr_data["number"]

    # 4. Idempotency check — prevent duplicate/concurrent reviews
    trace_id = new_trace_id()
    idempotency = get_idempotency_store()
    if not idempotency.try_acquire(repo, pr_number, trace_id):
        logger.info("webhook.idempotency_rejected", repo=repo, pr=pr_number, trace=trace_id)
        return {"status": "rejected", "reason": "review_already_in_progress_or_recent", "trace": trace_id}

    logger.info("webhook.received", event=x_github_event, repo=repo, pr=pr_number, trace=trace_id)

    # 5. Schedule review in background
    background_tasks.add_task(_run_review, pr_data, trace_id)
    return {"status": "accepted", "pr": pr_number, "repo": repo, "trace": trace_id}


@app.get("/health")
async def health():
    return {"status": "ok"}


def _run_review(pr_data: dict, trace_id: str):
    """Background task with idempotency lifecycle + memory."""
    from cr_agent.agent.graph import build_graph
    from cr_agent.core.models import ReviewReport
    from cr_agent.observability.tracing import new_trace_id

    new_trace_id(trace_id)  # Re-bind trace ID for background context
    start_time = time.time()
    repo = pr_data["repo"]
    pr_number = pr_data["number"]
    idempotency = get_idempotency_store()

    try:
        logger.info("review.started", repo=repo, pr=pr_number)

        diff = get_pr_diff(pr_number, repo, GITHUB_TOKEN)
        pr_info = {
            "repo": repo, "number": pr_number,
            "title": pr_data.get("title", ""),
            "author": pr_data.get("author", ""),
            "base": pr_data.get("base", ""),
            "head": pr_data.get("head", ""),
        }

        # Load repository memory for context
        memory_context = build_memory_context(repo)

        model = os.environ.get("CR_MODEL", "DeepSeek-V4-Flash")
        graph = build_graph(model_name=model)
        result = graph.invoke({"diff": diff, "pr_info": pr_info})

        report_data = result.get("report")
        if report_data:
            report = ReviewReport(**report_data)
            markdown = report.to_markdown()

            # Post comment
            success = post_pr_comment(pr_number, repo, markdown, GITHUB_TOKEN)

            # Save to memory
            save_review_memory(repo, pr_number, report_data.get("findings", []), report.verdict.value)

            elapsed = time.time() - start_time
            logger.info(
                "review.complete", repo=repo, pr=pr_number,
                verdict=report.verdict.value, findings=len(report.findings),
                elapsed_s=round(elapsed, 1), comment_posted=success,
            )
            idempotency.release(repo, pr_number, success=True)
        else:
            logger.error("review.no_report", repo=repo, pr=pr_number)
            idempotency.release(repo, pr_number, success=False)

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error("review.failed", repo=repo, pr=pr_number, error=str(e), elapsed_s=round(elapsed, 1))
        idempotency.release(repo, pr_number, success=False)
