"""指标收集器 — 追踪各阶段耗时和 token 使用量。

学习重点：
  - 为什么需要按阶段测量耗时：识别哪个步骤慢（LLM vs 工具 vs 解析）
  - Token 追踪：了解每次 LLM 调用的成本
  - 使用 ContextVar 实现请求级隔离：并发审查不会互相干扰

指标流转流程：
  1. graph.py 在每个节点周围调用 record_phase()
  2. graph.py 在每次 LLM 调用后调用 record_token_usage()
  3. web/server.py 读取 get_metrics() 并返回给前端
  4. index.html 渲染指标面板
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from cr_agent.observability.logger import logger


@dataclass
class PhaseTiming:
    """单个 graph 阶段的耗时记录。"""
    name: str
    elapsed_ms: float = 0.0


@dataclass
class TokenUsage:
    """单次 LLM 调用的 token 使用量。"""
    call_index: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""

    @property
    def estimated_cost_usd(self) -> float:
        """基于 DeepSeek-V4-Flash 定价的粗略成本估算（输入约 $0.14/百万 token，输出约 $0.28/百万 token）。"""
        return round(
            self.prompt_tokens * 0.14 / 1_000_000
            + self.completion_tokens * 0.28 / 1_000_000,
            6,
        )


@dataclass
class ReviewMetrics:
    """单次审查运行的所有指标。"""
    phases: list[PhaseTiming] = field(default_factory=list)
    token_usages: list[TokenUsage] = field(default_factory=list)
    total_elapsed_ms: float = 0.0
    llm_call_count: int = 0
    tool_call_count: int = 0
    iteration_count: int = 0

    def to_dict(self) -> dict:
        """序列化为 API 响应格式。"""
        return {
            "phases": [
                {"name": p.name, "elapsed_ms": round(p.elapsed_ms, 1)}
                for p in self.phases
            ],
            "tokens": {
                "total_prompt": sum(t.prompt_tokens for t in self.token_usages),
                "total_completion": sum(t.completion_tokens for t in self.token_usages),
                "total_tokens": sum(t.total_tokens for t in self.token_usages),
                "estimated_cost_usd": sum(t.estimated_cost_usd for t in self.token_usages),
                "per_call": [
                    {
                        "call_index": t.call_index,
                        "prompt_tokens": t.prompt_tokens,
                        "completion_tokens": t.completion_tokens,
                        "total_tokens": t.total_tokens,
                        "model": t.model,
                    }
                    for t in self.token_usages
                ],
            },
            "llm_call_count": self.llm_call_count,
            "tool_call_count": self.tool_call_count,
            "iteration_count": self.iteration_count,
            "total_elapsed_ms": round(self.total_elapsed_ms, 1),
        }


# ContextVar：每个请求拥有独立的指标实例（对异步是线程安全的）
_current_metrics: ContextVar[ReviewMetrics | None] = ContextVar("_current_metrics", default=None)


def init_metrics() -> ReviewMetrics:
    """为此请求初始化一个新的指标实例。在审查开始时调用。"""
    metrics = ReviewMetrics()
    _current_metrics.set(metrics)
    return metrics


def get_metrics() -> ReviewMetrics | None:
    """获取当前请求的指标实例，如果存在的话。"""
    return _current_metrics.get()


def record_phase(name: str, start_time: float) -> None:
    """记录一个阶段的耗时。传入在阶段开始前捕获的 time.time() 值。"""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    elapsed_ms = (time.time() - start_time) * 1000
    metrics.phases.append(PhaseTiming(name=name, elapsed_ms=elapsed_ms))
    logger.info("metrics.phase", phase=name, elapsed_ms=round(elapsed_ms, 1))


def record_token_usage(
    call_index: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    model: str,
) -> None:
    """记录 LLM 响应的 token 使用量。"""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    usage = TokenUsage(
        call_index=call_index,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        model=model,
    )
    metrics.token_usages.append(usage)
    metrics.llm_call_count += 1
    logger.info(
        "metrics.tokens",
        call=call_index,
        prompt=prompt_tokens,
        completion=completion_tokens,
        total=total_tokens,
    )


def record_tool_call() -> None:
    """递增工具调用计数器。"""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.tool_call_count += 1


def record_iteration() -> None:
    """递增迭代计数器。"""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.iteration_count += 1


def finalize_metrics() -> None:
    """标记指标为已完成并记录摘要。"""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    total_tokens = sum(t.total_tokens for t in metrics.token_usages)
    logger.info(
        "metrics.summary",
        phases=len(metrics.phases),
        llm_calls=metrics.llm_call_count,
        tool_calls=metrics.tool_call_count,
        iterations=metrics.iteration_count,
        total_tokens=total_tokens,
    )
