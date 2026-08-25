"""LangGraph 状态机，集成中间件链。

架构：
  START → prepare → llm ↔ tools → finalize → END

  中间件链包裹每个节点：
  - before_model: InputSanitization, ContextCompression
  - after_model: LoopDetection, TokenBudget
  - before_tool: LoopDetection（封禁的工具）
  - after_tool: ToolErrorHandling, ToolOutputBudget, InputSanitization（脱敏）

  LLM 不掌控全局——我们的 graph + 中间件才掌控。LLM 只是"建议"
  调用哪个工具；中间件决定是否允许，graph 决定是循环还是终止。
"""

from __future__ import annotations

import json
import logging
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from cr_agent.agent.middlewares import MiddlewareChain, build_default_chain
from cr_agent.agent.middlewares.tool_error_handling import ToolErrorHandler
from cr_agent.agent.prompts import SYSTEM_PROMPT, build_review_prompt
from cr_agent.agent.state import AgentState
from cr_agent.agent.tools import ALL_TOOLS
from cr_agent.core.diff_parser import DiffHunk, compute_metrics, parse_diff
from cr_agent.core.models import (
    Confidence,
    DiffMetrics,
    Finding,
    ReviewReport,
    Severity,
    determine_verdict,
)
from cr_agent.core.rules_engine import run_deterministic_checks
from cr_agent.security.sanitizer import sanitize_input

logger = logging.getLogger(__name__)
MAX_ITERATIONS = 15

# 模块级熔断器单例：跨多次审查共享状态，避免每次 build_graph 重置
from cr_agent.observability.logger import CircuitBreaker as _CircuitBreaker
_llm_circuit_breaker = _CircuitBreaker(threshold=5, recovery_timeout=60.0)

# 增量审查阈值：diff 超过此字符数时启用 hunk 压缩
INCREMENTAL_REVIEW_THRESHOLD = 10_000
# 每个 hunk 保留的上下文行数（变更行前后各保留多少行）
HUNK_CONTEXT_LINES = 2


def _compress_diff_for_llm(hunks: list[DiffHunk]) -> str:
    """将大 diff 按 hunk 压缩，只保留变更行 + 少量上下文行。

    确定性规则引擎在完整 diff 上运行（不受影响）。
    LLM 收到的是压缩后的 diff，大幅减少 token 消耗。

    压缩策略：
    - 每个 hunk 保留变更行（+/- 行）和前后各 HUNK_CONTEXT_LINES 行上下文
    - 跳过纯上下文的大段未变更代码
    - 用分隔符标注 hunk 边界，保留 file 和行号信息
    """
    compressed_parts: list[str] = []
    for hunk in hunks:
        if not hunk.file or not hunk.lines:
            continue

        changed_indices = [
            i for i, line in enumerate(hunk.lines)
            if line.startswith("+") or line.startswith("-")
        ]
        if not changed_indices:
            continue

        first = max(0, changed_indices[0] - HUNK_CONTEXT_LINES)
        last = min(len(hunk.lines) - 1, changed_indices[-1] + HUNK_CONTEXT_LINES)

        header = f"--- {hunk.file} (lines around {hunk.new_start or '?'}) ---"
        selected = hunk.lines[first:last + 1]
        compressed_parts.append(header + "\n" + "\n".join(selected))

    return "\n\n".join(compressed_parts)


def _prepare_review(state: AgentState) -> dict:
    """入口节点：确定性检查 + 构建 prompt。"""
    diff = state["diff"]
    pr_info = state["pr_info"]
    memory_context = state.get("memory_context", "")

    # 在入口处截断 diff，确保后续所有环节使用同一份数据
    max_diff_chars = 50_000
    diff_truncated = False
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars]
        diff_truncated = True

    hunks = parse_diff(diff)
    det_findings = run_deterministic_checks(hunks)
    files_changed, lines_added, lines_removed = compute_metrics(hunks)

    # 增量审查：diff 较大时压缩传给 LLM 的内容，确定性规则不受影响
    diff_for_llm = diff
    incremental_used = False
    if len(diff) > INCREMENTAL_REVIEW_THRESHOLD:
        compressed = _compress_diff_for_llm(hunks)
        if len(compressed) < len(diff):
            diff_for_llm = compressed
            incremental_used = True
            logger.info(
                "增量审查: diff %d → %d 字符 (压缩 %.0f%%)",
                len(diff), len(compressed), (1 - len(compressed) / len(diff)) * 100,
            )

    safe_diff = sanitize_input(diff_for_llm)
    safe_pr_info = {
        k: sanitize_input(str(v)) if isinstance(v, str) else v
        for k, v in pr_info.items()
    }
    safe_memory = sanitize_input(memory_context) if memory_context else ""

    user_msg = HumanMessage(
        content=build_review_prompt(safe_pr_info, safe_diff, det_findings, safe_memory)
    )
    system_msg = SystemMessage(content=SYSTEM_PROMPT)

    logger.info(
        "审查准备完成: %d 个文件, +%d/-%d 行, %d 条确定性发现%s%s",
        files_changed, lines_added, lines_removed, len(det_findings),
        " (diff 已截断)" if diff_truncated else "",
        " (增量审查)" if incremental_used else "",
    )

    return {
        "messages": [system_msg, user_msg],
        "deterministic_findings": det_findings,
        "diff": diff,
        "report": None,
        "iteration": 0,
    }


def _make_llm_node(llm: ChatOpenAI, chain: MiddlewareChain):
    """创建 LLM 决策节点，集成中间件。"""
    from cr_agent.observability.logger import CircuitBreakerOpenError, retry_with_backoff

    def _llm_decide(state: AgentState) -> dict:
        iteration = state.get("iteration", 0)

        if iteration >= MAX_ITERATIONS:
            logger.warning("达到最大迭代次数 (%d)，强制终止", iteration)
            return {
                "messages": [AIMessage(content="已达到最大迭代次数，使用已有发现生成报告。")],
                "iteration": iteration + 1,
                "forced_finalize": True,
            }

        # 运行 before_model 中间件（输入清洗、上下文压缩）
        state_dict = dict(state)
        state_dict["messages"] = list(state.get("messages", []))
        state_dict = chain.run_before_model(state_dict)

        # 调用 LLM（带熔断器 + 重试）
        llm_with_tools = llm.bind_tools(ALL_TOOLS)

        @retry_with_backoff(max_retries=3, base_delay=1.0)
        def _call_llm():
            return _llm_circuit_breaker.call(llm_with_tools.invoke, state_dict["messages"])

        try:
            response = _call_llm()
        except CircuitBreakerOpenError:
            logger.warning("Circuit breaker open, forcing finalize")
            response = AIMessage(content="LLM service unavailable (circuit breaker open). Generating report with available findings.")
            return {
                "messages": [response],
                "iteration": iteration + 1,
                "forced_finalize": True,
            }
        except Exception as e:
            logger.error("LLM call failed after retries: %s", e)
            response = AIMessage(content=f"LLM call failed: {e}. Generating report with available findings.")
            return {
                "messages": [response],
                "iteration": iteration + 1,
                "forced_finalize": True,
            }

        # 运行 after_model 中间件（循环检测、token 预算、密钥脱敏）
        response = chain.run_after_model(state_dict, response)

        # 检查中间件是否强制终止
        forced = chain.should_finalize()
        if forced:
            logger.info("中间件强制终止")

        chain.ctx.iteration = iteration + 1

        return {
            "messages": [response],
            "iteration": iteration + 1,
            "forced_finalize": forced,
        }

    return _llm_decide


def _execute_tools(state: AgentState, chain: MiddlewareChain) -> dict:
    """执行工具调用，带中间件包裹。"""
    last_msg = state["messages"][-1]
    tool_map = {t.name: t for t in ALL_TOOLS}
    tool_results = []

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        tool_call_id = tc["id"]

        # 运行 before_tool 中间件（循环检测、授权）
        blocked = chain.run_before_tool(state, tc)
        if blocked:
            tool_results.append(ToolMessage(
                content=blocked.get("error", "Tool blocked by middleware"),
                tool_call_id=tool_call_id,
            ))
            continue

        logger.info("执行工具: %s", tool_name)

        # 带错误处理地执行
        handler = ToolErrorHandler(tool_name)
        with handler:
            result = tool_map[tool_name].invoke(tool_args)

        if handler.error:
            result_str = handler.error_message
        else:
            result_str = str(result)

        # 运行 after_tool 中间件（输出截断、错误格式化、密钥脱敏）
        result_str = chain.run_after_tool(state, result_str)

        tool_results.append(ToolMessage(
            content=result_str,
            tool_call_id=tool_call_id,
        ))

    return {"messages": tool_results}


def _should_continue(state: AgentState) -> str:
    """条件边：走向 tools 还是 finalize。"""
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "finalize"


def _category_from_rule_id(rule_id: str) -> str:
    """从 rule_id 提取问题类别，用于跨来源去重。

    确定性规则: "security.sql-injection" → "security.sql-injection"
    LLM 规则:   "llm.unknown" → 无法匹配，返回 "llm.unknown"
    去重时尝试用 (file, line, category) 匹配，category 不一致则不去重。
    """
    return rule_id


def _findings_match(a: Finding, b: Finding) -> bool:
    """判断两条 Finding 是否指向同一个问题（用于去重）。

    匹配策略（从严到松）：
    1. 同 file + 同 line + 同 rule_id → 精确匹配
    2. 同 file + 同 line + rule_id 类别前缀相同 → 跨来源匹配
       例如 "security.sql-injection" 和 "llm.sql-injection" 视为同类
    3. 同 file + 同 line + message 关键词重叠 → 兜底匹配
    """
    if a.file != b.file:
        return False
    if a.line is not None and b.line is not None and a.line != b.line:
        return False
    if a.line is None or b.line is None:
        # 行号未知的 finding 不参与去重，保留两条
        return False

    if a.rule_id == b.rule_id:
        return True

    a_cat = a.rule_id.split(".", 1)[-1]
    b_cat = b.rule_id.split(".", 1)[-1]
    if a_cat == b_cat and a_cat != "unknown":
        return True

    a_words = set(a.message.lower().split())
    b_words = set(b.message.lower().split())
    overlap = a_words & b_words - {"the", "a", "an", "in", "of", "to", "is", "and"}
    if len(overlap) >= 2:
        return True

    return False


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """合并确定性规则和 LLM 的重复发现。

    去重策略：按 (file, line, 问题类别) 匹配，保留信息更丰富的那条。
    优先保留有 suggestion 和更高 confidence 的 finding。
    如果两条都保留，取 severity 更高的。
    """
    if not findings:
        return []

    deduped: list[Finding] = []
    for f in findings:
        matched = False
        for i, existing in enumerate(deduped):
            if _findings_match(existing, f):
                # 合并：保留信息更丰富的那条
                if len(f.suggestion) > len(existing.suggestion):
                    deduped[i] = f
                elif len(f.suggestion) == len(existing.suggestion):
                    if f.confidence.value > existing.confidence.value:
                        deduped[i] = f
                matched = True
                break
        if not matched:
            deduped.append(f)

    return deduped


def _finalize(state: AgentState) -> dict:
    """从最后一条 AIMessage 的 content 中解析 LLM 审查结果，构建 ReviewReport。"""
    det_findings = state.get("deterministic_findings", [])
    llm_findings: list[Finding] = []

    # 找最后一条 AIMessage，从 content 解析 JSON
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                llm_findings = _parse_llm_findings(content)
            break

    raw_count = len(det_findings) + len(llm_findings)
    all_findings = _deduplicate_findings(det_findings + llm_findings)
    if raw_count > len(all_findings):
        logger.info("去重: %d 条 → %d 条 (移除 %d 条重复)", raw_count, len(all_findings), raw_count - len(all_findings))
    hunks = parse_diff(state["diff"])
    files_changed, lines_added, lines_removed = compute_metrics(hunks)
    verdict = determine_verdict(all_findings)

    if not all_findings:
        summary = "未发现问题，代码变更看起来良好。"
    else:
        blocker_count = sum(1 for f in all_findings if f.severity == Severity.BLOCKER)
        major_count = sum(1 for f in all_findings if f.severity == Severity.MAJOR)
        summary_parts = [f"共 {len(all_findings)} 条发现"]
        if blocker_count:
            summary_parts.append(f"{blocker_count} 条 blocker")
        if major_count:
            summary_parts.append(f"{major_count} 条 major")
        summary = f"审查完成：{', '.join(summary_parts)}。"

    forced = state.get("forced_finalize", False)
    if forced:
        summary += " ⚠️ 本次审查因 token 预算/循环检测/迭代上限提前终止，可能遗漏部分问题。"

    report = ReviewReport(
        verdict=verdict,
        summary=summary,
        findings=all_findings,
        metrics=DiffMetrics(files_changed=files_changed, lines_added=lines_added, lines_removed=lines_removed),
    )

    logger.info("报告生成完成: verdict=%s, %d 条发现", verdict.value, len(all_findings))
    return {"report": report.model_dump()}


def _parse_llm_findings(content: str) -> list[Finding]:
    """从 LLM 回复文本中解析 JSON 格式的审查结果。

    LLM 可能将 JSON 包裹在 ```json 代码块中或直接输出。
    也可能 LLM 被强制终止时输出的是纯文本——此时返回空列表。
    """
    import re as _re

    # 尝试提取 JSON：先找 ```json ... ``` 代码块，再找裸 JSON
    json_str = None
    json_block = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, _re.DOTALL)
    if json_block:
        json_str = json_block.group(1)
    else:
    # 尝试找第一个 { 到最后一个 } 之间的内容
        first_brace = content.find("{")
        last_brace = content.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            json_str = content[first_brace:last_brace + 1]

    if not json_str:
        logger.warning("LLM 回复中未找到 JSON 格式的审查结果")
        return []

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("LLM 回复的 JSON 解析失败: %s (内容前 200 字符: %s)", e, json_str[:200])
        return []

    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        return []

    findings: list[Finding] = []
    for rf in raw_findings:
        try:
            findings.append(Finding(
                rule_id=rf.get("rule_id", "llm.unknown"),
                severity=Severity(rf.get("severity", "info")),
                file=rf.get("file"),
                line=rf.get("line"),
                message=rf.get("message", ""),
                suggestion=rf.get("suggestion", ""),
                confidence=Confidence(rf.get("confidence", "medium")),
                source="llm",
            ))
        except Exception as e:
            logger.warning(
                "跳过格式错误的 LLM finding: %s (数据: %s)",
                e, json.dumps(rf, ensure_ascii=False)[:200],
            )

    return findings


def build_graph(model_name: str = "DeepSeek-V4-Flash", temperature: float = 0.1):
    """构建 LangGraph 状态机，集成中间件链。"""
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("XITA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 API key，请设置 OPENAI_API_KEY 或 XITA_API_KEY 环境变量。"
        )
    llm = ChatOpenAI(
        model=model_name,
        temperature=temperature,
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=api_key,
    )
    chain = build_default_chain()

    # 每次审查开始时重置中间件上下文，防止状态泄漏。
    # 注意：chain 与 graph 实例绑定，每次 build_graph 创建新的 chain。
    # 调用方应每次审查调 build_graph()，不要复用 graph 实例跨多次 invoke，
    # 否则 MiddlewareContext 可能在并发 invoke 间产生竞态。
    def prepare_with_reset(state: AgentState) -> dict:
        chain.reset()
        return _prepare_review(state)

    graph = StateGraph(AgentState)

    graph.add_node("prepare", prepare_with_reset)
    graph.add_node("llm", _make_llm_node(llm, chain))
    graph.add_node("tools", lambda s: _execute_tools(s, chain))
    graph.add_node("finalize", _finalize)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "llm")
    graph.add_conditional_edges("llm", _should_continue, {"tools": "tools", "finalize": "finalize"})
    graph.add_edge("tools", "llm")
    graph.add_edge("finalize", END)

    return graph.compile()
