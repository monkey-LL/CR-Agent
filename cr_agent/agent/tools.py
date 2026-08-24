"""Agent 工具——LLM 可以选择调用的动作。

学习重点：
  - @tool 装饰器：LangChain 如何将函数转换为工具 schema
  - 工具的 docstring 会成为 LLM 看到的工具描述（要清晰、具体！）
  - 返回类型很重要：LLM 看到的是 ToolMessage 格式的返回值
  - 错误处理：返回错误信息，不要抛异常（Agent 可以恢复）

工具是 Agent 的"手"。LLM 根据以下信息决定调用哪个工具：
  1. 工具的名称和 docstring（这是它"看到"的内容）
  2. 当前上下文（它已经知道什么）
  3. 它接下来需要完成什么

关键理解：LLM 不"执行代码"——它发出一个结构化的请求来
调用工具，我们的代码执行它并返回结果。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import tool


@tool
def run_lint(command: str, cwd: str = ".") -> str:
    """运行 lint 或 type-check 命令并返回输出。

    可用于运行项目的 linter，如：
    - Python: "ruff check ." 或 "mypy ."
    - JavaScript: "npx eslint ." 或 "npx tsc --noEmit"

    命令在沙箱中执行，有命令白名单限制。
    Shell 元字符（;, |, &&, $(), 反引号）会被拒绝。

    Args:
        command: 要执行的 lint 命令（如 "ruff check . --output-format=json"）。
        cwd: 命令的工作目录（默认为当前目录）。
    """
    from cr_agent.sandbox.executor import SandboxConfig, run_command

    try:
        exit_code, output = run_command(command, cwd=cwd, config=SandboxConfig())
    except Exception as e:
        return f"Lint error: {type(e).__name__}: {e}. Skipped."

    if exit_code == -1:
        return f"Lint skipped: {output}"

    if exit_code == 0:
        return f"Lint passed (exit 0):\n{output[:2000]}"
    else:
        return f"Lint failed (exit {exit_code}):\n{output[:2000]}"


@tool
def read_file(path: str, max_lines: int = 200) -> str:
    """读取文件内容以获取 diff 之外的完整上下文。

    仅在 diff 上下文不足以理解变更时使用。
    不要读取每个文件。

    路径会经过路径遍历攻击校验。

    Args:
        path: 要读取的文件路径（相对于仓库根目录）。
        max_lines: 最大读取行数（默认 200，防止 token 爆炸）。
    """
    from cr_agent.security.sanitizer import validate_path

    try:
        safe_path = validate_path(path)
        p = Path(safe_path)
        if not p.exists():
            return f"File not found: {path}"
        content = p.read_text(errors="replace")
        lines = content.split("\n")[:max_lines]
        return "\n".join(lines)
    except ValueError as e:
        return f"Path validation error: {e}"
    except Exception as e:
        return f"Error reading {path}: {type(e).__name__}: {e}"


@tool
def generate_report(
    verdict: str,
    summary: str,
    findings: str,
    files_reviewed: int = 0,
    lines_added: int = 0,
    lines_removed: int = 0,
) -> str:
    """生成最终的代码审查报告。

    当你完成分析后调用此工具。

    Args:
        verdict: 审查结论，取值为 "approve"、"request_changes" 或 "block"。
        summary: 1-3 句话的审查概述。
        findings: finding 对象的 JSON 数组。每个 finding 包含：
            rule_id (str), severity (blocker/major/minor/info), file (str),
            line (int), message (str), suggestion (str), confidence (high/medium/low)。
        files_reviewed: 审查的文件数量。
        lines_added: diff 中新增的行数。
        lines_removed: diff 中删除的行数。
    """
    # 实际的报告构建在处理此工具调用的 graph 节点中进行。
    # 这里只是校验并透传。
    try:
        parsed_findings = json.loads(findings) if isinstance(findings, str) else findings
    except json.JSONDecodeError:
        parsed_findings = []

    report = {
        "verdict": verdict,
        "summary": summary,
        "findings": parsed_findings,
        "metrics": {
            "files_reviewed": files_reviewed,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        },
    }
    return json.dumps(report, ensure_ascii=False)


# 暴露给 LLM 的工具列表
ALL_TOOLS = [run_lint, read_file, generate_report]
