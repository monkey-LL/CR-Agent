"""LoopDetectionMiddleware —— 检测并阻止无限工具调用循环。

异常场景：
  LLM 调用 read_file("src/app.py") → 看到内容 → 再次调用 read_file("src/app.py")
  → 看到相同内容 → 再次调用 → ... 无限循环。
  或者：LLM 调用 grep("password") → 0 个结果 → 调用 grep("passwd") → 0 个结果 →
  调用 grep("secret") → ... 不断尝试变体。

检测机制（两层）：
  第一层：精确去重。对工具名 + 参数进行哈希。如果相同哈希在滑动窗口
          中出现 N 次，则判定为循环。
  第二层：频率检测。如果单个工具名被调用的总次数超过 M 次，
          则判定为过度使用。

响应策略：
  warn_threshold：注入提示 "你已经调用该工具 N 次了，考虑换一种方法。"
  hard_limit：从响应中移除 tool_calls，强制 LLM 生成最终答案。
              设置 forced_finalize=True。
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, deque

from langchain_core.messages import AIMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class LoopDetectionMiddleware(Middleware):
    """通过工具调用去重和频率分析检测无限循环。

    Args:
        warn_threshold: 相同调用重复达到此次数 -> 注入警告。
        hard_limit: 相同调用重复达到此次数 -> 强制结束。
        window_size: 用于去重检测的滑动窗口大小。
    """

    def __init__(self, warn_threshold: int = 3, hard_limit: int = 5, window_size: int = 15):
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self._call_hashes: deque = deque(maxlen=window_size)
        self._tool_counts: Counter = Counter()

    def reset(self) -> None:
        """重置每次调用的状态。由 MiddlewareChain.reset() 调用。"""
        self._call_hashes.clear()
        self._tool_counts.clear()

    def _hash_call(self, tool_call: dict) -> str:
        """对工具调用进行哈希，用于去重检测。"""
        name = tool_call.get("name", "")
        args = str(sorted(tool_call.get("args", {}).items()))
        return hashlib.sha256(f"{name}:{args}".encode()).hexdigest()[:16]

    def after_model(self, state: dict, response, ctx: MiddlewareContext):
        if not isinstance(response, AIMessage) or not response.tool_calls:
            return None

        # 检查每个工具调用
        for tc in response.tool_calls:
            call_hash = self._hash_call(tc)
            self._call_hashes.append(call_hash)
            self._tool_counts[tc.get("name", "")] += 1

            # 第一层：精确去重
            dup_count = sum(1 for h in self._call_hashes if h == call_hash)

            if dup_count >= self.hard_limit:
                logger.warning(
                    "Loop detected: tool %s called %d times (hard limit %d). Forcing finalize.",
                    tc.get("name"), dup_count, self.hard_limit,
                )
                # 移除工具调用，强制结束
                response.tool_calls = []
                response.content = (
                    "I've detected I'm repeating the same tool calls. "
                    "Let me generate the review report with what I have so far."
                )
                ctx.forced_finalize = True
                return response

            if dup_count >= self.warn_threshold:
                # 注入提示但不阻止调用
                hint = (
                    f"\n\n[Hint: You've called {tc.get('name')} with the same arguments "
                    f"{dup_count} times. Consider a different approach.]"
                )
                if isinstance(response.content, str):
                    response.content += hint
                logger.info("Loop warning: %s called %d times", tc.get("name"), dup_count)

        # 第二层：频率检查
        for tool_name, count in self._tool_counts.items():
            if count > 30:  # 硬频率上限
                logger.warning("Frequency limit: %s called %d times total", tool_name, count)
                if tool_name not in ctx.blocked_tools:
                    ctx.blocked_tools.add(tool_name)
                    response.content = (
                        f"\n\n[Tool {tool_name} has been used {count} times and is now blocked. "
                        f"Use a different approach or generate the report.]"
                    )

        return None

    def before_tool(self, state: dict, tool_call: dict, ctx: MiddlewareContext) -> dict | None:
        """阻止因频率超限而被封禁的工具。"""
        tool_name = tool_call.get("name", "")
        if tool_name in ctx.blocked_tools:
            return {
                "error": f"Tool '{tool_name}' is blocked due to overuse. Try a different approach.",
                "tool_call_id": tool_call.get("id", ""),
            }
        return None
