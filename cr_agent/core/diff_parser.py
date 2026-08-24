"""Unified diff 解析器——从原始 diff 文本中提取结构化的 hunk。

学习重点：
  - 用正则表达式解析 unified diff 格式
  - 行号追踪（old_start / new_start）
  - 边界情况处理（/dev/null、no newline、二进制文件）

Unified diff 格式说明：
  --- a/file.py          (旧文件头)
  +++ b/file.py          (新文件头)
  @@ -old_start,old_len +new_start,new_len @@  (hunk 头)
   context line          (前导空格 = 上下文行)
  -removed line          (前导 - = 删除行)
  +added line            (前导 + = 新增行)
  \\ No newline at end    (元数据，跳过)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@",
    re.MULTILINE,
)


@dataclass
class DiffHunk:
    """diff 中的一个 hunk（代码块变更单元）。"""

    file: str | None
    old_start: int | None
    new_start: int | None
    lines: list[str] = field(default_factory=list)

    @property
    def added_lines(self) -> list[str]:
        """新增的行（以 '+' 开头，但不是 '+++' 文件头）。"""
        return [l[1:] for l in self.lines if l.startswith("+") and not l.startswith("+++")]

    @property
    def removed_lines(self) -> list[str]:
        """删除的行（以 '-' 开头，但不是 '---' 文件头）。"""
        return [l[1:] for l in self.lines if l.startswith("-") and not l.startswith("---")]


def parse_diff(diff_text: str) -> list[DiffHunk]:
    """将 unified diff 字符串解析为结构化的 hunk 列表。

    Args:
        diff_text: 原始 unified diff 输出（如来自 `git diff` 或 `gh pr diff`）。

    Returns:
        DiffHunk 对象列表，每个包含文件路径、行号和原始行（保留 +/-/空格 前缀）。
    """
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    current_hunk: DiffHunk | None = None

    for raw_line in diff_text.split("\n"):
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            if current_file in ("/dev/null", ""):
                current_file = None
        elif raw_line.startswith("+++ /dev/null"):
            # 文件删除——没有新文件
            current_file = None
        elif raw_line.startswith("--- "):
            pass  # 旧文件头，我们通过 +++ 行追踪新文件即可
        elif raw_line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            match = HUNK_PATTERN.match(raw_line)
            current_hunk = DiffHunk(
                file=current_file,
                old_start=int(match.group("old_start")) if match else None,
                new_start=int(match.group("new_start")) if match else None,
            )
        elif current_hunk and (
            raw_line.startswith(("+", "-", " "))
        ):
            current_hunk.lines.append(raw_line)
        # 以 "\" 开头的行（no newline 标记）或其他内容会被跳过

    if current_hunk:
        hunks.append(current_hunk)

    return hunks


def compute_metrics(hunks: list[DiffHunk]) -> tuple[int, int, int]:
    """从 hunk 列表计算（变更文件数, 新增行数, 删除行数）。"""
    files: set[str | None] = set()
    added = 0
    removed = 0
    for hunk in hunks:
        files.add(hunk.file)
        for line in hunk.lines:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return len(files), added, removed
