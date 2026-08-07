"""ToolErrorHandlingMiddleware — graceful degradation when tools fail.

Unhappy path:
  LLM says "run ruff check" → ruff not installed → subprocess raises FileNotFoundError
  Without middleware: exception propagates → Agent crashes → review fails
  With middleware: error caught → return ToolMessage(error) → Agent continues with
                   what it has → report says "Static Analysis: skipped (linter unavailable)"

Production principle: fail-open for non-critical tools, fail-closed for critical.
  - lint failing → fail-open (skip, continue review)
  - bash executing PR code → fail-closed (must never happen)
  - generate_report failing → fail-closed (can't produce output)

The error message includes a recovery hint so the LLM knows what to do next:
  "Lint failed: command not found. Continue with available context, or try a different tool."
"""

from __future__ import annotations

import logging
import traceback

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

_RECOVERY_HINT = "Continue with available context, or choose an alternative tool."


class ToolErrorHandlingMiddleware(Middleware):
    """Catch tool execution errors and convert them to ToolMessages.

    This middleware wraps tool execution. If the tool raises, we:
    1. Log the error with full traceback
    2. Return a ToolMessage with status="error" and a truncated error message
    3. Include a recovery hint so the LLM knows to try something else

    The Agent continues running — it just knows that tool didn't work.
    """

    def __init__(self, max_error_length: int = 500):
        self.max_error_length = max_error_length

    def after_tool(self, state: dict, tool_result: str, ctx: MiddlewareContext) -> str | None:
        """Check if tool result contains an error and ensure it's actionable."""
        # Tool results that indicate errors often start with "Error:" or contain "Traceback"
        if "Traceback" in tool_result:
            # Truncate long tracebacks — LLM doesn't need the full stack
            lines = tool_result.split("\n")
            if len(lines) > 10:
                tool_result = "\n".join(lines[:5]) + "\n...[truncated]...\n" + lines[-1]
            tool_result = f"{tool_result}\n\nRecovery hint: {_RECOVERY_HINT}"
            return tool_result[:self.max_error_length]
        return None


class ToolErrorHandler:
    """Context manager for wrapping tool execution with error handling.

    Usage in graph.py:
        with ToolErrorHandler() as handler:
            result = tool.invoke(args)
        if handler.error:
            return ToolMessage(content=handler.error_message, ...)
    """

    def __init__(self, tool_name: str, max_error_length: int = 500):
        self.tool_name = tool_name
        self.max_error_length = max_error_length
        self.error: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            self.error = exc_val
            logger.error(
                "Tool %s failed: %s: %s",
                self.tool_name,
                type(exc_val).__name__,
                str(exc_val)[:200],
            )
            logger.debug("Traceback: %s", "".join(traceback.format_tb(exc_tb)))
            return True  # Suppress the exception
        return False

    @property
    def error_message(self) -> str:
        if not self.error:
            return ""
        msg = f"Error executing {self.tool_name}: {type(self.error).__name__}: {self.error}"
        return (msg + f"\n\nRecovery hint: {_RECOVERY_HINT}")[:self.max_error_length]
