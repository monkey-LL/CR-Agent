"""TokenBudgetMiddleware —— 对每次审查强制执行总 token 预算控制。

异常场景：
  Agent 持续在工具和 LLM 之间循环调用。每次 LLM 调用消耗 5K token。
  40 次迭代后：消耗了 200K token。API 账单爆炸。
  没有上限 = 成本失控。

生产环境原则：
  设定预算。当消耗达到 80% 时 -> 警告 LLM 尽快收尾。
  当消耗达到 100% 时 -> 强制结束。停止 Agent，用已有的发现生成报告。

Token 计数：
  优先使用 tiktoken（cl100k_base 编码）进行精确计数。
  如果 tiktoken 未安装或加载失败，则回退到基于字符的估算（1 token ~ 4 字符）。

状态管理：
  每次调用的状态（_warned）在检测到新一轮审查开始时重置
  （通过 iteration 重置为 0 来检测）。如果不重置，上一轮审查的
  警告标志会抑制下一轮的警告。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4  # 英文回退估算


def _estimate_tokens(text: str) -> int:
    """混合中英文文本的 token 估算。

    英文约 4 字符/token，中文约 1.5 字符/token。
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u30ff')
    non_cjk = len(text) - cjk
    return int(cjk / 1.5 + non_cjk / 4)


class TokenBudgetMiddleware(Middleware):
    """强制执行总 token 预算，在阈值处发出警告或停止。

    Args:
        max_tokens: 一次审查的总 token 预算。
        warn_threshold: 触发 LLM 收尾警告的比例阈值（0-1）。
    """

    def __init__(self, max_tokens: int = 200000, warn_threshold: float = 0.8):
        self.max_tokens = max_tokens
        self.warn_threshold = warn_threshold
        self._warned = False
        self._encoder = None
        try:
            import tiktoken
            self._encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            logger.debug("tiktoken not available, using character-based estimation")

    def reset(self) -> None:
        """重置每次调用的状态。由 MiddlewareChain.reset() 调用。"""
        self._warned = False

    def _count_tokens(self, messages: list) -> int:
        """使用 tiktoken（如果可用）计算消息列表中的 token 数。"""
        if self._encoder is not None:
            total = 0
            for msg in messages:
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    total += len(self._encoder.encode(content))
                # 每条消息的额外开销（role token 等）
                total += 4
            return total
        else:
            total = 0
            for msg in messages:
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    total += _estimate_tokens(content)
                total += 4  # role 开销
            return total

    def after_model(self, state: dict, response, ctx: MiddlewareContext):
        # 精确计算 token 数
        messages = state.get("messages", [])
        estimated_tokens = self._count_tokens(messages)
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