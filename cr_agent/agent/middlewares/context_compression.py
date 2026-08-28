"""ContextCompressionMiddleware —— 压缩对话历史，防止 token 爆炸。

异常场景：
  Agent 读取了 10 个文件，每个 500 行。工具输出在 messages 中不断累积。
  5 次迭代后，上下文达到 80K token。LLM 无法容纳 diff + 上下文。
  最终：上下文窗口超出 -> API 报错 -> 审查失败。

优化（参考业界做法）：
  C1: 触发条件从消息条数改为 token 数（复用 tiktoken），避免"20 条短消息
      不该压却压了，20 条长消息早该压却没压"的问题。
  C2: 结构化摘要按工具类型分别提取关键信息——read_file 提取文件路径和
      行数，run_lint 提取错误数量和严重错误摘要，AIMessage 提取包含
      findings 的 JSON 片段。不再只取第一行前 100 字符。
  C3: 标记含 LLM findings 的 AIMessage 为不可压缩，避免审查结论被丢失。

压缩机制：
  当 token 数超过 max_tokens 时：
  1. 保留 system prompt + 首条用户消息（含 diff）
  2. 保留含 findings 的 AIMessage（不可压缩）
  3. 保留最近 keep_recent 条消息（当前工作上下文）
  4. 其余旧消息生成结构化摘要替换
"""

from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cr_agent.agent.middlewares.base import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4  # 英文回退估算


def _estimate_tokens(text: str) -> int:
    """混合中英文文本的 token 估算。

    英文约 4 字符/token，中文约 1.5 字符/token。
    按字符的 Unicode 范围区分，比纯除 4 更准确。
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u30ff')
    non_cjk = len(text) - cjk
    return int(cjk / 1.5 + non_cjk / 4)


class ContextCompressionMiddleware(Middleware):
    """当对话 token 数超过阈值时压缩旧消息。

    Args:
        max_tokens: token 数超过此值时触发压缩。
        keep_recent: 需要原样保留的最近消息条数。
        summary_llm: 可选的 LLM，用于生成旧消息的真实摘要。
                     如果为 None，则使用结构化提取。
    """

    def __init__(
        self,
        max_tokens: int = 50000,
        keep_recent: int = 8,
        summary_llm=None,
    ):
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent
        self._summary_llm = summary_llm
        self._encoder = None
        try:
            import tiktoken
            self._encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            logger.debug("tiktoken not available, using character-based estimation for compression")

    def _count_tokens(self, messages: list) -> int:
        """估算 messages 列表的 token 数。"""
        if self._encoder is not None:
            total = 0
            for msg in messages:
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    total += len(self._encoder.encode(content))
                total += 4  # role 开销
            return total
        else:
            total = 0
            for msg in messages:
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    total += _estimate_tokens(content)
                total += 4  # role 开销
            return total

    def _contains_findings(self, msg) -> bool:
        """检查 AIMessage 的 content 是否包含 LLM 审查结果 JSON。"""
        if not isinstance(msg, AIMessage):
            return False
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            return False
        # D1 后 LLM 在 content 里直接输出 JSON，检查是否含 findings 字段
        return '"findings"' in content and '"severity"' in content

    def _extract_tool_info(self, messages: list) -> str:
        """从旧消息中按工具类型分别提取结构化信息。"""
        files_read: list[str] = []
        lint_results: list[str] = []
        llm_analysis: list[str] = []
        errors: list[str] = []

        for msg in messages:
            # 从 AIMessage 中提取工具调用
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                    if name == "read_file":
                        path = args.get("path", "?")
                        files_read.append(path)
                    elif name == "run_lint":
                        cmd = args.get("command", "?")
                        lint_results.append(f"ran: {cmd[:80]}")

            # 从 ToolMessage 中按内容特征提取关键信息
            if isinstance(msg, ToolMessage):
                content = getattr(msg, "content", "")
                if not isinstance(content, str):
                    continue
                if "Error" in content or "Traceback" in content:
                    # 提取错误类型和位置
                    error_lines = [l for l in content.split("\n") if "Error" in l or "error" in l.lower()][:3]
                    errors.extend(error_lines)
                elif "Lint passed" in content:
                    lint_results.append("lint passed")
                elif "Lint failed" in content:
                    # 提取 lint 错误摘要（前 3 条错误）
                    error_lines = [l.strip() for l in content.split("\n")
                                   if l.strip() and not l.startswith("Lint failed")][:3]
                    lint_results.extend(error_lines)
                elif "File not found" in content:
                    pass  # 跳过无效读取
                else:
                    # read_file 的返回——提取文件名和行数
                    line_count = content.count("\n") + 1
                    first_line = content.split("\n")[0][:60] if content else ""
                    if first_line:
                        files_read.append(f"({line_count} lines)")

            # 提取 LLM 分析文本（非工具调用的 AIMessage）
            if isinstance(msg, AIMessage):
                content = getattr(msg, "content", "")
                if isinstance(content, str) and len(content) > 50:
                    # 如果包含 findings JSON，提取 findings 数量
                    if self._contains_findings(msg):
                        try:
                            # 尝试提取 JSON 并统计 findings
                            json_match = re.search(r'\{.*\}', content, re.DOTALL)
                            if json_match:
                                data = json.loads(json_match.group())
                                finding_count = len(data.get("findings", []))
                                llm_analysis.append(f"produced {finding_count} findings")
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    else:
                        # 普通分析/推理文本，取前 300 字符保留推理过程
                        # 300 比 150 更能保留完整推理链（如"这个函数可能有竞态因为..."）
                        llm_analysis.append(content[:300])

        parts = []
        if files_read:
            # 去重并保持顺序
            seen = set()
            unique_files = [f for f in files_read if not (f in seen or seen.add(f))]
            parts.append(f"Files read: {', '.join(unique_files[:10])}")
        if lint_results:
            parts.append(f"Lint results: {' | '.join(lint_results[:5])}")
        if llm_analysis:
            parts.append(f"LLM reasoning: {' | '.join(llm_analysis[:3])}")
        if errors:
            parts.append(f"Errors: {len(errors)} encountered")

        return "; ".join(parts) if parts else "No extractable info"

    def _summarize_with_llm(self, messages: list) -> str:
        """使用 LLM 生成旧消息的正式摘要。"""
        try:
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

        return self._extract_tool_info(messages)

    def before_model(self, state: dict, ctx: MiddlewareContext) -> dict | None:
        messages = state.get("messages", [])

        # C1: 按 token 数触发，而非消息条数
        current_tokens = self._count_tokens(messages)
        if current_tokens <= self.max_tokens:
            return None

        # 分类消息：受保护的 / 含 findings 的 / 可压缩的
        # 以"带 tool_calls 的 AIMessage + 其后续 ToolMessage"为最小不可分单元，
        # 防止压缩后出现孤立的 tool_call 或孤立的 ToolMessage（会导致 API 400）。
        protected: list = []
        compressible: list = []
        first_user_protected = False

        i = 0
        while i < len(messages):
            msg = messages[i]

            if isinstance(msg, SystemMessage):
                protected.append(msg)
                i += 1
            elif isinstance(msg, HumanMessage) and not first_user_protected:
                protected.append(msg)
                first_user_protected = True
                i += 1
            elif isinstance(msg, AIMessage) and self._contains_findings(msg):
                protected.append(msg)
                i += 1
            elif isinstance(msg, AIMessage) and msg.tool_calls:
                # 带工具调用的 AIMessage：收集它和后续对应的 ToolMessage 作为一个单元
                unit = [msg]
                j = i + 1
                while j < len(messages) and isinstance(messages[j], ToolMessage):
                    unit.append(messages[j])
                    j += 1
                compressible.extend(unit)
                i = j
            elif isinstance(msg, ToolMessage):
                # 孤立的 ToolMessage（不应该出现，但防御性处理）—— 与前一消息同区
                compressible.append(msg)
                i += 1
            else:
                compressible.append(msg)
                i += 1

        # 保留最近的消息，压缩其余消息
        # 以"AIMessage(tool_calls) + 其 ToolMessage"为单元切分，避免截断配对
        recent: list = []
        old: list = []
        if len(compressible) > self.keep_recent:
            # 从末尾向前收集 keep_recent 条，但不截断在 tool_call/ToolMessage 中间
            cut_point = len(compressible) - self.keep_recent
            # 如果 cut_point 落在 ToolMessage 上，向后推到包含该配对的 AIMessage 之前
            while cut_point < len(compressible) and isinstance(compressible[cut_point], ToolMessage):
                cut_point += 1
            old = compressible[:cut_point]
            recent = compressible[cut_point:]
        else:
            recent = compressible

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
        new_tokens = self._count_tokens(new_messages)
        logger.info(
            "Context compressed: %d tokens -> %d tokens, %d messages -> %d messages "
            "(protected=%d, kept_recent=%d, summarized=%d, llm=%s)",
            current_tokens, new_tokens, len(messages), len(new_messages),
            len(protected), len(recent), len(old),
            self._summary_llm is not None,
        )

        state["messages"] = new_messages
        return state