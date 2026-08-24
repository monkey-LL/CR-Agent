"""ToolOutputBudgetMiddleware —— 截断工具输出以保护 token 预算。

异常场景：
  LLM 在一个混乱的项目上调用 run_lint("ruff check .") → 50,000 字符的 lint 输出
  这些内容作为 ToolMessage 进入对话 → 下一次 LLM 调用多出 50K token
  → token 预算耗尽 → 审查不完整

生产环境原则：
  工具输出有用，但并非越多越好。lint 输出的前 20K 字符已经包含了
  你需要的所有信息。剩下的 30K 只是重复内容。

  我们截断输出并添加提示："[output truncated, 50000 → 20000 chars]"
  LLM 知道自己看到的是部分输出，可以决定是否需要更多信息。
"""

from __future__ import annotations

import logging

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class ToolOutputBudgetMiddleware(Middleware):
    """截断超过字符限制的工具输出。

    Args:
        max_chars: 工具输出中保留的最大字符数。
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
