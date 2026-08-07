"""Agent state definition — the shared memory across all graph nodes.

Learning focus:
  - TypedDict vs Pydantic for LangGraph state
  - How state flows through the graph (each node reads + writes state)
  - Reducer annotations (operator.add for appending instead of overwriting)

The state is the single source of truth. Each node receives the current state,
does some work, and returns a partial state update. LangGraph merges the update
back into the full state using the reducers defined here.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage

from cr_agent.core.models import Finding, StaticAnalysisResult


class AgentState(TypedDict):
    """All data that flows through the CR Agent graph.

    Attributes:
        messages: Conversation history (system prompt + tool calls + LLM responses).
            Uses operator.add reducer so new messages are appended, not replaced.
        diff: The raw PR diff text. Set once at the start.
        pr_info: PR metadata (number, repo, title, author, base/head branches).
        deterministic_findings: Findings from regex rules. Appended by review node.
        llm_findings: Findings from LLM semantic analysis. Appended by LLM node.
        static_analysis: Results from lint/type-check tools. Appended by sandbox node.
        file_contents: Dict of file_path -> full content, for files the agent read.
        report: The final ReviewReport, set by the report node.
        iteration: How many tool-call loops we've done (for recursion limit).
    """

    messages: Annotated[list[BaseMessage], operator.add]
    diff: str
    pr_info: dict
    deterministic_findings: Annotated[list[Finding], operator.add]
    llm_findings: Annotated[list[Finding], operator.add]
    static_analysis: Annotated[list[StaticAnalysisResult], operator.add]
    file_contents: dict[str, str]
    report: dict | None
    iteration: int
