"""Trace ID propagation — follow one review across all log lines.

Unhappy path:
  3 reviews running in parallel. Logs are interleaved:
    [info] review started for PR #42
    [info] review started for PR #43
    [info] deterministic checks done for PR #42
    [info] LLM call failed for PR #43
    [info] LLM call done for PR #42
  Which LLM call failed? Which started first? Hard to tell.

With trace ID:
    [info] trace=abc123 review started for PR #42
    [info] trace=def456 review started for PR #43
    [info] trace=abc123 deterministic checks done
    [error] trace=def456 LLM call failed: rate limited
    [info] trace=abc123 LLM call done in 23s
  Now it's clear: trace=def456 (PR #43) had the LLM failure.

Production: use OpenTelemetry / Langfuse for distributed tracing.
Here: simple UUID injected into structlog contextvars.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

import structlog

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """Generate a new trace ID and bind it to the current context."""
    tid = uuid.uuid4().hex[:12]
    _trace_id.set(tid)
    structlog.contextvars.bind_contextvars(trace_id=tid)
    return tid


def get_trace_id() -> str:
    """Get the current trace ID."""
    return _trace_id.get()


def with_trace_id(func):
    """Decorator: generate a trace ID for a function call.

    Usage:
        @with_trace_id
        def run_review(diff, pr_info): ...
    """
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
