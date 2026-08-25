"""审查记忆 —— 从过往审查中学习以改进未来审查。

学习重点：
  - SQLite 作为轻量持久化：零配置、单文件、够快
  - 记忆上下文注入 LLM prompt：让 LLM 知道仓库的历史问题模式
  - 确认偏误防护：只注入 blocker+major 级别的历史 pattern，避免 minor/info 干扰

数据流：
  save_review_memory → SQLite reviews 表
  build_memory_context → 查询 SQLite → 拼接 prompt 上下文
  cleanup_old_reviews → 定期清理过期记录（由调用方触发）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(os.environ.get("CR_MEMORY_DIR", ".cr_agent_memory"))
MEMORY_DB = Path(os.environ.get("CR_MEMORY_DB", str(MEMORY_DIR / "memory.db")))

# 默认保留天数，超过此天数的审查记录会被 cleanup_old_reviews 清理
DEFAULT_RETENTION_DAYS = 90

_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """获取或创建 SQLite 记忆数据库。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            findings_count INTEGER DEFAULT 0,
            finding_types TEXT,  -- JSON 数组
            severities TEXT,     -- JSON 数组
            reviewed_at TEXT NOT NULL,
            trace_id TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reviews_repo ON reviews(repo)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reviews_repo_pr ON reviews(repo, pr_number)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reviews_reviewed_at ON reviews(reviewed_at)
    """)
    conn.commit()
    return conn


def save_review_memory(repo: str, pr_number: int, findings: list[dict], verdict: str, trace_id: str = ""):
    """将审查结果保存到 SQLite，供后续参考。

    finding_types 和 severities 保持对应关系，不去重。
    """
    finding_types = [f.get("rule_id", "unknown") for f in findings]
    severities = [f.get("severity", "info") for f in findings]

    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                """INSERT INTO reviews (repo, pr_number, verdict, findings_count,
                   finding_types, severities, reviewed_at, trace_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    repo, pr_number, verdict, len(findings),
                    json.dumps(finding_types), json.dumps(severities),
                    datetime.now().isoformat(), trace_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    logger.info(
        "Memory saved: %s/%d (%d findings, verdict=%s)",
        repo, pr_number, len(findings), verdict,
    )


def load_review_memory(repo: str, pr_number: int | None = None) -> dict | None:
    """加载某个仓库的审查记忆（可选指定具体 PR）。"""
    with _db_lock:
        conn = _get_db()
        try:
            if pr_number is not None:
                cursor = conn.execute(
                    "SELECT * FROM reviews WHERE repo = ? AND pr_number = ? ORDER BY reviewed_at DESC LIMIT 1",
                    (repo, pr_number),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM reviews WHERE repo = ? ORDER BY reviewed_at DESC LIMIT 10",
                    (repo,),
                )
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        columns = [desc[0] for desc in cursor.description]
        records = [dict(zip(columns, row)) for row in rows]
        for r in records:
            if r.get("finding_types"):
                try:
                    r["finding_types"] = json.loads(r["finding_types"])
                except json.JSONDecodeError:
                    r["finding_types"] = []
            if r.get("severities"):
                try:
                    r["severities"] = json.loads(r["severities"])
                except json.JSONDecodeError:
                    r["severities"] = []

        return {"history": records} if pr_number is None else records[0]


def build_memory_context(repo: str) -> str:
    """从过往审查中构建上下文字符串，用于 LLM 提示词。

    查询 SQLite 获取该仓库的所有过往审查，并总结：
    - 总审查次数和最常见的发现模式（前 5 名）
    - 最近一次的裁定和发现数量
    - 反复出现的严重级别分布

    示例输出：
      "## Repository Memory (12 past reviews)
       Common issues found: security.sql-injection (5x), debug.print-statement (3x), ...
       Last review: 3 findings, verdict=request_changes.
       Pay extra attention to these patterns."
    """
    with _db_lock:
        conn = _get_db()
        try:
            cursor = conn.execute(
                "SELECT finding_types, severities, verdict, findings_count, reviewed_at "
                "FROM reviews WHERE repo = ? ORDER BY reviewed_at DESC",
                (repo,),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            return ""

        pattern_counts: Counter = Counter()
        severity_counts: Counter = Counter()
        total_reviews = len(rows)
        last_review = rows[0]

        for row in rows:
            finding_types = json.loads(row[0]) if row[0] else []
            severities = json.loads(row[1]) if row[1] else []
            # 只统计 blocker 和 major 级别的 pattern，避免误报强化确认偏误
            for ft, sev in zip(finding_types, severities):
                if sev in ("blocker", "major"):
                    pattern_counts[ft] += 1
            severity_counts.update(severities)

        # 最常见的前 5 种高危发现类型（仅 blocker + major）
        top_patterns = pattern_counts.most_common(5)
        patterns_text = ", ".join(f"{name} ({count}x)" for name, count in top_patterns)

        # 严重级别分布
        severity_text = ", ".join(f"{sev}: {count}" for sev, count in severity_counts.most_common())

        # 最近一次审查信息
        last_verdict = last_review[2] or "unknown"
        last_findings = last_review[3] or 0
        last_date = (last_review[4] or "")[:10]

        parts = [
            f"\n## Repository Memory ({total_reviews} past reviews)",
            f"High-severity patterns: {patterns_text}" if patterns_text else "",
            f"Severity breakdown: {severity_text}" if severity_text else "",
            f"Last review ({last_date}): {last_findings} findings, verdict={last_verdict}.",
        ]
        return "\n".join(p for p in parts if p) + "\n"


def cleanup_old_reviews(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """清理超过保留天数的审查记录。

    Args:
        retention_days: 保留最近多少天的记录，默认 90 天。

    Returns:
        被删除的记录数。
    """
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    with _db_lock:
        conn = _get_db()
        try:
            cursor = conn.execute(
                "DELETE FROM reviews WHERE reviewed_at < ?", (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

    if deleted > 0:
        logger.info("Memory cleanup: removed %d reviews older than %d days", deleted, retention_days)
    return deleted
