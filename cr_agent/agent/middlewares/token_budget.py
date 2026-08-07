"""TokenBudgetMiddleware — enforce total token budget per review.

Unhappy path:
  Agent keeps calling tools and LLM in a loop. Each LLM call costs 5K tokens.
  After 40 iterations: 200K tokens consumed. API bill explodes.
  No upper bound = uncontrolled cost.

Production principle:
  Set a budget. When 80% consumed → warn the LLM to start wrapping up.
  When 100% consumed → force finalize. Stop the Agent, produce report with
  whatever findings exist.

  This is the "runaway cost prevention" middleware.
  Without it, a single buggy review could cost $10 in API calls.
  With it, max cost per review is bounded.

Estimating tokens (simplified):
  We don't have a real tokenizer here. We approximate: 1 token ≈ 4 chars.
  This is rough but good enough for budget enforcement.
  Production: use tiktoken for exact counts.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4  # Approximate


class TokenBudgetMiddleware(Middleware):
    """Enforce total token budget and warn/stop at thresholds.

    Args:
        max_tokens: Total token budget for one review.
        warn_threshold: Fraction (0-1) at which to warn the LLM to wrap up.
    """

    def __init__(self, max_tokens: int = 200000, warn_threshold: float = 0.8):
        self.max_tokens = max_tokens
        self.warn_threshold = warn_threshold
        self._warned = False

    def after_model(self, state: dict, response, ctx: MiddlewareContext) -> any:
        # Estimate tokens from message content
        messages = state.get("messages", [])
        total_chars = sum(
            len(getattr(msg, "content", "")) for msg in messages
            if isinstance(getattr(msg, "content", ""), str)
        )
        estimated_tokens = total_chars // _CHARS_PER_TOKEN
        ctx.total_tokens = estimated_tokens

        if estimated_tokens >= self.max_tokens:
            logger.warning(
                "Token budget exhausted: %d tokens (max %d). Forcing finalize.",
                estimated_tokens, self.max_tokens,
            )
            if isinstance(response, AIMessage) and response.tool_calls:
                response.tool_calls = []
                response.content = (
                    "Token budget exhausted. Generating review report with "
                    "findings collected so far."
                )
            ctx.forced_finalize = True
            return response

        if estimated_tokens >= self.max_tokens * self.warn_threshold and not self._warned:
            self._warned = True
            budget_pct = int(estimated_tokens / self.max_tokens * 100)
            logger.info("Token budget warning: %d%% used (%d / %d)", budget_pct, estimated_tokens, self.max_tokens)
            if isinstance(response, AIMessage) and isinstance(response.content, str) and response.tool_calls:
                response.content += (
                    f"\n\n[Budget alert: {budget_pct}% of token budget used. "
                    f"Start synthesizing your review report soon.]"
                )
                return response

        return None
