"""LoopDetectionMiddleware — detect and stop infinite tool-calling loops.

Unhappy path:
  LLM calls read_file("src/app.py") → sees content → calls read_file("src/app.py") again
  → sees same content → calls again → ... forever.
  Or: LLM calls grep("password") → 0 results → calls grep("passwd") → 0 results →
  calls grep("secret") → ... trying variations endlessly.

Detection (two layers):
  Layer 1: Exact dedup. Hash tool name + args. If same hash appears N times
           in the sliding window, it's a loop.
  Layer 2: Frequency. If a single tool name is called more than M times total,
           the agent is over-using it.

Response:
  warn_threshold: inject a hint "You've called this tool N times. Consider
                  a different approach."
  hard_limit: strip tool_calls from the response, force the LLM to produce
              a final answer. Set forced_finalize=True.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, deque

from langchain_core.messages import AIMessage, ToolMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class LoopDetectionMiddleware(Middleware):
    """Detect infinite loops via tool call dedup and frequency analysis.

    Args:
        warn_threshold: Same exact call repeated this many times → inject warning.
        hard_limit: Same exact call repeated this many times → force finalize.
        window_size: Sliding window size for dedup detection.
    """

    def __init__(self, warn_threshold: int = 3, hard_limit: int = 5, window_size: int = 15):
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self._call_hashes: deque = deque(maxlen=window_size)
        self._tool_counts: Counter = Counter()

    def _hash_call(self, tool_call: dict) -> str:
        """Hash a tool call for dedup detection."""
        name = tool_call.get("name", "")
        args = str(sorted(tool_call.get("args", {}).items()))
        return hashlib.sha256(f"{name}:{args}".encode()).hexdigest()[:16]

    def after_model(self, state: dict, response, ctx: MiddlewareContext) -> any:
        if not isinstance(response, AIMessage) or not response.tool_calls:
            return None

        # Check each tool call
        for tc in response.tool_calls:
            call_hash = self._hash_call(tc)
            self._call_hashes.append(call_hash)
            self._tool_counts[tc.get("name", "")] += 1

            # Layer 1: exact dedup
            dup_count = sum(1 for h in self._call_hashes if h == call_hash)

            if dup_count >= self.hard_limit:
                logger.warning(
                    "Loop detected: tool %s called %d times (hard limit %d). Forcing finalize.",
                    tc.get("name"), dup_count, self.hard_limit,
                )
                # Strip tool calls, force finalization
                response.tool_calls = []
                response.content = (
                    "I've detected I'm repeating the same tool calls. "
                    "Let me generate the review report with what I have so far."
                )
                ctx.forced_finalize = True
                return response

            if dup_count >= self.warn_threshold:
                # Inject a hint without blocking
                hint = (
                    f"\n\n[Hint: You've called {tc.get('name')} with the same arguments "
                    f"{dup_count} times. Consider a different approach.]"
                )
                if isinstance(response.content, str):
                    response.content += hint
                logger.info("Loop warning: %s called %d times", tc.get("name"), dup_count)

        # Layer 2: frequency check
        for tool_name, count in self._tool_counts.items():
            if count > 30:  # Hard frequency limit
                logger.warning("Frequency limit: %s called %d times total", tool_name, count)
                if tool_name not in ctx.blocked_tools:
                    ctx.blocked_tools.add(tool_name)
                    response.content = (
                        f"\n\n[Tool {tool_name} has been used {count} times and is now blocked. "
                        f"Use a different approach or generate the report.]"
                    )

        return None

    def before_tool(self, state: dict, tool_call: dict, ctx: MiddlewareContext) -> dict | None:
        """Block tools that have been frequency-capped."""
        tool_name = tool_call.get("name", "")
        if tool_name in ctx.blocked_tools:
            return {
                "error": f"Tool '{tool_name}' is blocked due to overuse. Try a different approach.",
                "tool_call_id": tool_call.get("id", ""),
            }
        return None
