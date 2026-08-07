"""Idempotency and concurrency control — prevent duplicate reviews and races.

Unhappy path 1 (webhook redelivery):
  GitHub sends webhook for PR #42 → review starts → takes 40s
  GitHub doesn't get 200 in time → resends webhook → second review starts
  → two reviews running in parallel → two PR comments posted → spam

Unhappy path 2 (concurrent PRs):
  PR #42 and PR #43 created at the same time → both trigger webhook
  → both reviews run in parallel → this is fine, different PRs
  → but: both call `gh pr comment` → GitHub API rate limit hit

Unhappy path 3 (same PR, rapid pushes):
  Developer pushes to PR #42 → review starts
  Developer pushes again 5s later → second webhook → second review starts
  → first review finishes, posts comment about old code
  → second review finishes, posts comment about new code
  → confusing for developer

Solutions:
  1. Idempotency store: (repo, pr_number) → in_progress/done. Reject duplicates.
  2. Concurrency limit: max N parallel reviews (default 3).
  3. Debounce: buffer rapid successive webhooks for the same PR (5s window).
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
    """Track the state of a review for a specific PR."""
    repo: str
    pr_number: int
    status: ReviewStatus = ReviewStatus.IDLE
    started_at: float = 0.0
    completed_at: float = 0.0
    trace_id: str = ""


class IdempotencyStore:
    """In-memory idempotency + concurrency store.

    Production: use Redis or a database with TTL + atomic operations.
    Here: thread-safe dict with TTL for simplicity.

    Key: (repo, pr_number)
    Value: ReviewRecord with status

    Rules:
    - If status=IN_PROGRESS → reject (return False, review already running)
    - If status=DONE and within TTL → reject (return False, already reviewed recently)
    - If status=IDLE or expired → accept (return True, proceed)
    """

    def __init__(self, ttl_seconds: int = 300, max_concurrent: int = 3):
        self._store: dict[tuple[str, int], ReviewRecord] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_concurrent = max_concurrent
        self._active_count = 0

    def try_acquire(self, repo: str, pr_number: int, trace_id: str = "") -> bool:
        """Try to start a review. Returns True if allowed, False if rejected.

        Rejection reasons:
        - Same PR already IN_PROGRESS (duplicate webhook)
        - Same PR DONE within TTL (recent review, skip)
        - Too many concurrent reviews (overload protection)
        """
        key = (repo, pr_number)
        now = time.time()

        with self._lock:
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

            # Check concurrency limit
            if self._active_count >= self._max_concurrent:
                logger.warning(
                    "Concurrency reject: %d/%d reviews in progress",
                    self._active_count, self._max_concurrent,
                )
                return False

            # Acquire
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
        """Mark a review as completed."""
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
        """Query the current status of a PR review."""
        with self._lock:
            return self._store.get((repo, pr_number))


# Global singleton (production: inject via dependency injection)
_idempotency_store: IdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore:
    global _idempotency_store
    if _idempotency_store is None:
        _idempotency_store = IdempotencyStore()
    return _idempotency_store
