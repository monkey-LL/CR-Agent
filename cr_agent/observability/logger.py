"""Observability: structured logging, metrics, retry logic.

Learning focus:
  - structlog for structured (JSON) logs — searchable, filterable
  - Retry with exponential backoff — for transient LLM/API failures
  - Circuit breaker pattern — stop hammering a failing service
  - Timing metrics — measure each phase of the review pipeline

Why structured logging?
  Plain text logs are hard to search. Structured logs (JSON key-value pairs)
  can be filtered by any field: logger.filter(event="review_complete", repo="foo/bar")

Why retry with backoff?
  LLM APIs rate-limit (429) and have transient failures (500). Naive retry
  hammers the server. Exponential backoff (1s, 2s, 4s, ...) gives the server
  time to recover. Adding jitter (random 0-1s) prevents thundering herd.

Why circuit breaker?
  If the LLM API is down, retrying every request wastes time and money.
  After N consecutive failures, "open the circuit" — fail fast for M seconds
  before trying again (half-open probe).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps

import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

logger = structlog.get_logger()


def setup_logging(level: str = "INFO"):
    """Configure logging level."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
    )


@dataclass
class CircuitBreaker:
    """Simple circuit breaker for protecting against cascading failures.

    States:
      closed: requests flow normally
      open: requests fail immediately (after threshold consecutive failures)
      half_open: one probe request allowed (after recovery_timeout)
    """
    threshold: int = 5  # consecutive failures before opening
    recovery_timeout: float = 60.0  # seconds before half-open probe
    _failures: int = 0
    _last_failure_time: float = 0.0
    _state: str = "closed"

    def call(self, func: Callable, *args, **kwargs):
        if self._state == "open":
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = "half_open"
                logger.info("circuit_breaker.half_open")
            else:
                raise CircuitBreakerOpenError("Circuit breaker is open")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        self._failures = 0
        if self._state != "closed":
            logger.info("circuit_breaker.closed")
        self._state = "closed"

    def _on_failure(self):
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.threshold:
            self._state = "open"
            logger.warning("circuit_breaker.opened", failures=self._failures)


class CircuitBreakerOpenError(Exception):
    pass


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
):
    """Decorator: retry a function with exponential backoff + jitter.

    delay = min(base_delay * 2^attempt + random_jitter, max_delay)

    Usage:
      @retry_with_backoff(max_retries=3, base_delay=2.0)
      def call_llm(prompt): ...
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                        logger.warning(
                            "retry_scheduled",
                            func=func.__name__,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay=round(delay, 2),
                            error=str(e),
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "retry_exhausted",
                            func=func.__name__,
                            attempts=max_retries + 1,
                            error=str(e),
                        )
            raise last_exc
        return wrapper
    return decorator


def time_phase(phase_name: str):
    """Decorator: log execution time of a review phase.

    Usage:
      @time_phase("deterministic_checks")
      def run_checks(diff): ...
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            result = func(*args, **kwargs)
            elapsed = time.time() - start
            logger.info("phase_complete", phase=phase_name, elapsed_ms=round(elapsed * 1000))
            return result
        return wrapper
    return decorator
