"""Metrics collector — tracks per-phase timing and token usage.

Learning focus:
  - Why measure per-phase timing: identify which step is slow (LLM vs tools vs parsing)
  - Token tracking: understand the cost of each LLM call
  - ContextVar for per-request isolation: concurrent reviews don't clobber each other

The metrics flow:
  1. graph.py calls record_phase() around each node
  2. graph.py calls record_token_usage() after each LLM call
  3. web/server.py reads get_metrics() and returns it to the frontend
  4. index.html renders the metrics panel
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from cr_agent.observability.logger import logger


@dataclass
class PhaseTiming:
    """Timing for a single graph phase."""
    name: str
    elapsed_ms: float = 0.0


@dataclass
class TokenUsage:
    """Token usage for a single LLM call."""
    call_index: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""

    @property
    def estimated_cost_usd(self) -> float:
        """Rough cost estimate for DeepSeek-V4-Flash pricing (~$0.14/M input, ~$0.28/M output)."""
        return round(
            self.prompt_tokens * 0.14 / 1_000_000
            + self.completion_tokens * 0.28 / 1_000_000,
            6,
        )


@dataclass
class ReviewMetrics:
    """All metrics for a single review run."""
    phases: list[PhaseTiming] = field(default_factory=list)
    token_usages: list[TokenUsage] = field(default_factory=list)
    total_elapsed_ms: float = 0.0
    llm_call_count: int = 0
    tool_call_count: int = 0
    iteration_count: int = 0

    def to_dict(self) -> dict:
        """Serialize for API response."""
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


# ContextVar: each request gets its own metrics instance (thread-safe for async)
_current_metrics: ContextVar[ReviewMetrics | None] = ContextVar("_current_metrics", default=None)


def init_metrics() -> ReviewMetrics:
    """Initialize a fresh metrics instance for this request. Call at the start of a review."""
    metrics = ReviewMetrics()
    _current_metrics.set(metrics)
    return metrics


def get_metrics() -> ReviewMetrics | None:
    """Get the current request's metrics, if any."""
    return _current_metrics.get()


def record_phase(name: str, start_time: float) -> None:
    """Record the elapsed time of a phase. Call with time.time() captured before the phase."""
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
    """Record token usage from an LLM response."""
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
    """Increment the tool call counter."""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.tool_call_count += 1


def record_iteration() -> None:
    """Increment the iteration counter."""
    metrics = _current_metrics.get()
    if metrics is None:
        return
    metrics.iteration_count += 1


def finalize_metrics() -> None:
    """Mark metrics as complete and log summary."""
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
