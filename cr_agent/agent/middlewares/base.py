"""Middleware 基类与链式执行器。

架构设计：
  每个 middleware 实现以下 4 个可选钩子：
    - before_model(state): 在 LLM 调用前执行。可以修改 messages、
      注入上下文，或直接阻止调用。
    - after_model(state, response): 在 LLM 响应后执行。可以检查
      tool 调用、修改响应，或强制结束。
    - before_tool(state, tool_call): 在执行工具前调用。可以
      阻止工具调用（例如循环检测、权限校验）。
    - after_tool(state, tool_result): 在工具返回后调用。可以
      截断输出、脱敏处理、记录日志。

  链式执行按注册顺序依次调用钩子。如果某个 middleware 在
  before_model/before_tool 中返回非 None 结果，则触发短路
  （跳过后续 middleware 和实际调用）。

生产环境 vs 演示：
  演示环境不需要 middleware —— 正常流程下一切都能工作。
  生产环境需要 middleware，因为：
  - LLM 输入可能包含注入攻击（InputSanitization）
  - LLM 可能无限循环（LoopDetection）
  - 工具输出可能导致上下文爆炸（ToolOutputBudget）
  - Token 消耗可能超出预算（TokenBudget）
  - 工具可能执行失败（ToolErrorHandling → 优雅降级）
  - 上下文可能无限增长（ContextCompression）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MiddlewareContext:
    """在 middleware 链中传递的共享上下文。

    这与 AgentState 不同。这是每次调用的元数据，
    middleware 用它在不同钩子之间跟踪信息：
    - 迭代计数（用于循环检测）
    - 工具调用历史（用于去重检测）
    - token 使用量（用于预算控制）
    - trace_id（用于可观测性）
    """
    iteration: int = 0
    tool_call_history: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    trace_id: str = ""
    forced_finalize: bool = False
    blocked_tools: set[str] = field(default_factory=set)


class Middleware:
    """所有 middleware 的基类。按需覆写钩子方法。"""

    def before_model(self, state: dict, ctx: MiddlewareContext) -> dict | None:
        """在 LLM 调用前执行。返回修改后的 state，或 None 表示透传。"""
        return None

    def after_model(self, state: dict, response: Any, ctx: MiddlewareContext) -> Any | None:
        """在 LLM 响应后执行。返回修改后的 response，或 None 表示透传。"""
        return None

    def before_tool(self, state: dict, tool_call: dict, ctx: MiddlewareContext) -> dict | None:
        """在工具执行前调用。返回错误字典以阻止执行，返回 None 表示允许。"""
        return None

    def after_tool(self, state: dict, tool_result: str, ctx: MiddlewareContext) -> str | None:
        """在工具返回后调用。返回修改后的结果，或 None 表示透传。"""
        return None

    @property
    def name(self) -> str:
        return self.__class__.__name__


class MiddlewareChain:
    """按注册顺序执行 middleware 钩子。

    顺序至关重要：
      1. InputSanitization（最先执行 —— 在 LLM 看到输入前进行中和处理）
      2. ContextCompression（在发送给 LLM 前压缩上下文）
      3. LoopDetection（在工具执行前检查循环）
      4. ToolErrorHandling（捕获工具执行失败）
      5. ToolOutputBudget（在工具返回后截断输出）
      6. TokenBudget（最后执行 —— 检查总消耗量）
    """

    def __init__(self, middlewares: list[Middleware] | None = None):
        self.middlewares = middlewares or []
        self.ctx = MiddlewareContext()

    def add(self, middleware: Middleware) -> MiddlewareChain:
        self.middlewares.append(middleware)
        return self

    def run_before_model(self, state: dict) -> dict:
        """运行所有 before_model 钩子。返回可能被修改的 state。"""
        for mw in self.middlewares:
            try:
                result = mw.before_model(state, self.ctx)
                if result is not None:
                    logger.debug(f"Middleware {mw.name} modified state in before_model")
                    state = result
            except Exception as e:
                logger.error(f"Middleware {mw.name} before_model error: {e}", exc_info=True)
        return state

    def run_after_model(self, state: dict, response: Any) -> Any:
        """运行所有 after_model 钩子。返回可能被修改的 response。"""
        for mw in self.middlewares:
            try:
                result = mw.after_model(state, response, self.ctx)
                if result is not None:
                    response = result
            except Exception as e:
                logger.error(f"Middleware {mw.name} after_model error: {e}", exc_info=True)
        return response

    def run_before_tool(self, state: dict, tool_call: dict) -> dict | None:
        """运行所有 before_tool 钩子。返回错误字典以阻止执行，返回 None 表示允许。"""
        for mw in self.middlewares:
            try:
                result = mw.before_tool(state, tool_call, self.ctx)
                if result is not None:
                    logger.info(f"Middleware {mw.name} blocked tool {tool_call.get('name')}")
                    return result
            except Exception as e:
                logger.error(f"Middleware {mw.name} before_tool error: {e}", exc_info=True)
        return None

    def run_after_tool(self, state: dict, tool_result: str) -> str:
        """运行所有 after_tool 钩子。返回可能被修改的结果。"""
        for mw in self.middlewares:
            try:
                result = mw.after_tool(state, tool_result, self.ctx)
                if result is not None:
                    tool_result = result
            except Exception as e:
                logger.error(f"Middleware {mw.name} after_tool error: {e}", exc_info=True)
        return tool_result

    def should_finalize(self) -> bool:
        """检查是否有 middleware 触发了强制结束。"""
        return self.ctx.forced_finalize

    def reset(self) -> None:
        """为新的一次审查调用重置链上下文。

        当 middleware 链被复用时（例如生产环境中的单例模式），
        此方法可防止不同审查之间的状态泄漏。
        同时重置共享上下文和每个 middleware 的内部状态。
        """
        self.ctx = MiddlewareContext()
        for mw in self.middlewares:
            if hasattr(mw, "reset"):
                mw.reset()
        logger.debug("Middleware chain context reset for new review")


def build_default_chain() -> MiddlewareChain:
    """构建 CR Agent 的默认生产环境 middleware 链。

    顺序很重要！详见 MiddlewareChain 的 docstring。
    """
    from cr_agent.agent.middlewares.context_compression import ContextCompressionMiddleware
    from cr_agent.agent.middlewares.input_sanitization import InputSanitizationMiddleware
    from cr_agent.agent.middlewares.loop_detection import LoopDetectionMiddleware
    from cr_agent.agent.middlewares.output_budget import ToolOutputBudgetMiddleware
    from cr_agent.agent.middlewares.token_budget import TokenBudgetMiddleware
    from cr_agent.agent.middlewares.tool_error_handling import ToolErrorHandlingMiddleware

    chain = MiddlewareChain()
    chain.add(InputSanitizationMiddleware())
    chain.add(ContextCompressionMiddleware(max_tokens=50000, keep_recent=8))
    chain.add(LoopDetectionMiddleware(warn_threshold=3, hard_limit=5, window_size=15))
    chain.add(ToolErrorHandlingMiddleware())
    chain.add(ToolOutputBudgetMiddleware(max_chars=20000))
    chain.add(TokenBudgetMiddleware(max_tokens=200000, warn_threshold=0.8))
    return chain
