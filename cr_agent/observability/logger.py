"""可观测性：结构化日志、指标、重试逻辑。

学习重点：
  - structlog 用于结构化（JSON）日志 — 可搜索、可过滤
  - 带指数退避的重试 — 应对 LLM/API 的瞬时故障
  - Circuit breaker 模式 — 停止对故障服务的持续请求
  - 耗时指标 — 衡量审查流水线每个阶段的耗时

为什么需要结构化日志？
  纯文本日志难以搜索。结构化日志（JSON 键值对）
  可以按任意字段过滤：logger.filter(event="review_complete", repo="foo/bar")

为什么需要带退避的重试？
  LLM API 会限流（429）并且有瞬时故障（500）。简单的重试
  会持续请求服务器。指数退避（1s, 2s, 4s, ...）给服务器
  恢复时间。添加抖动（随机 0-1s）防止惊群效应。

为什么需要 circuit breaker？
  如果 LLM API 宕机了，每次请求都重试是在浪费时间和金钱。
  连续 N 次失败后，"打开断路器" — 快速失败 M 秒
  然后再尝试（半开探测）。
"""

from __future__ import annotations

import logging
import random
import threading
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
    """配置日志级别。"""
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
    """简单的 circuit breaker，用于防止级联故障。

    状态：
      closed（关闭）：请求正常通过
      open（打开）：请求立即失败（连续失败达到阈值后触发）
      half_open（半开）：允许一个探测请求通过（超过恢复超时时间后触发）
    """
    threshold: int = 5  # 打开断路器前的连续失败次数
    recovery_timeout: float = 60.0  # 半开探测前的等待秒数
    _failures: int = 0
    _last_failure_time: float = 0.0
    _state: str = "closed"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def call(self, func: Callable, *args, **kwargs):
        with self._lock:
            if self._state == "open":
                if time.time() - self._last_failure_time > self.recovery_timeout:
                    self._state = "half_open"
                    logger.info("circuit_breaker.half_open")
                else:
                    raise CircuitBreakerOpenError("Circuit breaker is open")

        # 在锁外执行实际调用，避免长时间持锁
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self):
        with self._lock:
            self._failures = 0
            if self._state != "closed":
                logger.info("circuit_breaker.closed")
            self._state = "closed"

    def _on_failure(self):
        with self._lock:
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
    """装饰器：使用指数退避 + 抖动重试函数。

    delay = min(base_delay * 2^attempt + random_jitter, max_delay)

    用法：
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
    """装饰器：记录审查阶段的执行时间。

    用法：
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
