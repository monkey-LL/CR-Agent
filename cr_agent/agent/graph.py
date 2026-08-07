"""LangGraph state machine with middleware chain integration.

Architecture:
  START → prepare → llm ↔ tools → finalize → END

  The middleware chain wraps each node:
  - before_model: InputSanitization, ContextCompression
  - after_model: LoopDetection, TokenBudget
  - before_tool: LoopDetection (blocked tools)
  - after_tool: ToolErrorHandling, ToolOutputBudget, InputSanitization (mask secrets)

  The LLM is NOT in control — our graph + middleware is. The LLM only suggests
  which tool to call; middleware decides whether to allow it, and the graph
  decides whether to loop or finalize.
"""

from __future__ import annotations

import json
import logging
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from cr_agent.agent.prompts import SYSTEM_PROMPT, build_review_prompt
from cr_agent.agent.state import AgentState
from cr_agent.agent.tools import ALL_TOOLS
from cr_agent.agent.middlewares import build_default_chain, MiddlewareChain
from cr_agent.agent.middlewares.tool_error_handling import ToolErrorHandler
from cr_agent.core.diff_parser import parse_diff, compute_metrics
from cr_agent.core.models import (
    Confidence, DiffMetrics, Finding, ReviewReport, Severity, determine_verdict,
)
from cr_agent.core.rules_engine import run_deterministic_checks
from cr_agent.security.sanitizer import sanitize_input, mask_secrets
from cr_agent.observability.tracing import new_trace_id, get_trace_id

logger = logging.getLogger(__name__)
MAX_ITERATIONS = 15


def _prepare_review(state: AgentState) -> dict:
    """Entry node: deterministic checks + build prompt."""
    diff = state["diff"]
    pr_info = state["pr_info"]

    hunks = parse_diff(diff)
    det_findings = run_deterministic_checks(hunks)
    files_changed, lines_added, lines_removed = compute_metrics(hunks)

    safe_diff = sanitize_input(diff)
    safe_pr_info = {
        k: sanitize_input(str(v)) if isinstance(v, str) else v
        for k, v in pr_info.items()
    }

    user_msg = HumanMessage(content=build_review_prompt(safe_pr_info, safe_diff, det_findings))
    system_msg = SystemMessage(content=SYSTEM_PROMPT)

    logger.info(
        "Prepared review: %d files, +%d/-%d lines, %d deterministic findings",
        files_changed, lines_added, lines_removed, len(det_findings),
    )

    return {
        "messages": [system_msg, user_msg],
        "deterministic_findings": det_findings,
        "file_contents": {},
        "report": None,
        "iteration": 0,
    }


def _make_llm_node(llm: ChatOpenAI, chain: MiddlewareChain):
    """Create the LLM decision node with middleware integration."""

    def _llm_decide(state: AgentState) -> dict:
        iteration = state.get("iteration", 0)

        if iteration >= MAX_ITERATIONS:
            logger.warning("Hit max iterations (%d), forcing finalization", iteration)
            return {
                "messages": [AIMessage(content="Max iterations reached. Generating report with available findings.")],
                "iteration": iteration + 1,
            }

        # Run before_model middleware (sanitization, compression)
        state_dict = dict(state)
        state_dict["messages"] = list(state.get("messages", []))
        state_dict = chain.run_before_model(state_dict)

        # Call LLM
        llm_with_tools = llm.bind_tools(ALL_TOOLS)
        response = llm_with_tools.invoke(state_dict["messages"])

        # Run after_model middleware (loop detection, token budget, secret masking)
        response = chain.run_after_model(state_dict, response)

        # Check if middleware forced finalization
        if chain.should_finalize():
            logger.info("Middleware forced finalization")

        chain.ctx.iteration = iteration + 1

        return {
            "messages": [response],
            "iteration": iteration + 1,
        }

    return _llm_decide


def _execute_tools(state: AgentState, chain: MiddlewareChain) -> dict:
    """Execute tool calls with middleware wrapping."""
    last_msg = state["messages"][-1]
    tool_map = {t.name: t for t in ALL_TOOLS}
    tool_results = []

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        tool_call_id = tc["id"]

        # Run before_tool middleware (loop detection, authorization)
        blocked = chain.run_before_tool(state, tc)
        if blocked:
            tool_results.append(ToolMessage(
                content=blocked.get("error", "Tool blocked by middleware"),
                tool_call_id=tool_call_id,
            ))
            continue

        logger.info("Executing tool: %s", tool_name)

        # Execute with error handling
        handler = ToolErrorHandler(tool_name)
        with handler:
            result = tool_map[tool_name].invoke(tool_args)

        if handler.error:
            result_str = handler.error_message
        else:
            result_str = str(result)

        # Run after_tool middleware (output budget, error formatting, secret masking)
        result_str = chain.run_after_tool(state, result_str)

        tool_results.append(ToolMessage(
            content=result_str,
            tool_call_id=tool_call_id,
        ))

    return {"messages": tool_results}


def _should_continue(state: AgentState) -> str:
    """Conditional edge: tools or finalize."""
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "finalize"


def _finalize(state: AgentState) -> dict:
    """Build the final ReviewReport from conversation."""
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
                        except Exception:
                            pass
                    break
            else:
                continue
            break

    all_findings = det_findings + llm_findings
    hunks = parse_diff(state["diff"])
    files_changed, lines_added, lines_removed = compute_metrics(hunks)
    verdict = determine_verdict(all_findings)

    if not all_findings:
        summary = "No issues found. The code changes look good."
    else:
        blocker_count = sum(1 for f in all_findings if f.severity == Severity.BLOCKER)
        major_count = sum(1 for f in all_findings if f.severity == Severity.MAJOR)
        summary_parts = [f"{len(all_findings)} findings"]
        if blocker_count:
            summary_parts.append(f"{blocker_count} blocker(s)")
        if major_count:
            summary_parts.append(f"{major_count} major(s)")
        summary = f"Review completed with {', '.join(summary_parts)}."

    report = ReviewReport(
        verdict=verdict,
        summary=summary,
        findings=all_findings,
        metrics=DiffMetrics(files_changed=files_changed, lines_added=lines_added, lines_removed=lines_removed),
    )

    logger.info("Finalized report: verdict=%s, %d findings", verdict.value, len(all_findings))
    return {"report": report.model_dump()}


def build_graph(model_name: str = "DeepSeek-V4-Flash", temperature: float = 0.1) -> any:
    """Build the LangGraph with middleware chain."""
    llm = ChatOpenAI(
        model=model_name,
        temperature=temperature,
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    chain = build_default_chain()

    graph = StateGraph(AgentState)

    graph.add_node("prepare", _prepare_review)
    graph.add_node("llm", _make_llm_node(llm, chain))
    graph.add_node("tools", lambda s: _execute_tools(s, chain))
    graph.add_node("finalize", _finalize)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "llm")
    graph.add_conditional_edges("llm", _should_continue, {"tools": "tools", "finalize": "finalize"})
    graph.add_edge("tools", "llm")
    graph.add_edge("finalize", END)

    return graph.compile()
