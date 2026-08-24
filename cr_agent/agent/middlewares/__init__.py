"""Middleware 链 —— 生产环境 Agent 的核心骨架。

学习重点：
  - 为什么需要 middleware？和 Express/Koa 使用中间件的理由相同：横切关注点
    （日志、鉴权、压缩）不应该硬编码在业务逻辑中。
  - 四个钩子：before_model / after_model / before_tool / after_tool
  - 顺序很重要：InputSanitization 必须在 LLM 看到输入之前运行；
    TokenBudget 必须在 LLM 响应之后运行；LoopDetection 必须在
    工具执行之前运行。

  链式管道。每个 middleware 可以：
  - 修改 state（添加上下文、脱敏、截断输出）
  - 短路（阻止工具调用、强制结束）
  - 透传（仅观察，例如日志记录）

演示环境 vs 生产环境：
  演示：LLM 说 "调用 get_weather" → 你调用它 → 返回结果 → 完成。
  生产：如果天气 API 挂了怎么办？超时？限流？API key 泄露？
        工具名错误？无限循环？Token 爆炸？这些都由 middleware 处理，
        而不是由你的业务逻辑处理。
"""

from cr_agent.agent.middlewares.base import Middleware, MiddlewareChain, build_default_chain
from cr_agent.agent.middlewares.context_compression import ContextCompressionMiddleware
from cr_agent.agent.middlewares.input_sanitization import InputSanitizationMiddleware
from cr_agent.agent.middlewares.loop_detection import LoopDetectionMiddleware
from cr_agent.agent.middlewares.output_budget import ToolOutputBudgetMiddleware
from cr_agent.agent.middlewares.token_budget import TokenBudgetMiddleware
from cr_agent.agent.middlewares.tool_error_handling import ToolErrorHandlingMiddleware

__all__ = [
    "ContextCompressionMiddleware",
    "InputSanitizationMiddleware",
    "LoopDetectionMiddleware",
    "Middleware",
    "MiddlewareChain",
    "TokenBudgetMiddleware",
    "ToolErrorHandlingMiddleware",
    "ToolOutputBudgetMiddleware",
    "build_default_chain",
]
