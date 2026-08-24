"""环境变量净化器 —— 防止 secret 泄露到 subprocess。

异常场景:
  Agent 进程的环境变量中包含 OPENAI_API_KEY 和 GH_TOKEN。
  LLM 说 "run bash: env" → subprocess 继承所有环境变量 →
  API key 打印在工具输出中 → 传给 LLM → 可能最终出现在 PR 评论中。

解决方案:
  为 subprocess 构建一个净化后的环境变量字典:
  1. 仅传递白名单变量（PATH、HOME、LANG 等）
  2. 将匹配 *KEY*/*SECRET*/*TOKEN* 模式的变量替换为 [REDACTED]
  3. 仅在需要时显式传递 GH_TOKEN（默认不传递）
"""

from __future__ import annotations

import os
import re

# 可以安全传递给 subprocess 的变量
SAFE_ENV_VARS = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "TMPDIR", "TMP", "TEMP",
    "SYSTEMROOT", "COMSPEC",  # Windows 系统
}

# 表示 secret 变量的匹配模式
SECRET_PATTERNS = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE|API)",
    re.IGNORECASE,
)


def build_safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """为 subprocess 执行构建净化后的环境变量字典。

    仅传递安全变量。任何匹配 SECRET_PATTERNS 的变量
    会被替换为 [REDACTED]。

    Args:
        extra: 需要显式注入的额外变量（例如 {"GH_TOKEN": "xxx"}）。
               这些变量按原样添加，由调用者决定暴露哪些内容。
    """
    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in SAFE_ENV_VARS:
            safe[key] = value
        elif SECRET_PATTERNS.search(key):
            safe[key] = "[REDACTED]"
        # 其他变量不传递

    if extra:
        safe.update(extra)

    return safe
