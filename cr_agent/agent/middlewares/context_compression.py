"""ContextCompressionMiddleware —— 压缩对话历史，防止 token 爆炸。

异常场景：
  Agent 读取了 10 个文件，每个 500 行。工具输出在 messages 中不断累积。
  5 次迭代后，上下文达到 80K token。LLM 无法容纳 diff + 上下文。
  最终：上下文窗口超出 -> API 报错 -> 审查失败。

压缩机制：
  当消息数量超过 max_messages 时，我们会：
  1. 保留最近的 `keep_recent` 条消息（当前上下文）
  2. 用摘要替换较早的消息
  3. 重要：保留 system prompt 和初始用户消息（审查上下文）

压缩级别：
  - 使用 LLM（生产环境）：用轻量级模型生成旧消息的真实摘要，
    保留关键发现和上下文。
  - 不使用 LLM（回退方案）：从旧消息中提取工具名和关键信息，
    生成结构化摘要。比简单截断效果更好。

不压缩的内容（重要信息保留）：
  - System prompt（Agent 身份和规则）
  - 第一条用户消息（diff + PR 信息）
  - 确定性发现（已在 state 中，不在 messages 中）
  - 最近 N 条消息（活跃工作上下文）
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class ContextCompressionMiddleware(Middleware):
    """当对话过长时压缩旧消息。

    Args:
        max_messages: 消息数量超过此值时触发压缩。
        keep_recent: 需要原样保留的最近消息条数。
        summary_llm: 可选的 LLM，用于生成旧消息的真实摘要。
                     如果为 None，则使用结构化提取（工具名 + 关键信息）。
    """

    def __init__(
        self,
        max_messages: int = 20,
        keep_recent: int = 8,
        summary_llm=None,
    ):
        self.max_messages = max_messages
        self.keep_recent = keep_recent
        self._summary_llm = summary_llm

    def _extract_tool_info(self, messages: list) -> str:
        """从旧消息中提取结构化信息（无 LLM 时的回退方案）。"""
        tool_names = set()
        tool_errors = []
        key_outputs = []

        for msg in messages:
            # 从 AIMessage 中提取工具调用名
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_names.add(tc.get("name", "unknown"))
                    # 提取关键参数（文件路径等）
                    args = tc.get("args", {})
                    if "path" in args:
                        key_outputs.append(f"read: {args['path']}")
                    elif "command" in args:
                        key_outputs.append(f"ran: {args['command'][:50]}")

            # 提取工具结果
            if isinstance(msg, ToolMessage):
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    # 捕获每个工具输出的第一行作为关键发现
                    first_line = content.split("\n")[0][:100]
                    if first_line and not first_line.startswith("Error"):
                        key_outputs.append(first_line)
                    # 记录错误
                    if "Error" in content or "Traceback" in content:
                        tool_errors.append(content[:80])

            # 提取 LLM 文本内容
            if isinstance(msg, AIMessage):
                content = getattr(msg, "content", "")
                if isinstance(content, str) and len(content) > 50:
                    # 捕获 LLM 的分析片段
                    key_outputs.append(content[:100])

        parts = []
        if tool_names:
            parts.append(f"Tools used: {', '.join(sorted(tool_names))}")
        if key_outputs:
            parts.append(f"Key info: {' | '.join(key_outputs[:5])}")
        if tool_errors:
            parts.append(f"Errors encountered: {len(tool_errors)}")

        return "; ".join(parts) if parts else "No extractable info"

    def _summarize_with_llm(self, messages: list) -> str:
        """使用 LLM 生成旧消息的正式摘要。"""
        try:
            # 构建旧消息的紧凑表示
            msg_text = []
            for msg in messages:
                role = "Unknown"
                if isinstance(msg, SystemMessage):
                    role = "System"
                elif isinstance(msg, HumanMessage):
                    role = "User"
                elif isinstance(msg, AIMessage):
                    role = "AI"
                elif isinstance(msg, ToolMessage):
                    role = "Tool"
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    msg_text.append(f"[{role}] {content[:300]}")

            summary_prompt = (
                "Summarize the following conversation messages from a code review agent. "
                "Preserve: files analyzed, key findings, tool results, and any issues found. "
                "Be concise (max 200 words).\n\n" + "\n".join(msg_text)
            )

            response = self._summary_llm.invoke([HumanMessage(content=summary_prompt)])
            content = getattr(response, "content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
        except Exception as e:
            logger.warning("LLM summarization failed, falling back to extraction: %s", e)

        # 回退到提取方式
        return self._extract_tool_info(messages)

    def before_model(self, state: dict, ctx: MiddlewareContext) -> dict | None:
        messages = state.get("messages", [])
        if len(messages) <= self.max_messages:
            return None

        # 识别受保护的消息（system prompt + 初始用户消息）
        protected: list = []
        compressible: list = []

        for msg in messages:
            if isinstance(msg, (SystemMessage,)):
                protected.append(msg)
            elif isinstance(msg, HumanMessage) and not compressible and not any(
                isinstance(m, HumanMessage) for m in protected
            ):
                # 第一条用户消息 = 包含 diff 的审查请求 —— 保护它
                protected.append(msg)
            else:
                compressible.append(msg)

        # 保留最近的消息，压缩其余消息
        recent = compressible[-self.keep_recent:] if len(compressible) > self.keep_recent else compressible
        old = compressible[:-self.keep_recent] if len(compressible) > self.keep_recent else []

        if not old:
            return None

        # 生成旧消息的摘要
        if self._summary_llm is not None:
            summary_text = self._summarize_with_llm(old)
            summary_prefix = "[LLM Summary"
        else:
            summary_text = self._extract_tool_info(old)
            summary_prefix = "[Context compressed"

        summary_msg = SystemMessage(
            content=(
                f"{summary_prefix}: {len(old)} earlier messages compressed. "
                f"{summary_text}. Key findings preserved in state.]"
            )
        )

        new_messages = protected + [summary_msg] + recent
        logger.info(
            "Context compressed: %d -> %d messages (kept %d recent, summarized %d old, llm=%s)",
            len(messages), len(new_messages), len(recent), len(old),
            self._summary_llm is not None,
        )

        state["messages"] = new_messages
        return state