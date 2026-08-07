"""ContextCompressionMiddleware — compress conversation history to prevent token explosion.

Unhappy path:
  Agent reads 10 files, each 500 lines. Tool outputs accumulate in messages.
  After 5 iterations, context is 80K tokens. LLM can't fit the diff + context.
  Eventually: context window exceeded → API error → review fails.

How compression works:
  When message count exceeds max_messages, we:
  1. Keep the most recent `keep_recent` messages (current context)
  2. Replace older messages with a compact summary
  3. IMPORTANT: preserve system prompt and initial user message (review context)

What we DON'T compress (important info retention):
  - System prompt (agent identity and rules)
  - First user message (the diff + PR info)
  - Deterministic findings (already in state, not in messages)
  - Last N messages (active working context)

This is a simplified version. DeerFlow uses SummarizationMiddleware with
an independent summary model + DurableContextMiddleware that persists
delegations/skills to separate channels. For learning, this is enough.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class ContextCompressionMiddleware(Middleware):
    """Compress old messages when conversation grows too long.

    Args:
        max_messages: Trigger compression when message count exceeds this.
        keep_recent: Number of recent messages to keep verbatim.
    """

    def __init__(self, max_messages: int = 20, keep_recent: int = 8):
        self.max_messages = max_messages
        self.keep_recent = keep_recent

    def before_model(self, state: dict, ctx: MiddlewareContext) -> dict | None:
        messages = state.get("messages", [])
        if len(messages) <= self.max_messages:
            return None

        # Identify protected messages (system prompt + initial user message)
        protected: list = []
        compressible: list = []

        for msg in messages:
            if isinstance(msg, (SystemMessage,)):
                protected.append(msg)
            elif isinstance(msg, HumanMessage) and not compressible and not any(
                isinstance(m, HumanMessage) for m in protected
            ):
                # First human message = the review request with diff — protect it
                protected.append(msg)
            else:
                compressible.append(msg)

        # Keep the most recent messages, compress the rest
        recent = compressible[-self.keep_recent:] if len(compressible) > self.keep_recent else compressible
        old = compressible[:-self.keep_recent] if len(compressible) > self.keep_recent else []

        if not old:
            return None

        # Create a summary of old messages (simplified: just note what was compressed)
        # In production: use a separate LLM call to summarize. Here: extract tool names.
        tool_names = set()
        for msg in old:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_names.add(tc.get("name", "unknown"))
            if hasattr(msg, "name") and msg.name:
                tool_names.add(msg.name)

        summary_text = f"[Context compressed: {len(old)} earlier messages from tools: {', '.join(sorted(tool_names))}. Key findings preserved in state.]"
        summary_msg = SystemMessage(content=summary_text)

        new_messages = protected + [summary_msg] + recent
        logger.info(
            "Context compressed: %d → %d messages (kept %d recent, summarized %d old)",
            len(messages), len(new_messages), len(recent), len(old),
        )

        state["messages"] = new_messages
        return state
