"""FastAPI webhook 服务器，支持幂等性、trace ID 和记忆系统集成。

生产环境特性：
  - HMAC 验证（防止伪造的 webhook）
  - 幂等性存储（防止 webhook 重复投递导致的重复审查）
  - Trace ID（跨所有日志行追踪一次完整的审查过程）
  - 记忆系统（从过往审查中学习）
  - 后台任务（快速返回 200，审查异步执行）
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from cr_agent.agent.memory import build_memory_context, save_review_memory
from cr_agent.github.client import (
    get_pr_diff,
    parse_webhook_payload,
    post_pr_comment,
    verify_webhook_signature,
)
from cr_agent.observability.idempotency import get_idempotency_store
from cr_agent.observability.logger import logger
from cr_agent.observability.tracing import new_trace_id

app = FastAPI(title="CR Agent Webhook Server")

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")
_processed_deliveries: OrderedDict[str, None] = OrderedDict()
_MAX_DEDUP = 1000

if not WEBHOOK_SECRET:
    logger.warning(
        "GITHUB_WEBHOOK_SECRET not set — all webhooks will be rejected (401). "
        "Set it to your GitHub webhook secret to enable webhook reviews."
    )


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
):
    body = await request.body()

    # 1. HMAC 验证
    if not verify_webhook_signature(body, x_hub_signature_256, WEBHOOK_SECRET):
        logger.warning("webhook.signature_failed", delivery=x_github_delivery)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. 按 delivery ID 去重（LRU 淘汰）
    if x_github_delivery in _processed_deliveries:
        _processed_deliveries.move_to_end(x_github_delivery)
        logger.info("webhook.duplicate", delivery=x_github_delivery)
        return {"status": "duplicate", "delivery": x_github_delivery}
    _processed_deliveries[x_github_delivery] = None
    if len(_processed_deliveries) > _MAX_DEDUP:
        _processed_deliveries.popitem(last=False)  # 移除最旧的条目

    # 3. 解析负载
    pr_data = parse_webhook_payload(body)
    if pr_data is None:
        return {"status": "ignored", "event": x_github_event}
    if pr_data["action"] not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "action": pr_data["action"]}

    repo = pr_data["repo"]
    pr_number = pr_data["number"]

    # 4. 幂等性检查 — 防止重复/并发审查
    trace_id = new_trace_id()
    idempotency = get_idempotency_store()
    if not idempotency.try_acquire(repo, pr_number, trace_id):
        logger.info("webhook.idempotency_rejected", repo=repo, pr=pr_number, trace=trace_id)
        return {"status": "rejected", "reason": "review_already_in_progress_or_recent", "trace": trace_id}

    logger.info("webhook.received", event=x_github_event, repo=repo, pr=pr_number, trace=trace_id)

    # 5. 在后台调度审查任务
    background_tasks.add_task(_run_review, pr_data, trace_id)
    return {"status": "accepted", "pr": pr_number, "repo": repo, "trace": trace_id}


@app.get("/health")
async def health():
    return {"status": "ok"}


def _run_review(pr_data: dict, trace_id: str):
    """后台任务，包含幂等性生命周期管理和记忆系统。"""
    from cr_agent.agent.graph import build_graph
    from cr_agent.core.models import ReviewReport
    from cr_agent.observability.tracing import set_trace_id

    set_trace_id(trace_id)  # 为后台上下文重新绑定 trace ID(不生成新 ID)
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

        # 加载仓库记忆作为上下文
        memory_context = build_memory_context(repo)

        model = os.environ.get("CR_MODEL", "DeepSeek-V4-Flash")
        graph = build_graph(model_name=model)
        result = graph.invoke({
            "diff": diff,
            "pr_info": pr_info,
            "memory_context": memory_context,
        })

        report_data = result.get("report")
        if report_data:
            report = ReviewReport(**report_data)
            markdown = report.to_markdown()

            # 发表评论
            success = post_pr_comment(pr_number, repo, markdown, GITHUB_TOKEN)

            # 保存到记忆
            save_review_memory(repo, pr_number, report_data.get("findings", []), report.verdict.value, trace_id=trace_id)

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
