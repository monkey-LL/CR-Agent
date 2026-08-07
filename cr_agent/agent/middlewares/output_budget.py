"""ToolOutputBudgetMiddleware — truncate tool output to protect token budget.

Unhappy path:
  LLM calls run_lint("ruff check .") on a messy project → 50,000 chars of lint output
  This goes into the conversation as a ToolMessage → next LLM call has 50K extra tokens
  → token budget exhausted → review incomplete

Production principle:
  Tool output is useful but not infinitely useful. The first 20K chars of lint output
  tell you everything you need. The remaining 30K is just more of the same.

  We truncate and add a note: "[output truncated, 50000 → 20000 chars]"
  The LLM knows it's seeing partial output and can decide if it needs more.
"""

from __future__ import annotations

import logging

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class ToolOutputBudgetMiddleware(Middleware):
    """Truncate tool outputs that exceed a character limit.

    Args:
        max_chars: Maximum characters to keep in tool output.
    """

    def __init__(self, max_chars: int = 20000):
        self.max_chars = max_chars

    def after_tool(self, state: dict, tool_result: str, ctx: MiddlewareContext) -> str | None:
        if len(tool_result) <= self.max_chars:
            return None

        original_len = len(tool_result)
        truncated = tool_result[:self.max_chars]
        truncated += f"\n\n[output truncated: {original_len} → {self.max_chars} chars. Use a more specific query if you need details from the omitted portion.]"

        logger.info(
            "Tool output truncated: %d → %d chars",
            original_len, self.max_chars,
        )
        return truncated
