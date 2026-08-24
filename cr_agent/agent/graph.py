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
from cr_agent.core.diff_parser import compute_metrics, parse_diff
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


def _prepare_review(state: AgentState) -> dict:
    """入口节点：确定性检查 + 构建 prompt。"""
    diff = state["diff"]
    pr_info = state["pr_info"]
    memory_context = state.get("memory_context", "")

    hunks = parse_diff(diff)
    det_findings = run_deterministic_checks(hunks)
    files_changed, lines_added, lines_removed = compute_metrics(hunks)

    safe_diff = sanitize_input(diff)
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
        "审查准备完成: %d 个文件, +%d/-%d 行, %d 条确定性发现",
        files_changed, lines_added, lines_removed, len(det_findings),
    )

    return {
        "messages": [system_msg, user_msg],
        "deterministic_findings": det_findings,
        "report": None,
        "iteration": 0,
    }


def _make_llm_node(llm: ChatOpenAI, chain: MiddlewareChain):
    """创建 LLM 决策节点，集成中间件。"""
    from cr_agent.observability.logger import CircuitBreaker, CircuitBreakerOpenError, retry_with_backoff

    circuit_breaker = CircuitBreaker(threshold=5, recovery_timeout=60.0)

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
            return circuit_breaker.call(llm_with_tools.invoke, state_dict["messages"])

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


def _finalize(state: AgentState) -> dict:
    """从对话历史中构建最终的 ReviewReport。"""
    det_findings = state.get("deterministic_findings", [])
    llm_findings: list[Finding] = []

    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc["name"] == "generate_report":
                    args = tc["args"]
                    raw_findings = args.get("findings", [])
                    if isinstance(raw_findings, str):
                        try:
                            raw_findings = json.loads(raw_findings)
                        except json.JSONDecodeError:
                            raw_findings = []
                    for rf in raw_findings:
                        try:
                            llm_findings.append(Finding(
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
                    break
            else:
                continue
            break

    all_findings = det_findings + llm_findings
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

    # 每次审查开始时重置中间件上下文，防止状态泄漏
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
