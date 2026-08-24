"""Tests for Redis idempotency store and memory system."""


import pytest

from cr_agent.observability.idempotency import (
    RedisIdempotencyStore,
)


class TestRedisIdempotencyStore:
    """Test Redis-backed idempotency using fakeredis."""

    @pytest.fixture
    def redis_store(self):
        try:
            import fakeredis
            client = fakeredis.FakeRedis(decode_responses=True)
            return RedisIdempotencyStore(redis_client=client, ttl_seconds=5, max_concurrent=3)
        except ImportError:
            pytest.skip("fakeredis not installed")

    def test_acquire_and_release(self, redis_store):
        assert redis_store.try_acquire("owner/repo", 42, "trace1") is True
        redis_store.release("owner/repo", 42)

    def test_rejects_duplicate(self, redis_store):
        assert redis_store.try_acquire("owner/repo", 42, "trace1") is True
        assert redis_store.try_acquire("owner/repo", 42, "trace2") is False
        redis_store.release("owner/repo", 42)

    def test_different_prs_allowed(self, redis_store):
        assert redis_store.try_acquire("owner/repo", 42, "trace1") is True
        assert redis_store.try_acquire("owner/repo", 43, "trace2") is True
        redis_store.release("owner/repo", 42)
        redis_store.release("owner/repo", 43)

    def test_concurrency_limit(self, redis_store):
        # Acquire 3 (max_concurrent=3)
        for i in range(3):
            assert redis_store.try_acquire("owner/repo", 40 + i, f"trace{i}") is True
        # 4th should be rejected
        assert redis_store.try_acquire("owner/repo", 50, "trace3") is False
        for i in range(3):
            redis_store.release("owner/repo", 40 + i)

    def test_get_status(self, redis_store):
        redis_store.try_acquire("owner/repo", 42, "trace1")
        status = redis_store.get_status("owner/repo", 42)
        assert status is not None
        assert "trace1" in status.trace_id
        redis_store.release("owner/repo", 42)

    def test_fail_open_without_redis(self):
        store = RedisIdempotencyStore(redis_client=None)
        assert store.try_acquire("owner/repo", 42) is True


class TestMemorySystem:
    """Test the SQLite-backed memory system."""

    @pytest.fixture
    def temp_memory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CR_MEMORY_DIR", str(tmp_path / "memory"))
        monkeypatch.setenv("CR_MEMORY_DB", str(tmp_path / "memory" / "memory.db"))
        # Re-import to pick up new env vars
        import importlib

        import cr_agent.agent.memory as mem
        importlib.reload(mem)
        return mem

    def test_save_and_load(self, temp_memory, tmp_path):
        findings = [
            {"rule_id": "security.sql-injection", "severity": "blocker"},
            {"rule_id": "debug.print-statement", "severity": "minor"},
        ]
        temp_memory.save_review_memory("owner/repo", 42, findings, "block")
        record = temp_memory.load_review_memory("owner/repo", 42)
        assert record is not None
        assert record["verdict"] == "block"
        assert record["findings_count"] == 2

    def test_load_nonexistent(self, temp_memory, tmp_path):
        result = temp_memory.load_review_memory("nonexistent/repo", 99)
        assert result is None

    def test_build_memory_context_empty(self, temp_memory, tmp_path):
        ctx = temp_memory.build_memory_context("empty/repo")
        assert ctx == ""

    def test_build_memory_context_with_history(self, temp_memory, tmp_path):
        findings1 = [{"rule_id": "security.sql-injection", "severity": "blocker"}]
        findings2 = [{"rule_id": "security.sql-injection", "severity": "blocker"},
                      {"rule_id": "debug.print-statement", "severity": "minor"}]
        findings3 = [{"rule_id": "maintainability.todo-comment", "severity": "info"}]

        temp_memory.save_review_memory("owner/repo", 1, findings1, "block")
        temp_memory.save_review_memory("owner/repo", 2, findings2, "block")
        temp_memory.save_review_memory("owner/repo", 3, findings3, "approve")

        ctx = temp_memory.build_memory_context("owner/repo")
        assert "Repository Memory" in ctx
        assert "3 past reviews" in ctx
        assert "security.sql-injection" in ctx
        # D7: 记忆系统只统计 blocker+major，minor 级不注入 prompt 避免确认偏误
        assert "debug.print-statement" not in ctx