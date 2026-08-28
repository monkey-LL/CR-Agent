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
import time

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
from cr_agent.observability.metrics import (
    finalize_metrics,
    init_metrics,
    record_iteration,
    record_phase,
    record_token_usage,
    record_tool_call,
)
from cr_agent.security.sanitizer import sanitize_input

logger = logging.getLogger(__name__)
MAX_ITERATIONS = 15

# 模块级熔断器单例：跨多次审查共享状态，避免每次 build_graph 重置
from cr_agent.observability.logger import CircuitBreaker as _CircuitBreaker
_llm_circuit_breaker = _CircuitBreaker(threshold=5, recovery_timeout=60.0)


def _prepare_review(state: AgentState) -> dict:
    """入口节点：确定性检查 + 构建 prompt。"""
    diff = state["diff"]
    pr_info = state["pr_info"]
    memory_context = state.get("memory_context", "")

    # 在入口处截断 diff，确保后续所有环节使用同一份数据
    max_diff_chars = 50_000
    diff_truncated = False
    if len(diff) > max_diff_chars:
        diff_truncated = True

    # 在截断前计算 metrics，避免截断落在 hunk 中间导致统计错误
    full_hunks = parse_diff(diff)
    files_changed, lines_added, lines_removed = compute_metrics(full_hunks)
    diff_metrics = {
        "files_changed": files_changed,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }

    if diff_truncated:
        diff = diff[:max_diff_chars]

    hunks = parse_diff(diff)
    det_findings = run_deterministic_checks(hunks)

    # 计算已审查和未审查的文件清单
    full_files = sorted({h.file for h in full_hunks if h.file})
    reviewed_files = sorted({h.file for h in hunks if h.file})
    unreviewed_files = sorted(set(full_files) - set(reviewed_files))

    safe_diff = sanitize_input(diff)
    safe_pr_info = {
        k: sanitize_input(str(v)) if isinstance(v, str) else v
        for k, v in pr_info.items()
    }
    safe_memory = sanitize_input(memory_context) if memory_context else ""

    # 按当前 diff 涉及的文件检索历史审查记录，注入精准上下文
    diff_file_names = [h.file for h in full_hunks if h.file]
    try:
        from cr_agent.agent.memory import build_file_memory_context
        file_memory = build_file_memory_context(
            safe_pr_info.get("repo", ""), diff_file_names
        )
        if file_memory:
            safe_memory += sanitize_input(file_memory)
    except Exception as e:
        logger.debug("File memory context failed: %s", e)

    user_msg = HumanMessage(
        content=build_review_prompt(safe_pr_info, safe_diff, det_findings, safe_memory)
    )
    system_msg = SystemMessage(content=SYSTEM_PROMPT)

    logger.info(
        "审查准备完成: %d 个文件, +%d/-%d 行, %d 条确定性发现%s",
        files_changed, lines_added, lines_removed, len(det_findings),
        " (diff 已截断)" if diff_truncated else "",
    )

    return {
        "messages": [system_msg, user_msg],
        "deterministic_findings": det_findings,
        "diff": diff,
        "report": None,
        "iteration": 0,
        "diff_metrics": diff_metrics,
        "diff_truncated": diff_truncated,
        "reviewed_files": reviewed_files,
        "unreviewed_files": unreviewed_files,
    }


def _make_llm_node(llm: ChatOpenAI, chain: MiddlewareChain, model_name: str = ""):
    """创建 LLM 决策节点，集成中间件。"""
    from cr_agent.observability.logger import CircuitBreakerOpenError, retry_with_backoff

    def _llm_decide(state: AgentState) -> dict:
        iteration = state.get("iteration", 0)
        record_iteration()
        phase_start = time.time()

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
            # 记录 token 使用量
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                record_token_usage(
                    call_index=iteration + 1,
                    prompt_tokens=response.usage_metadata.get("input_tokens", 0),
                    completion_tokens=response.usage_metadata.get("output_tokens", 0),
                    total_tokens=response.usage_metadata.get("total_tokens", 0),
                    model=model_name,
                )
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

        record_phase(f"llm_iter_{iteration + 1}", phase_start)
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
    phase_start = time.time()

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
        record_tool_call()

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

    record_phase("tools", phase_start)
    return {"messages": tool_results}


def _should_continue(state: AgentState) -> str:
    """条件边：走向 tools 还是 finalize。"""
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "finalize"


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """对 findings 按 (file, line) 去重，同位置只保留 severity 最高的。

    确定性 findings 优先于 LLM findings（同位置同 severity 时保留确定性来源）。
    """
    severity_order = {
        Severity.BLOCKER: 0,
        Severity.MAJOR: 1,
        Severity.MINOR: 2,
        Severity.INFO: 3,
    }
    best: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.file, f.line)
        if key not in best:
            best[key] = f
            continue
        existing = best[key]
        existing_rank = severity_order.get(existing.severity, 99)
        new_rank = severity_order.get(f.severity, 99)
        if new_rank < existing_rank:
            best[key] = f
        elif new_rank == existing_rank and existing.source == "llm" and f.source == "deterministic":
            best[key] = f
    return list(best.values())


def _finalize(state: AgentState) -> dict:
    """从最后一条 AIMessage 的 content 中解析 LLM 审查结果，构建 ReviewReport。"""
    phase_start = time.time()
    det_findings = state.get("deterministic_findings", [])
    llm_findings: list[Finding] = []

    # 找最后一条 AIMessage，从 content 解析 JSON
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                llm_findings = _parse_llm_findings(content)
            break

    all_findings = det_findings + llm_findings
    all_findings = _dedup_findings(all_findings)

    # 优先使用 _prepare_review 在截断前算好的 metrics，避免截断后重算不准
    diff_metrics = state.get("diff_metrics")
    if diff_metrics:
        files_changed = diff_metrics["files_changed"]
        lines_added = diff_metrics["lines_added"]
        lines_removed = diff_metrics["lines_removed"]
    else:
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
    if state.get("diff_truncated"):
        unreviewed = state.get("unreviewed_files", [])
        reviewed = state.get("reviewed_files", [])
        summary += f" ⚠️ diff 过大已被截断，已审查 {len(reviewed)}/{len(reviewed) + len(unreviewed)} 个文件。"
        if unreviewed:
            files_list = ", ".join(unreviewed[:10])
            if len(unreviewed) > 10:
                files_list += f" 等 {len(unreviewed)} 个"
            summary += f" 以下文件未被审查：{files_list}。建议拆分 PR 后重新审查。"

    report = ReviewReport(
        verdict=verdict,
        summary=summary,
        findings=all_findings,
        metrics=DiffMetrics(files_changed=files_changed, lines_added=lines_added, lines_removed=lines_removed),
    )

    logger.info("报告生成完成: verdict=%s, %d 条发现", verdict.value, len(all_findings))
    record_phase("finalize", phase_start)
    finalize_metrics()
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
        init_metrics()
        phase_start = time.time()
        result = _prepare_review(state)
        record_phase("prepare", phase_start)
        return result

    graph = StateGraph(AgentState)

    graph.add_node("prepare", prepare_with_reset)
    graph.add_node("llm", _make_llm_node(llm, chain, model_name))
    graph.add_node("tools", lambda s: _execute_tools(s, chain))
    graph.add_node("finalize", _finalize)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "llm")
    graph.add_conditional_edges("llm", _should_continue, {"tools": "tools", "finalize": "finalize"})
    graph.add_edge("tools", "llm")
    graph.add_edge("finalize", END)

    return graph.compile()
