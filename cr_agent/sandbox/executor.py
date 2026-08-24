"""沙箱：用于 lint、类型检查和 git 命令的隔离执行环境。

生产环境加固:
  - 不使用 shell=True: 命令通过 shlex 拆分后以参数列表形式传递
  - 命令白名单: 仅允许预批准的工具前缀运行
  - 环境变量净化: 剥离 secret，仅传递安全变量
  - 超时强制执行: lint 挂起 = review 阻塞，而非无限等待
  - 输出截断: 保护 token 预算
  - 优雅降级: lint 失败 → "skipped"，而非崩溃
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cr_agent.core.models import StaticAnalysisResult
from cr_agent.security.env_sanitizer import build_safe_env

logger = logging.getLogger(__name__)

# 允许的命令前缀白名单（二进制名称或完整路径）。
# 不在此列表中的命令将被拒绝。
_ALLOWED_COMMANDS = {
    "ruff", "mypy", "pylint", "flake8", "pyright",
    "eslint", "tsc", "prettier", "stylelint",
    "gh", "git",
    "python3", "node", "npm", "npx", "yarn", "pnpm",
    "go", "golangci-lint", "staticcheck",
    "rustc", "cargo", "clippy",
    "java", "javac", "spotbugs",
    "cat", "head", "wc",
}

# python3 / node 中允许任意代码执行的 flag —— 必须拒绝
_FORBIDDEN_FLAGS = {"-c", "--command", "-e", "--eval"}

# 命令中绝不允许出现的子串（针对注入的纵深防御）。
# 注意: 禁用单个 | 以防止管道操作。带空格的 ; 用于防止命令链式执行，
# 同时允许引号内字符串中的分号（在 shell 命令中很少见）。
_FORBIDDEN_PATTERNS = re.compile(
    r"(;|\|\||&&|`|\$\(|\|\s|>\s*/dev/|rm\s+-rf|curl\s|wget\s|nc\s|/bin/sh|/bin/bash)",
    re.IGNORECASE,
)


@dataclass
class SandboxConfig:
    """沙箱执行配置。"""
    timeout: int = 30
    max_output_chars: int = 2000
    allowed_cwd: str | None = None  # 如果设置，cwd 必须在此路径之下


class SandboxSecurityError(Exception):
    """当命令违反沙箱安全策略时抛出。"""


def _validate_command(command: str) -> list[str]:
    """校验命令字符串并将其拆分为安全的参数列表。

    安全检查:
    1. 拒绝包含 shell 元字符的命令（;, ``, $(), &&, ||）
    2. 使用 shlex 拆分（正确处理带引号的参数）
    3. 检查二进制名称是否在白名单中
    4. 拒绝空命令

    Returns:
        适用于 subprocess.run(args=...) 的参数列表。

    Raises:
        SandboxSecurityError: 如果命令未通过校验。
    """
    if not command or not command.strip():
        raise SandboxSecurityError("Empty command")

    # 检查禁止的模式（shell 注入向量）
    if _FORBIDDEN_PATTERNS.search(command):
        raise SandboxSecurityError(
            f"Command contains forbidden pattern (shell metacharacter or dangerous command): {command[:100]}"
        )

    # 拆分为参数列表 —— shlex 能正确处理引号
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise SandboxSecurityError(f"Invalid command syntax: {e}")

    if not args:
        raise SandboxSecurityError("Empty command after parsing")

    # 检查二进制名称是否在白名单中
    binary = Path(args[0]).name  # 处理完整路径如 /usr/bin/ruff
    if binary not in _ALLOWED_COMMANDS:
        raise SandboxSecurityError(
            f"Command '{binary}' is not in the allowed list. "
            f"Allowed: {', '.join(sorted(_ALLOWED_COMMANDS))}"
        )

    # 拒绝解释器（python3、node）上的 -c / --command flag，以防止
    # 任意代码执行: `python3 -c "import os; os.system('...')"` 会绕过
    # 所有 shell 元字符检查。
    if binary in ("python3", "node") and len(args) > 1:
        for arg in args[1:]:
            if arg in _FORBIDDEN_FLAGS:
                raise SandboxSecurityError(
                    f"'{binary} {arg}' is not allowed: arbitrary code execution risk"
                )
            # 遇到第一个非 flag 参数（即脚本/模块）后停止检查
            if not arg.startswith("-"):
                break

    return args


def _validate_cwd(cwd: str, config: SandboxConfig) -> str:
    """校验 cwd 是否安全且在允许的边界内。"""
    cwd_path = Path(cwd).resolve()

    if not cwd_path.exists():
        raise SandboxSecurityError(f"Working directory does not exist: {cwd}")

    if not cwd_path.is_dir():
        raise SandboxSecurityError(f"Working directory is not a directory: {cwd}")

    if config.allowed_cwd:
        allowed_root = Path(config.allowed_cwd).resolve()
        try:
            cwd_path.relative_to(allowed_root)
        except ValueError:
            raise SandboxSecurityError(
                f"Working directory {cwd} is outside allowed root {config.allowed_cwd}"
            )

    return str(cwd_path)


def run_command(
    command: str,
    cwd: str = ".",
    config: SandboxConfig | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """在沙箱中运行 shell 命令，使用净化后的环境变量。

    安全措施:
    - 不使用 shell=True: 命令经过校验、shlex 拆分后以参数列表形式执行
    - 命令白名单: 仅允许预批准的工具运行
    - 环境变量净化: 仅传递安全变量，secret 被脱敏
    - 拒绝 shell 元字符: 不允许 ;, |, &&, $(), 反引号
    - cwd 校验: 必须存在且在允许的边界内
    """
    config = config or SandboxConfig()

    try:
        args = _validate_command(command)
        safe_cwd = _validate_cwd(cwd, config)
    except SandboxSecurityError as e:
        logger.warning("Sandbox rejected command: %s", e)
        return -1, f"Command rejected by sandbox: {e}"

    safe_env = build_safe_env(extra_env)

    try:
        result = subprocess.run(
            args,  # 不使用 shell=True —— 直接传递参数列表
            capture_output=True,
            text=True,
            timeout=config.timeout,
            cwd=safe_cwd,
            env=safe_env,
        )
        output = (result.stdout + result.stderr)[:config.max_output_chars]
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {config.timeout}s"
    except FileNotFoundError:
        return -1, f"Command not found: {args[0]}"
    except Exception as e:
        return -1, f"Error: {type(e).__name__}: {e}"


def run_lint_check(
    tool: str,
    command: str,
    cwd: str = ".",
    config: SandboxConfig | None = None,
) -> StaticAnalysisResult:
    """运行 lint 工具并返回结构化结果。

    优雅降级: 如果工具未安装或运行失败，返回
    status="skipped"，使 review 流程在没有该分析的情况下继续。
    """
    exit_code, output = run_command(command, cwd=cwd, config=config)

    if exit_code == -1:
        return StaticAnalysisResult(
            tool=tool, status="skipped", issues=0, output=output,
        )

    issue_count = 0
    if exit_code != 0:
        if "error" in output.lower():
            issue_count = output.lower().count("error")
        elif "warning" in output.lower():
            issue_count = output.lower().count("warning")
        else:
            issue_count = 1

    return StaticAnalysisResult(
        tool=tool,
        status="pass" if exit_code == 0 else "fail",
        issues=issue_count,
        output=output,
    )