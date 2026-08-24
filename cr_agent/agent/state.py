"""Agent 状态定义——所有 graph 节点共享的内存。

学习重点：
  - TypedDict vs Pydantic 在 LangGraph state 中的选择
  - state 如何在 graph 中流转（每个节点读取 + 写入 state）
  - Reducer 注解（operator.add 用于追加而非覆盖）

state 是唯一的真相来源。每个节点接收当前 state，
做一些工作，然后返回一个部分 state 更新。LangGraph 使用这里
定义的 reducer 将更新合并回完整 state。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage

from cr_agent.core.models import Finding


class AgentState(TypedDict):
    """CR Agent graph 中流转的所有数据。

    Attributes:
        messages: 对话历史（系统提示词 + 工具调用 + LLM 响应）。
            使用 operator.add reducer，新消息会追加而非替换。
        diff: 原始 PR diff 文本，在开始时设置一次。
        pr_info: PR 元数据（编号、仓库、标题、作者、base/head 分支）。
        deterministic_findings: 正则规则的发现，由 prepare 节点设置。
        report: 最终的 ReviewReport，由 finalize 节点设置。
        iteration: 工具调用循环次数（用于递归限制）。
        memory_context: 该仓库的历史审查模式，注入 LLM prompt
            让 Agent 优先关注反复出现的问题。
        forced_finalize: 中间件是否强制终止（token 预算/循环检测/迭代上限），
            用于 finalize 节点在报告中标注审查不完整。
    """

    messages: Annotated[list[BaseMessage], operator.add]
    diff: str
    pr_info: dict
    deterministic_findings: Annotated[list[Finding], operator.add]
    report: dict | None
    iteration: int
    memory_context: str
    forced_finalize: bool
