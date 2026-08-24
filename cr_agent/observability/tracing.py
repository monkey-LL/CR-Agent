"""Trace ID 传播 — 通过所有日志行追踪一次完整的审查过程。

异常场景：
  3 个审查并行运行。日志交错出现：
    [info] review started for PR #42
    [info] review started for PR #43
    [info] deterministic checks done for PR #42
    [info] LLM call failed for PR #43
    [info] LLM call done for PR #42
  哪个 LLM 调用失败了？哪个先开始的？很难分辨。

有了 trace ID：
    [info] trace=abc123 review started for PR #42
    [info] trace=def456 review started for PR #43
    [info] trace=abc123 deterministic checks done
    [error] trace=def456 LLM call failed: rate limited
    [info] trace=abc123 LLM call done in 23s
  现在很清楚了：trace=def456（PR #43）的 LLM 调用失败了。

生产环境：使用 OpenTelemetry / Langfuse 进行分布式追踪。
这里：简单的 UUID 注入到 structlog contextvars 中。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from functools import wraps

import structlog

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """生成一个新的 trace ID 并绑定到当前上下文。"""
    tid = uuid.uuid4().hex[:12]
    _trace_id.set(tid)
    structlog.contextvars.bind_contextvars(trace_id=tid)
    return tid


def set_trace_id(tid: str) -> None:
    """将一个已存在的 trace ID 绑定到当前上下文(不生成新 ID)。

    用于后台任务把请求阶段已生成的 trace ID 重新绑定到后台上下文,
    保持整条审查链路共用同一个 trace,而不是再生成一个新 ID。
    """
    _trace_id.set(tid)
    structlog.contextvars.bind_contextvars(trace_id=tid)


def get_trace_id() -> str:
    """获取当前的 trace ID。"""
    return _trace_id.get()


def with_trace_id(func):
    """装饰器：为函数调用生成一个 trace ID。

    用法：
        @with_trace_id
        def run_review(diff, pr_info): ...
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        tid = new_trace_id()
        logger = structlog.get_logger().bind(trace_id=tid)
        logger.info("trace.started")
        try:
            result = func(*args, **kwargs)
            logger.info("trace.completed")
            return result
        except Exception as e:
            logger.error("trace.failed", error=str(e))
            raise
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")
    return wrapper
