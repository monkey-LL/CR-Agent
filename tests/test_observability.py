"""Unit tests for observability: retry, circuit breaker."""

import pytest
import time
from cr_agent.observability.logger import CircuitBreaker, CircuitBreakerOpenError, retry_with_backoff


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
