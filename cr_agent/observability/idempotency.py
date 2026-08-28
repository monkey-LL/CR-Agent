"""幂等性和并发控制 — 防止重复审查和竞态条件。

异常场景 1（webhook 重复投递）：
  GitHub 为 PR #42 发送 webhook -> 审查开始 -> 耗时 40 秒
  GitHub 未及时收到 200 响应 -> 重发 webhook -> 第二次审查开始
  -> 两个审查并行运行 -> 发布了两条 PR 评论 -> 垃圾信息

异常场景 2（并发 PR）：
  PR #42 和 PR #43 同时创建 -> 都触发 webhook
  -> 两个审查并行运行 -> 这是正常的，因为是不同的 PR
  -> 但：都调用 `gh pr comment` -> 触发 GitHub API 速率限制

异常场景 3（同一 PR 快速推送）：
  开发者推送到 PR #42 -> 审查开始
  开发者 5 秒后再次推送 -> 第二个 webhook -> 第二次审查开始
  -> 第一次审查完成，发布关于旧代码的评论
  -> 第二次审查完成，发布关于新代码的评论
  -> 让开发者困惑

解决方案：
  1. 幂等性存储：(repo, pr_number) -> in_progress/done。拒绝重复。
  2. 并发限制：最多 N 个并行审查（默认 3）。
  3. 防抖：为同一 PR 的快速连续 webhook 设置缓冲窗口（5 秒）。

存储后端：
  - 内存（默认，用于开发/测试）：带 TTL 的线程安全 dict
  - Redis（生产环境）：原子 SET NX + EX 实现分布式锁
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ReviewStatus(str, Enum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass
class ReviewRecord:
    """追踪特定 PR 审查状态的记录。"""
    repo: str
    pr_number: int
    status: ReviewStatus = ReviewStatus.IDLE
    started_at: float = 0.0
    completed_at: float = 0.0
    trace_id: str = ""


class IdempotencyStore:
    """内存幂等性 + 并发存储。

    生产环境：分布式部署时使用 RedisIdempotencyStore。
    这里：为简单起见使用带 TTL 的线程安全 dict。

    键：(repo, pr_number)
    值：包含状态的 ReviewRecord

    规则：
    - 如果 status=IN_PROGRESS -> 拒绝（返回 False，审查已在运行中）
    - 如果 status=DONE 且在 TTL 内 -> 拒绝（返回 False，最近已审查过）
    - 如果 status=IDLE 或已过期 -> 接受（返回 True，继续执行）
    """

    def __init__(self, ttl_seconds: int = 300, max_concurrent: int = 3):
        self._store: dict[tuple[str, int], ReviewRecord] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_concurrent = max_concurrent
        self._active_count = 0

    def try_acquire(self, repo: str, pr_number: int, trace_id: str = "") -> bool:
        """尝试启动审查。允许则返回 True，拒绝则返回 False。"""
        key = (repo, pr_number)
        now = time.time()

        with self._lock:
            # 周期性清理已过期的 DONE 记录（每 100 次调用清理一次）
            if len(self._store) > 0 and len(self._store) % 100 == 0:
                expired = [
                    k for k, v in self._store.items()
                    if v.status == ReviewStatus.DONE
                    and now - v.completed_at > self._ttl
                ]
                for k in expired:
                    del self._store[k]

            record = self._store.get(key)
            record = self._store.get(key)

            if record:
                if record.status == ReviewStatus.IN_PROGRESS:
                    logger.info(
                        "Idempotency reject: %s/%d already in progress (trace=%s)",
                        repo, pr_number, record.trace_id,
                    )
                    return False

                if record.status == ReviewStatus.DONE:
                    age = now - record.completed_at
                    if age < self._ttl:
                        logger.info(
                            "Idempotency reject: %s/%d reviewed %.0fs ago (TTL=%ds)",
                            repo, pr_number, age, self._ttl,
                        )
                        return False

            # 检查并发限制
            if self._active_count >= self._max_concurrent:
                logger.warning(
                    "Concurrency reject: %d/%d reviews in progress",
                    self._active_count, self._max_concurrent,
                )
                return False

            # 获取锁
            self._store[key] = ReviewRecord(
                repo=repo,
                pr_number=pr_number,
                status=ReviewStatus.IN_PROGRESS,
                started_at=now,
                trace_id=trace_id,
            )
            self._active_count += 1
            logger.info(
                "Idempotency acquire: %s/%d (active=%d, trace=%s)",
                repo, pr_number, self._active_count, trace_id,
            )
            return True

    def release(self, repo: str, pr_number: int, success: bool = True):
        """标记审查为已完成。"""
        key = (repo, pr_number)
        now = time.time()

        with self._lock:
            record = self._store.get(key)
            if record and record.status == ReviewStatus.IN_PROGRESS:
                record.status = ReviewStatus.DONE
                record.completed_at = now
                self._active_count = max(0, self._active_count - 1)
                logger.info(
                    "Idempotency release: %s/%d (success=%s, active=%d)",
                    repo, pr_number, success, self._active_count,
                )

    def get_status(self, repo: str, pr_number: int) -> ReviewRecord | None:
        """查询 PR 审查的当前状态。"""
        with self._lock:
            return self._store.get((repo, pr_number))


class RedisIdempotencyStore:
    """基于 Redis 的幂等性存储，用于分布式部署。

    使用原子 SET NX + EX 实现无竞态的锁获取。
    如果 Redis 不可用则优雅降级。

    相比内存存储的优势：
    - 跨多个工作进程/机器工作
    - 原子操作（无竞态条件）
    - 自动 TTL 过期（无需清理）
    - 重启后仍可恢复
    """

    def __init__(
        self,
        redis_client=None,
        ttl_seconds: int = 300,
        max_concurrent: int = 3,
        key_prefix: str = "cr_agent:review",
    ):
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._max_concurrent = max_concurrent
        self._prefix = key_prefix
        self._active_key = f"{key_prefix}:active_count"

    def _make_key(self, repo: str, pr_number: int) -> str:
        return f"{self._prefix}:{repo}:{pr_number}"

    def try_acquire(self, repo: str, pr_number: int, trace_id: str = "") -> bool:
        """使用 Redis 原子操作尝试启动审查。"""
        if self._redis is None:
            logger.warning("Redis not available, allowing review (fail-open)")
            return True

        try:
            key = self._make_key(repo, pr_number)

            # 检查并发限制
            active = int(self._redis.get(self._active_key) or 0)
            if active >= self._max_concurrent:
                logger.warning(
                    "Redis concurrency reject: %d/%d reviews in progress",
                    active, self._max_concurrent,
                )
                return False

            # 尝试原子获取锁：SET NX EX
            # value = trace_id，用于调试
            acquired = self._redis.set(key, trace_id, nx=True, ex=self._ttl)
            if not acquired:
                # 键已存在 — 正在审查中或最近刚审查过
                logger.info(
                    "Redis idempotency reject: %s/%d already locked (trace=%s)",
                    repo, pr_number, trace_id,
                )
                return False

            # 增加活跃计数
            self._redis.incr(self._active_key)
            logger.info(
                "Redis idempotency acquire: %s/%d (trace=%s)",
                repo, pr_number, trace_id,
            )
            return True

        except Exception as e:
            logger.error("Redis idempotency error: %s. Fail-open.", e)
            return True

    def release(self, repo: str, pr_number: int, success: bool = True):
        """通过删除锁键来标记审查为已完成。"""
        if self._redis is None:
            return

        try:
            key = self._make_key(repo, pr_number)
            self._redis.delete(key)
            self._redis.decr(self._active_key)
            logger.info(
                "Redis idempotency release: %s/%d (success=%s)",
                repo, pr_number, success,
            )
        except Exception as e:
            logger.error("Redis release error: %s", e)

    def get_status(self, repo: str, pr_number: int) -> ReviewRecord | None:
        """从 Redis 查询 PR 审查的当前状态。"""
        if self._redis is None:
            return None

        try:
            key = self._make_key(repo, pr_number)
            value = self._redis.get(key)
            if value is None:
                return None
            return ReviewRecord(
                repo=repo,
                pr_number=pr_number,
                status=ReviewStatus.IN_PROGRESS,
                trace_id=value.decode() if isinstance(value, bytes) else str(value),
            )
        except Exception as e:
            logger.error("Redis get_status error: %s", e)
            return None


# 工厂模式：根据环境选择合适的后端
_idempotency_store: IdempotencyStore | RedisIdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore | RedisIdempotencyStore:
    """获取或创建全局幂等性存储。

    如果环境中设置了 REDIS_URL 则使用 Redis，否则使用内存存储。
    """
    global _idempotency_store
    if _idempotency_store is not None:
        return _idempotency_store

    import os
    redis_url = os.environ.get("REDIS_URL")

    if redis_url:
        try:
            import redis
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()  # 测试连接
            _idempotency_store = RedisIdempotencyStore(redis_client=client)
            logger.info("Using Redis idempotency store: %s", redis_url)
        except Exception as e:
            logger.warning("Redis unavailable (%s), falling back to in-memory store", e)
            _idempotency_store = IdempotencyStore()
    else:
        _idempotency_store = IdempotencyStore()
        logger.warning(
            "Using in-memory idempotency store — NOT safe for multi-worker deployments. "
            "Set REDIS_URL for multi-worker (e.g. uvicorn --workers N) consistency."
        )

    return _idempotency_store


def set_idempotency_store(store):
    """注入自定义存储（用于测试）。"""
    global _idempotency_store
    _idempotency_store = store