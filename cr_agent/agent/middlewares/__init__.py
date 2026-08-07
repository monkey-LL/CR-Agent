"""Middleware chain — the backbone of a production Agent.

Learning focus:
  - Why middleware? Same reason Express/Koa use it: cross-cutting concerns
    (logging, auth, compression) should NOT be hardcoded in business logic.
  - Four hooks: before_model / after_model / before_tool / after_tool
  - Order matters: InputSanitization must run BEFORE the LLM sees input;
    TokenBudget must run AFTER the LLM responds; LoopDetection must run
    BEFORE tool execution.

  The chain is a pipeline. Each middleware can:
  - Modify state (add context, mask secrets, truncate output)
  - Short-circuit (block a tool call, force finalization)
  - Pass through (just observe, like logging)

Demo vs Production:
  Demo: LLM says "call get_weather" → you call it → return result → done.
  Prod:  What if weather API is down? Timeout? Rate limit? API key leaked?
         Wrong tool name? Infinite loop? Token explosion? All these are
         handled by middleware, NOT by your business logic.
"""

from cr_agent.agent.middlewares.base import Middleware, MiddlewareChain, build_default_chain
from cr_agent.agent.middlewares.input_sanitization import InputSanitizationMiddleware
from cr_agent.agent.middlewares.token_budget import TokenBudgetMiddleware
from cr_agent.agent.middlewares.loop_detection import LoopDetectionMiddleware
from cr_agent.agent.middlewares.tool_error_handling import ToolErrorHandlingMiddleware
from cr_agent.agent.middlewares.context_compression import ContextCompressionMiddleware
from cr_agent.agent.middlewares.output_budget import ToolOutputBudgetMiddleware

__all__ = [
    "Middleware",
    "MiddlewareChain",
    "build_default_chain",
    "InputSanitizationMiddleware",
    "TokenBudgetMiddleware",
    "LoopDetectionMiddleware",
    "ToolErrorHandlingMiddleware",
    "ContextCompressionMiddleware",
    "ToolOutputBudgetMiddleware",
]
