"""Unit tests for observability: retry, circuit breaker, trace ID."""

import time

import pytest
import structlog

from cr_agent.observability.logger import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    retry_with_backoff,
)
from cr_agent.observability.tracing import get_trace_id, new_trace_id, set_trace_id


class TestCircuitBreaker:
    def test_closed_on_success(self):
        cb = CircuitBreaker(threshold=3)
        cb.call(lambda: "ok")
        assert cb._state == "closed"
        assert cb._failures == 0

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(threshold=3, recovery_timeout=0.1)

        def failing():
            raise RuntimeError("boom")

        for i in range(3):
            with pytest.raises(RuntimeError):
                cb.call(failing)

        assert cb._state == "open"

        # Now calls should fail fast
        with pytest.raises(CircuitBreakerOpenError):
            cb.call(lambda: "ok")

    def test_half_open_recovery(self):
        cb = CircuitBreaker(threshold=2, recovery_timeout=0.05)

        def failing():
            raise RuntimeError("boom")

        for i in range(2):
            with pytest.raises(RuntimeError):
                cb.call(failing)

        assert cb._state == "open"
        time.sleep(0.06)
        # Should allow one probe
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb._state == "closed"


class TestRetry:
    def test_succeeds_first_try(self):
        calls = []

        @retry_with_backoff(max_retries=3, base_delay=0.01)
        def func():
            calls.append(1)
            return "ok"

        assert func() == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        calls = []

        @retry_with_backoff(max_retries=3, base_delay=0.01)
        def func():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("not yet")
            return "ok"

        assert func() == "ok"
        assert len(calls) == 3

    def test_exhausts_retries(self):
        @retry_with_backoff(max_retries=2, base_delay=0.01)
        def func():
            raise RuntimeError("always fails")

        with pytest.raises(RuntimeError):
            func()


@pytest.fixture
def clean_trace():
    """隔离 trace-id 上下文状态,避免测试间泄漏。"""
    from cr_agent.observability import tracing

    structlog.contextvars.clear_contextvars()
    token = tracing._trace_id.set("")
    yield
    tracing._trace_id.reset(token)
    structlog.contextvars.clear_contextvars()


class TestTraceId:
    def test_new_trace_id_generates_and_binds(self, clean_trace):
        tid = new_trace_id()
        assert get_trace_id() == tid
        assert structlog.contextvars.get_contextvars().get("trace_id") == tid
        assert len(tid) == 12  # uuid4().hex[:12]

    def test_set_trace_id_binds_existing_without_generating(self, clean_trace):
        # set_trace_id 只绑定、不生成新 uuid
        set_trace_id("fixedtrace1")
        assert get_trace_id() == "fixedtrace1"
        assert structlog.contextvars.get_contextvars().get("trace_id") == "fixedtrace1"

    def test_webhook_pattern_preserves_trace_across_context(self, clean_trace):
        """回归 R1: 后台任务用 set_trace_id 重新绑定请求阶段生成的 trace。

        旧实现 new_trace_id(trace_id) 既会因多参 TypeError 崩溃,即便不崩也会
        生成全新 uuid、丢失链路关联。修复后须保证同一个 trace 贯穿请求→后台。
        """
        request_trace = new_trace_id()  # 请求阶段生成
        set_trace_id(request_trace)  # 后台上下文重新绑定
        assert get_trace_id() == request_trace  # 未被替换为新 uuid
        assert structlog.contextvars.get_contextvars().get("trace_id") == request_trace

    def test_set_trace_id_accepts_string_without_error(self, clean_trace):
        """R1 核心:传字符串参数不应抛 TypeError(旧调用 new_trace_id(trace_id) 的崩溃点)。"""
        set_trace_id("does-not-crash")  # 不应抛异常
        assert get_trace_id() == "does-not-crash"
