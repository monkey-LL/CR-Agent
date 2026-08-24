"""审查记忆 —— 从过往审查中学习以改进未来审查。

无记忆场景（不愉快路径）：
  每次 PR 审查都从零开始。Agent 不知道：
  - 这个仓库的认证模块总是存在 SQL 注入问题
  - 团队的约定是使用 logging，而非 print
  - 上次对该 PR 的审查已经发现了 3 个问题

有记忆场景：
  在审查 PR #42 之前，加载历史记录：
  - "该仓库过往审查发现：SQL 注入 (3次)，硬编码密钥 (2次)"
  - "PR #42 的上次审查：3 个发现 (1 个阻塞级，2 个严重级)"
  这些上下文会注入到 LLM 提示词中，提升聚焦度和一致性。

存储方式：
  - 每个仓库一个 JSON 文件（默认，用于开发）：{repo}_{pr_number}.json
  - SQLite（生产环境）：结构化存储，支持快速查询
  - 向量数据库（未来）：用于相似代码模式的语义召回
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(os.environ.get("CR_MEMORY_DIR", ".cr_agent_memory"))
MEMORY_DB = Path(os.environ.get("CR_MEMORY_DB", str(MEMORY_DIR / "memory.db")))

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
    conn.commit()
    return conn


def save_review_memory(repo: str, pr_number: int, findings: list[dict], verdict: str, trace_id: str = ""):
    """将审查结果保存到记忆中，供后续参考。

    同时存储到 JSON（用于向后兼容）和 SQLite（用于快速查询）。
    """
    # 保存到 SQLite
    try:
        # 保持 finding_types 和 severities 的对应关系，不去重
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
            "Memory saved (SQLite): %s/%d (%d findings, verdict=%s)",
            repo, pr_number, len(findings), verdict,
        )
    except Exception as e:
        logger.warning("SQLite memory save failed, falling back to JSON: %s", e)
        _save_json_memory(repo, pr_number, findings, verdict)


def _save_json_memory(repo: str, pr_number: int, findings: list[dict], verdict: str):
    """备用方案：保存到 JSON 文件。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory_file = MEMORY_DIR / f"{repo.replace('/', '_')}_{pr_number}.json"

    record = {
        "repo": repo,
        "pr_number": pr_number,
        "verdict": verdict,
        "findings_count": len(findings),
        "finding_types": list({f.get("rule_id", "unknown") for f in findings}),
        "severities": list({f.get("severity", "info") for f in findings}),
        "reviewed_at": datetime.now().isoformat(),
    }

    history: list[dict] = []
    if memory_file.exists():
        try:
            data = json.loads(memory_file.read_text())
            history = data.get("history", [])
        except (json.JSONDecodeError, KeyError):
            pass

    history.append(record)
    memory_file.write_text(json.dumps({"history": history}, indent=2, ensure_ascii=False))
    logger.info("Memory saved (JSON): %s/%d", repo, pr_number)


def load_review_memory(repo: str, pr_number: int | None = None) -> dict | None:
    """加载某个仓库的审查记忆（可选指定具体 PR）。"""
    try:
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
            # 解析 JSON 字段
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
    except Exception as e:
        logger.warning("SQLite memory load failed, falling back to JSON: %s", e)
        return _load_json_memory(repo, pr_number)


def _load_json_memory(repo: str, pr_number: int | None = None) -> dict | None:
    """备用方案：从 JSON 文件加载。"""
    if pr_number is not None:
        memory_file = MEMORY_DIR / f"{repo.replace('/', '_')}_{pr_number}.json"
    else:
        memory_file = MEMORY_DIR / f"{repo.replace('/', '_')}_all.json"

    if not memory_file.exists():
        return None

    try:
        return json.loads(memory_file.read_text())
    except json.JSONDecodeError:
        return None


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
    try:
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

    except Exception as e:
        logger.warning("SQLite memory context failed, falling back to JSON: %s", e)
        return _build_json_memory_context(repo)


def _build_json_memory_context(repo: str) -> str:
    """备用方案：从 JSON 文件构建上下文。"""
    pattern_counts: dict[str, int] = {}
    total_reviews = 0

    for memory_file in MEMORY_DIR.glob(f"{repo.replace('/', '_')}*.json"):
        try:
            data = json.loads(memory_file.read_text())
            for record in data.get("history", []):
                total_reviews += 1
                for finding_type in record.get("finding_types", []):
                    pattern_counts[finding_type] = pattern_counts.get(finding_type, 0) + 1
        except (json.JSONDecodeError, KeyError):
            continue

    if not pattern_counts:
        return ""

    top_patterns = sorted(pattern_counts.items(), key=lambda x: -x[1])[:5]
    patterns_text = ", ".join(f"{name} ({count}x)" for name, count in top_patterns)

    return (
        f"\n## Repository Memory ({total_reviews} past reviews)\n"
        f"Common issues found: {patterns_text}\n"
        f"Pay extra attention to these patterns.\n"
    )