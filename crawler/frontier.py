from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from crawler.storage import Repository

_CLAIM_BATCH = 200
_MAX_BACKOFF = 15 * 60  # 15 minutes
_CONSECUTIVE_FAIL_BLOCK = 25
_MAX_ATTEMPTS = 3


@dataclass
class FrontierTask:
    url: str
    domain: str
    seed_id: str
    depth: int
    parent_url: str | None
    anchor_text: str
    scope_rule: str
    parent_topical: bool
    attempts: int = 0


class DomainScheduler:
    """Mercator-style polite frontier: one in-flight request per domain."""

    def __init__(
        self,
        repository: Repository,
        max_pages_per_domain: int,
        min_interval: float,
        logger: logging.Logger,
        stop_event: threading.Event,
    ) -> None:
        self._repo = repository
        self._max_pages = max_pages_per_domain
        self._min_interval = min_interval
        self._logger = logger
        self._stop_event = stop_event

        self._cond = threading.Condition()
        self._buffers: dict[str, deque[FrontierTask]] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._heap_domains: set[str] = set()
        self._in_flight: set[str] = set()
        self._blocked: set[str] = set()
        self._consec_fails: dict[str, int] = {}
        self._domain_counts: dict[str, int] = {}
        self._seq = 0

    def start(self) -> None:
        reset = self._repo.reset_in_progress()
        if reset:
            self._logger.info("FRONTIER_RESET_IN_PROGRESS count=%d", reset)
        self._domain_counts = dict(self._repo.frontier_domain_counts())
        with self._cond:
            for domain in self._repo.pending_domains():
                if domain not in self._blocked:
                    self._offer_domain(domain, ready_at=0.0)
            self._cond.notify_all()

    def _offer_domain(self, domain: str, ready_at: float) -> None:
        """Add domain to heap if not already scheduled / in flight / blocked."""
        if domain in self._blocked:
            return
        if domain in self._in_flight:
            return
        if domain in self._heap_domains:
            return
        self._seq += 1
        heapq.heappush(self._heap, (ready_at, self._seq, domain))
        self._heap_domains.add(domain)

    def add_urls(self, items: list[dict]) -> list[str]:
        """Persist new frontier URLs under per-domain caps. Returns inserted."""
        if not items:
            return []

        accepted: list[dict] = []
        with self._cond:
            for item in items:
                domain = item["domain"]
                if domain in self._blocked:
                    continue
                count = self._domain_counts.get(domain, 0)
                if count >= self._max_pages:
                    continue
                accepted.append(item)
                self._domain_counts[domain] = count + 1

        if not accepted:
            return []

        inserted = self._repo.enqueue_many(accepted)
        inserted_set = set(inserted)

        with self._cond:
            for item in accepted:
                if item["url"] not in inserted_set:
                    domain = item["domain"]
                    self._domain_counts[domain] = max(
                        0, self._domain_counts.get(domain, 1) - 1
                    )
            domains_woken = {
                item["domain"] for item in accepted if item["url"] in inserted_set
            }
            for domain in domains_woken:
                self._offer_domain(domain, ready_at=0.0)
            if domains_woken:
                self._cond.notify_all()

        return inserted

    def next_task(self, stop_event: threading.Event | None = None) -> FrontierTask | None:
        stop = stop_event or self._stop_event
        while True:
            if stop.is_set():
                return None

            domain: str | None = None
            with self._cond:
                while domain is None:
                    if stop.is_set():
                        return None

                    now = time.monotonic()
                    domain = self._pop_ready_domain(now)
                    if domain is not None:
                        self._in_flight.add(domain)
                        break

                    if self._idle_unlocked():
                        # Confirm no DB pending outside lock below.
                        break

                    if not self._heap:
                        self._cond.wait(timeout=0.5)
                    else:
                        wait = max(0.01, self._heap[0][0] - now)
                        self._cond.wait(timeout=min(wait, 0.5))

            if domain is None:
                # Possibly finished: double-check DB without holding cond.
                if stop.is_set():
                    return None
                if self.is_finished():
                    return None
                # Spurious: new work may appear; brief yield.
                time.sleep(0.05)
                continue

            task = self._take_task(domain)
            if task is None:
                has_pending = self._repo.has_pending_domain(domain)
                with self._cond:
                    self._in_flight.discard(domain)
                    if has_pending and domain not in self._blocked:
                        self._offer_domain(domain, ready_at=0.0)
                    self._cond.notify_all()
                continue
            return task

    def _pop_ready_domain(self, now: float) -> str | None:
        while self._heap and self._heap[0][0] <= now:
            _ready, _seq, domain = heapq.heappop(self._heap)
            self._heap_domains.discard(domain)
            if domain in self._blocked or domain in self._in_flight:
                continue
            return domain
        return None

    def _idle_unlocked(self) -> bool:
        if self._in_flight or self._heap:
            return False
        for buf in self._buffers.values():
            if buf:
                return False
        return True

    def _take_task(self, domain: str) -> FrontierTask | None:
        """Pop from memory buffer or claim a batch from SQLite (no cond held)."""
        with self._cond:
            buf = self._buffers.get(domain)
            if buf is None:
                buf = deque()
                self._buffers[domain] = buf
            if buf:
                return buf.popleft()

        rows = self._repo.claim_batch(domain, _CLAIM_BATCH)
        if not rows:
            return None

        tasks = [
            FrontierTask(
                url=row["url"],
                domain=row["domain"],
                seed_id=row["seed_id"] or "",
                depth=int(row["depth"] or 0),
                parent_url=row["parent_url"],
                anchor_text=row["anchor_text"] or "",
                scope_rule=row["scope_rule"] or "",
                parent_topical=bool(row["parent_topical"]),
                attempts=int(row["attempts"] or 0),
            )
            for row in rows
        ]
        first, rest = tasks[0], tasks[1:]
        if rest:
            with self._cond:
                buf = self._buffers.setdefault(domain, deque())
                buf.extend(rest)
        return first

    def task_done(
        self,
        task: FrontierTask,
        delay_seconds: float,
        *,
        status: str = "done",
        reason: str | None = None,
        success: bool = True,
        retry: bool = False,
        retry_after: float | None = None,
    ) -> None:
        domain = task.domain

        if retry:
            attempts = self._repo.requeue_frontier(task.url, reason=reason)
            if attempts >= _MAX_ATTEMPTS:
                self._repo.mark_frontier(task.url, "error", reason or "max_attempts")
                retry = False
                status = "error"
                reason = reason or "max_attempts"
            else:
                backoff = retry_after
                if backoff is None:
                    backoff = min(_MAX_BACKOFF, 30.0 * (2 ** max(0, attempts - 1)))
                delay_seconds = max(delay_seconds, backoff)

        if not retry:
            if status in ("done", "skipped", "error"):
                self._repo.mark_frontier(task.url, status, reason)

        if success:
            self._consec_fails[domain] = 0
        else:
            fails = self._consec_fails.get(domain, 0) + 1
            self._consec_fails[domain] = fails
            if fails >= _CONSECUTIVE_FAIL_BLOCK:
                with self._cond:
                    self._blocked.add(domain)
                self._logger.info(
                    "DOMAIN_BLOCKED domain=%s consecutive_fails=%d",
                    domain,
                    fails,
                )
                self._drain_domain(domain)

        delay = max(self._min_interval, delay_seconds)
        ready_at = time.monotonic() + delay

        with self._cond:
            self._in_flight.discard(domain)
            buf = self._buffers.get(domain)
            has_buf = bool(buf)
            blocked = domain in self._blocked

        has_pending = (not blocked) and (has_buf or self._repo.has_pending_domain(domain))

        with self._cond:
            if has_pending and domain not in self._blocked:
                self._offer_domain(domain, ready_at=ready_at)
            self._cond.notify_all()

    def _drain_domain(self, domain: str) -> None:
        """Skip buffered tasks for a blocked domain (DB pending left for resume)."""
        with self._cond:
            buf = self._buffers.pop(domain, None)
        if buf:
            for item in buf:
                self._repo.mark_frontier(item.url, "skipped", "domain_blocked")

    def note_http_result(
        self, domain: str, status_code: int | None, error: str | None
    ) -> bool:
        """Return True if this counts as a hard failure toward DOMAIN_BLOCKED."""
        if error:
            return True
        if status_code is None:
            return True
        if status_code in (403, 429) or status_code >= 500:
            return True
        return False

    def is_finished(self) -> bool:
        with self._cond:
            if self._in_flight or self._heap:
                return False
            for buf in self._buffers.values():
                if buf:
                    return False
            blocked = set(self._blocked)
        for domain in self._repo.pending_domains():
            if domain not in blocked:
                return False
        return True

    @property
    def blocked_count(self) -> int:
        with self._cond:
            return len(self._blocked)

    @property
    def in_flight_count(self) -> int:
        with self._cond:
            return len(self._in_flight)

    @property
    def active_domains(self) -> int:
        with self._cond:
            return len(self._heap_domains) + len(self._in_flight)

    def wake_all(self) -> None:
        with self._cond:
            self._cond.notify_all()


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()
