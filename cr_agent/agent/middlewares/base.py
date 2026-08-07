"""Middleware base class and chain executor.

Architecture:
  Each middleware implements 4 optional hooks:
    - before_model(state): called before LLM invocation. Can modify messages,
      inject context, or block the call entirely.
    - after_model(state, response): called after LLM responds. Can inspect
      tool calls, modify the response, or force finalization.
    - before_tool(state, tool_call): called before executing a tool. Can
      block the tool call (e.g., loop detection, authorization).
    - after_tool(state, tool_result): called after tool returns. Can
      truncate output, mask secrets, log results.

  The chain executes hooks in order. If any middleware returns a non-None
  result from before_model/before_tool, it short-circuits (skips remaining
  middleware and the actual call).

Production vs Demo:
  Demo doesn't need middleware — everything works on happy path.
  Production needs middleware because:
  - LLM input might contain injection (InputSanitization)
  - LLM might loop forever (LoopDetection)
  - Tool output might explode context (ToolOutputBudget)
  - Token consumption might exceed budget (TokenBudget)
  - Tool might fail (ToolErrorHandling → graceful degradation)
  - Context might grow unbounded (ContextCompression)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class MiddlewareContext:
    """Shared context passed through the middleware chain.

    This is NOT the same as AgentState. This is per-invocation metadata
    that middleware uses to track things across hooks:
    - iteration count (for loop detection)
    - tool call history (for dedup detection)
    - token usage (for budget enforcement)
    - trace_id (for observability)
    """
    iteration: int = 0
    tool_call_history: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    trace_id: str = ""
    forced_finalize: bool = False
    blocked_tools: set[str] = field(default_factory=set)


class Middleware:
    """Base class for all middleware. Override hooks as needed."""

    def before_model(self, state: dict, ctx: MiddlewareContext) -> dict | None:
        """Called before LLM invocation. Return modified state or None to pass through."""
        return None

    def after_model(self, state: dict, response: Any, ctx: MiddlewareContext) -> Any | None:
        """Called after LLM responds. Return modified response or None to pass through."""
        return None

    def before_tool(self, state: dict, tool_call: dict, ctx: MiddlewareContext) -> dict | None:
        """Called before tool execution. Return error dict to block, None to allow."""
        return None

    def after_tool(self, state: dict, tool_result: str, ctx: MiddlewareContext) -> str | None:
        """Called after tool returns. Return modified result or None to pass through."""
        return None

    @property
    def name(self) -> str:
        return self.__class__.__name__


class MiddlewareChain:
    """Executes middleware hooks in registration order.

    Order is critical:
      1. InputSanitization (first — neutralize before LLM sees it)
      2. ContextCompression (compress before sending to LLM)
      3. LoopDetection (check before tool execution)
      4. ToolErrorHandling (catch tool failures)
      5. ToolOutputBudget (truncate after tool returns)
      6. TokenBudget (last — check total consumption)
    """

    def __init__(self, middlewares: list[Middleware] | None = None):
        self.middlewares = middlewares or []
        self.ctx = MiddlewareContext()

    def add(self, middleware: Middleware) -> "MiddlewareChain":
        self.middlewares.append(middleware)
        return self

    def run_before_model(self, state: dict) -> dict:
        """Run all before_model hooks. Returns possibly-modified state."""
        for mw in self.middlewares:
            result = mw.before_model(state, self.ctx)
            if result is not None:
                logger.debug(f"Middleware {mw.name} modified state in before_model")
                state = result
        return state

    def run_after_model(self, state: dict, response: Any) -> Any:
        """Run all after_model hooks. Returns possibly-modified response."""
        for mw in self.middlewares:
            result = mw.after_model(state, response, self.ctx)
            if result is not None:
                response = result
        return response

    def run_before_tool(self, state: dict, tool_call: dict) -> dict | None:
        """Run all before_tool hooks. Returns error dict to block, None to allow."""
        for mw in self.middlewares:
            result = mw.before_tool(state, tool_call, self.ctx)
            if result is not None:
                logger.info(f"Middleware {mw.name} blocked tool {tool_call.get('name')}")
                return result
        return None

    def run_after_tool(self, state: dict, tool_result: str) -> str:
        """Run all after_tool hooks. Returns possibly-modified result."""
        for mw in self.middlewares:
            result = mw.after_tool(state, tool_result, self.ctx)
            if result is not None:
                tool_result = result
        return tool_result

    def should_finalize(self) -> bool:
        """Check if any middleware forced finalization."""
        return self.ctx.forced_finalize


def build_default_chain() -> MiddlewareChain:
    """Build the default production middleware chain for CR Agent.

    Order matters! See MiddlewareChain docstring.
    """
    from cr_agent.agent.middlewares.input_sanitization import InputSanitizationMiddleware
    from cr_agent.agent.middlewares.context_compression import ContextCompressionMiddleware
    from cr_agent.agent.middlewares.loop_detection import LoopDetectionMiddleware
    from cr_agent.agent.middlewares.tool_error_handling import ToolErrorHandlingMiddleware
    from cr_agent.agent.middlewares.output_budget import ToolOutputBudgetMiddleware
    from cr_agent.agent.middlewares.token_budget import TokenBudgetMiddleware

    chain = MiddlewareChain()
    chain.add(InputSanitizationMiddleware())
    chain.add(ContextCompressionMiddleware(max_messages=20, keep_recent=8))
    chain.add(LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, window_size=15))
    chain.add(ToolErrorHandlingMiddleware())
    chain.add(ToolOutputBudgetMiddleware(max_chars=20000))
    chain.add(TokenBudgetMiddleware(max_tokens=200000, warn_threshold=0.8))
    return chain
