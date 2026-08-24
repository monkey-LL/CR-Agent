"""ToolErrorHandlingMiddleware —— 工具执行失败时的优雅降级处理。

异常场景：
  LLM 说 "运行 ruff check" → ruff 未安装 → subprocess 抛出 FileNotFoundError
  无 middleware：异常向上传播 → Agent 崩溃 → 审查失败
  有 middleware：捕获错误 → 返回 ToolMessage(error) → Agent 用已有信息继续工作
                → 报告中写 "静态分析：已跳过（linter 不可用）"

生产环境原则：非关键工具采用失败放行策略，关键工具采用失败阻断策略。
  - lint 失败 → 失败放行（跳过，继续审查）
  - bash 执行 PR 代码 → 失败阻断（绝不允许发生）
  - generate_report 失败 → 失败阻断（无法生成输出）

错误信息中包含恢复提示，让 LLM 知道下一步该怎么做：
  "Lint failed: command not found. Continue with available context, or try a different tool."
"""

from __future__ import annotations

import logging
import traceback

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

_RECOVERY_HINT = "Continue with available context, or choose an alternative tool."  # 恢复提示：用现有上下文继续，或选择替代工具


class ToolErrorHandlingMiddleware(Middleware):
    """捕获工具执行错误并转换为 ToolMessage。

    此 middleware 包装工具执行过程。如果工具抛出异常，我们会：
    1. 记录完整 traceback 到日志
    2. 返回一个 status="error" 的 ToolMessage，包含截断后的错误信息
    3. 附带恢复提示，让 LLM 知道可以尝试其他方案

    Agent 会继续运行 —— 只是知道该工具不可用而已。
    """

    def __init__(self, max_error_length: int = 500):
        self.max_error_length = max_error_length

    def after_tool(self, state: dict, tool_result: str, ctx: MiddlewareContext) -> str | None:
        """检查工具结果是否包含错误，并确保错误信息可操作。"""
        # 包含错误的工具结果通常以 "Error:" 开头或包含 "Traceback"
        if "Traceback" in tool_result:
            # 截断过长的 traceback —— LLM 不需要完整的调用栈
            lines = tool_result.split("\n")
            if len(lines) > 10:
                tool_result = "\n".join(lines[:5]) + "\n...[truncated]...\n" + lines[-1]
            tool_result = f"{tool_result}\n\nRecovery hint: {_RECOVERY_HINT}"
            return tool_result[:self.max_error_length]
        return None


class ToolErrorHandler:
    """用于包装工具执行并处理错误的上下文管理器。

    在 graph.py 中的用法：
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
            return True  # 抑制异常，不再向上传播
        return False

    @property
    def error_message(self) -> str:
        if not self.error:
            return ""
        msg = f"Error executing {self.tool_name}: {type(self.error).__name__}: {self.error}"
        return (msg + f"\n\nRecovery hint: {_RECOVERY_HINT}")[:self.max_error_length]
