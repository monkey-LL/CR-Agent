"""确定性规则引擎——基于正则表达式的代码模式检测。

学习重点：
  - 规则设计：pattern + severity + message + suggestion
  - 只检查新增行（不检查上下文行和删除行）
  - 扫描时追踪行号
  - 通过精确的模式减少误报

为什么需要确定性规则？
  LLM 擅长语义分析但不擅长可靠的模式匹配。
  硬编码密钥或 SQL 注入等模式更适合用正则捕获：
  - 快速（无需 LLM 调用）
  - 确定性（相同输入 → 相同输出）
  - 高置信度（正则匹配 = 高精度）
  LLM 则聚焦于逻辑、架构和微妙的问题。
"""

from __future__ import annotations

import re

from cr_agent.core.diff_parser import DiffHunk
from cr_agent.core.models import Confidence, Finding, Severity

# 每条规则是一个 dict，包含：
#   rule_id: 唯一标识符（类别.具体问题）
#   pattern: 匹配新增行内容的正则表达式（不含 '+' 前缀）
#   severity: 严重度级别
#   message: 人类可读的问题描述
#   suggestion: 可执行的修复建议
#   languages: 可选的文件扩展名集合（None = 所有语言）
DETERMINISTIC_RULES: list[dict] = [
    # ===== 安全：Blocker =====
    {
        "rule_id": "security.hardcoded-secret",
        "pattern": r"(?:password|secret|api_key|apikey|token|access_key|private_key)\s*=\s*['\"][^'\"]{8,}",
        "severity": Severity.BLOCKER,
        "message": "Potential hardcoded secret detected.",
        "suggestion": "Use environment variables or a secret manager. Never commit credentials.",
    },
    {
        "rule_id": "security.sql-injection",
        "pattern": r"(?:execute|query|cursor\.execute)\s*\(\s*f['\"][^'\"]*\{.*\}",
        "severity": Severity.BLOCKER,
        "message": "Potential SQL injection via f-string query construction.",
        "suggestion": "Use parameterized queries: cursor.execute('SELECT ... WHERE id = %s', (id,))",
    },
    {
        "rule_id": "security.command-injection",
        "pattern": r"(?:os\.system|subprocess\.(?:call|run|Popen)|eval|exec)\s*\(\s*f['\"]",
        "severity": Severity.BLOCKER,
        "message": "Potential command injection via f-string in exec/system call.",
        "suggestion": "Use shell=False with argument lists; never interpolate untrusted input.",
    },
    {
        "rule_id": "security.eval-usage",
        "pattern": r"\beval\s*\(",
        "severity": Severity.BLOCKER,
        "message": "Use of eval() is dangerous and can execute arbitrary code.",
        "suggestion": "Use ast.literal_eval() for safe literal parsing, or restructure the code.",
    },
    {
        "rule_id": "security.exec-usage",
        "pattern": r"\bexec\s*\(",
        "severity": Severity.BLOCKER,
        "message": "Use of exec() is dangerous and can execute arbitrary code.",
        "suggestion": "Restructure the code to avoid dynamic execution.",
    },
    {
        "rule_id": "security.pickledeserialize",
        "pattern": r"pickle\.loads?\s*\(",
        "severity": Severity.BLOCKER,
        "message": "Pickle deserialization is unsafe for untrusted data (RCE risk).",
        "suggestion": "Use JSON or a safe serialization format. If pickle is required, validate input.",
    },
    {
        "rule_id": "security.yaml-unsafe-load",
        "pattern": r"yaml\.load\s*\((?!\s*Loader\s*=\s*yaml\.SafeLoader)",
        "severity": Severity.BLOCKER,
        "message": "yaml.load() without SafeLoader can execute arbitrary code.",
        "suggestion": "Use yaml.safe_load() or yaml.load(data, Loader=yaml.SafeLoader).",
    },
    {
        "rule_id": "security.shell-true",
        "pattern": r"subprocess\.(?:run|call|Popen|check_output)\s*\([^)]*shell\s*=\s*True",
        "severity": Severity.BLOCKER,
        "message": "subprocess with shell=True allows command injection.",
        "suggestion": "Pass arguments as a list with shell=False.",
    },

    # ===== 安全：Major =====
    {
        "rule_id": "security.weak-hash",
        "pattern": r"hashlib\.(md5|sha1)\s*\(",
        "severity": Severity.MAJOR,
        "message": "Weak hash function (MD5/SHA1) is cryptographically broken.",
        "suggestion": "Use hashlib.sha256 or stronger for security-sensitive contexts.",
    },
    {
        "rule_id": "security.verify-false",
        "pattern": r"verify\s*=\s*False",
        "severity": Severity.MAJOR,
        "message": "SSL certificate verification disabled.",
        "suggestion": "Never disable SSL verification in production. Fix the certificate chain instead.",
    },
    {
        "rule_id": "security.insecure-random",
        "pattern": r"random\.random\s*\(|random\.randint\s*\(",
        "severity": Severity.MAJOR,
        "message": "Insecure random number generator for security-sensitive context.",
        "suggestion": "Use secrets module or os.urandom() for cryptographic randomness.",
    },
    {
        "rule_id": "security.assert-sensitive",
        "pattern": r"\bassert\s+.*(?:password|secret|token|key|auth)",
        "severity": Severity.MAJOR,
        "message": "Using assert for sensitive checks — assert is stripped with -O flag.",
        "suggestion": "Use if/raise ValueError for security checks that must always run.",
    },
    {
        "rule_id": "security.tempfile-race",
        "pattern": r"tempfile\.mktemp\s*\(",
        "severity": Severity.MAJOR,
        "message": "tempfile.mktemp() is deprecated and vulnerable to race conditions.",
        "suggestion": "Use tempfile.mkstemp() or tempfile.NamedTemporaryFile().",
    },

    # ===== 调试：Major =====
    {
        "rule_id": "debug.breakpoint",
        "pattern": r"^\s*(breakpoint|pdb\.set_trace|import pdb)\b",
        "severity": Severity.MAJOR,
        "message": "Debugger breakpoint left in code.",
        "suggestion": "Remove the breakpoint before merging.",
    },

    # ===== 调试：Minor =====
    {
        "rule_id": "debug.print-statement",
        "pattern": r"^\s*(print|console\.log)\s*\(",
        "severity": Severity.MINOR,
        "message": "Debug print statement left in code.",
        "suggestion": "Remove debug print or use a proper logging framework (logging.getLogger).",
    },

    # ===== 异常处理 =====
    {
        "rule_id": "error-handling.bare-except",
        "pattern": r"except\s*:",
        "severity": Severity.MAJOR,
        "message": "Bare except clause catches all exceptions including SystemExit and KeyboardInterrupt.",
        "suggestion": "Catch specific exceptions: except ValueError: or except Exception: at minimum.",
    },
    {
        "rule_id": "error-handling.pass-in-except",
        "pattern": r"except\s+\w+.*:\s*pass",
        "severity": Severity.MINOR,
        "message": "Silent exception handling — errors are swallowed without logging.",
        "suggestion": "At minimum, log the exception. Consider whether it should be re-raised.",
    },
    {
        "rule_id": "error-handling.broad-except",
        "pattern": r"except\s+Exception\s*(?:as\s+\w+)?\s*:",
        "severity": Severity.MINOR,
        "message": "Catching Exception is too broad — may hide unexpected errors.",
        "suggestion": "Catch specific exception types where possible.",
    },

    # ===== 可维护性 =====
    {
        "rule_id": "maintainability.todo-comment",
        "pattern": r"#\s*(TODO|FIXME|HACK|XXX)\b",
        "severity": Severity.INFO,
        "message": "Unresolved TODO/FIXME comment.",
        "suggestion": "Track this in an issue tracker or resolve before merging.",
    },
    {
        "rule_id": "maintainability.mutable-default",
        "pattern": r"def\s+\w+\s*\([^)]*=\s*(\[\]|\{\}|\(\))",
        "severity": Severity.MAJOR,
        "message": "Mutable default argument — shared across all calls.",
        "suggestion": "Use None as default and initialize inside the function: if arg is None: arg = []",
    },
    {
        "rule_id": "maintainability.global-statement",
        "pattern": r"\bglobal\s+\w+",
        "severity": Severity.MINOR,
        "message": "Use of global statement makes code harder to test and reason about.",
        "suggestion": "Pass the variable as a parameter or use a class to manage state.",
    },

    # ===== 代码质量 =====
    {
        "rule_id": "quality.long-line",
        "pattern": r"^.{121,}$",
        "severity": Severity.INFO,
        "message": "Line exceeds 120 characters.",
        "suggestion": "Break long lines for readability. Configure your linter to enforce line length.",
    },
    {
        "rule_id": "quality.import-star",
        "pattern": r"from\s+\S+\s+import\s+\*",
        "severity": Severity.MINOR,
        "message": "Wildcard import pollutes namespace and makes dependencies unclear.",
        "suggestion": "Import specific names: from module import ClassA, function_b.",
    },
    {
        "rule_id": "quality.unused-import-common",
        "pattern": r"^\s*import\s+(typing|os\.path|json)\s*$",
        "severity": Severity.INFO,
        "message": "Potentially unused import (common false positive — verify).",
        "suggestion": "Check if this import is used. Remove if not needed.",
    },

    # ===== JavaScript / TypeScript 专用 =====
    {
        "rule_id": "security.js-innerhtml",
        "pattern": r"\.innerHTML\s*=",
        "severity": Severity.MAJOR,
        "message": "Setting innerHTML can lead to XSS if content is untrusted.",
        "suggestion": "Use textContent or sanitize HTML with DOMPurify before assignment.",
    },
    {
        "rule_id": "security.js-document-write",
        "pattern": r"document\.write\s*\(",
        "severity": Severity.MAJOR,
        "message": "document.write() is dangerous and blocks page rendering.",
        "suggestion": "Use DOM manipulation methods (createElement, appendChild) instead.",
    },
    {
        "rule_id": "quality.js-var-declaration",
        "pattern": r"\bvar\s+\w+",
        "severity": Severity.MINOR,
        "message": "var has function scope and can cause hoisting bugs.",
        "suggestion": "Use let (block scope, reassignable) or const (block scope, immutable).",
    },

    # ===== 类型安全 =====
    {
        "rule_id": "type-safety.any-annotation",
        "pattern": r":\s*Any\b",
        "severity": Severity.INFO,
        "message": "Any type annotation disables type checking.",
        "suggestion": "Use a more specific type: Union[str, int], Optional[Dict], etc.",
    },
]


def run_deterministic_checks(hunks: list[DiffHunk]) -> list[Finding]:
    """对 diff 中的新增行运行所有确定性规则。

    只检查新增行（以 '+' 开头），不检查上下文行和删除行。
    这样可以避免对正在被删除的代码报错。

    行号追踪：从 new_start 开始，对每行新增或上下文行递增
    （与 git diff 的行号编号一致）。

    Args:
        hunks: 从 parse_diff() 解析得到的 diff hunk 列表。

    Returns:
        Finding 对象列表，按严重度从高到低排列（blocker 在前）。
    """
    findings: list[Finding] = []

    for hunk in hunks:
        line_number = hunk.new_start
        for raw_line in hunk.lines:
            # 跳过文件头
            if raw_line.startswith(("+++", "---")):
                continue
            # 只检查新增行
            if not raw_line.startswith("+"):
                # 上下文行或删除行——上下文行仍需递增行号
                if raw_line.startswith(" ") and line_number is not None:
                    line_number += 1
                continue

            # 这是一个新增行
            content = raw_line[1:]  # 去掉 '+' 前缀

            # 跳过空行但仍需递增行号
            if not content.strip():
                if line_number is not None:
                    line_number += 1
                continue

            # 对该行运行所有规则
            for rule in DETERMINISTIC_RULES:
                # 如果规则有语言过滤则检查
                languages = rule.get("languages")
                if languages is not None and hunk.file:
                    ext = hunk.file.rsplit(".", 1)[-1] if "." in hunk.file else ""
                    if ext not in languages:
                        continue

                if re.search(rule["pattern"], content, re.IGNORECASE):
                    findings.append(
                        Finding(
                            rule_id=rule["rule_id"],
                            severity=rule["severity"],
                            file=hunk.file,
                            line=line_number,
                            message=rule["message"],
                            suggestion=rule["suggestion"],
                            confidence=Confidence.HIGH,
                            source="deterministic",
                        )
                    )

            if line_number is not None:
                line_number += 1

    # 按严重度排序（blocker 在前）
    severity_order = {Severity.BLOCKER: 0, Severity.MAJOR: 1, Severity.MINOR: 2, Severity.INFO: 3}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 99), f.file or "", f.line or 0))
    return findings