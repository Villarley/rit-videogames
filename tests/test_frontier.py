from __future__ import annotations

import logging
import threading
import time

import pytest

from crawler.frontier import DomainScheduler


def _frontier_row(url: str, domain: str) -> dict:
    return {
        "url": url,
        "domain": domain,
        "seed_id": "s1",
        "depth": 0,
        "parent_url": None,
        "anchor_text": "",
        "scope_rule": "",
        "parent_topical": False,
    }


def _make_scheduler(
    repo,
    *,
    max_pages: int = 3,
    min_interval: float = 0.05,
) -> tuple[DomainScheduler, threading.Event]:
    stop = threading.Event()
    sched = DomainScheduler(
        repository=repo,
        max_pages_per_domain=max_pages,
        min_interval=min_interval,
        logger=logging.getLogger("test.frontier"),
        stop_event=stop,
    )
    sched.start()
    return sched, stop


def _next_task_with_timeout(
    sched: DomainScheduler,
    stop: threading.Event,
    timeout: float = 2.0,
):
    result: list = []
    err: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(sched.next_task(stop))
        except BaseException as exc:
            err.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "next_task did not finish within timeout"
    if err:
        raise err[0]
    return result[0] if result else None


class TestAddUrls:
    def test_max_pages_per_domain_cap(self, repo) -> None:
        sched, _ = _make_scheduler(repo, max_pages=3)
        items = [
            _frontier_row(f"https://cap.example/p{i}", "cap.example")
            for i in range(5)
        ]
        inserted = sched.add_urls(items)
        assert len(inserted) == 3
        assert repo.frontier_domain_counts().get("cap.example", 0) == 3

    def test_duplicate_urls_not_reinserted(self, repo) -> None:
        sched, _ = _make_scheduler(repo, max_pages=10)
        row = _frontier_row("https://dup.example/only", "dup.example")
        first = sched.add_urls([row])
        second = sched.add_urls([row])
        assert first == ["https://dup.example/only"]
        assert second == []
        assert repo.frontier_domain_counts().get("dup.example", 0) == 1


class TestNextTaskPoliteness:
    def test_different_domains_without_waiting(self, repo) -> None:
        sched, stop = _make_scheduler(repo, max_pages=10, min_interval=0.05)
        sched.add_urls(
            [
                _frontier_row("https://alpha.example/a1", "alpha.example"),
                _frontier_row("https://beta.example/b1", "beta.example"),
            ]
        )
        task_a = _next_task_with_timeout(sched, stop, timeout=2.0)
        assert task_a is not None
        domain_a = task_a.domain

        task_b = _next_task_with_timeout(sched, stop, timeout=2.0)
        assert task_b is not None
        assert task_b.domain != domain_a

    def test_task_done_reopens_domain_after_delay(self, repo) -> None:
        sched, stop = _make_scheduler(repo, max_pages=10, min_interval=0.05)
        sched.add_urls(
            [
                _frontier_row("https://delay.example/d1", "delay.example"),
                _frontier_row("https://delay.example/d2", "delay.example"),
            ]
        )
        first = _next_task_with_timeout(sched, stop, timeout=2.0)
        assert first is not None
        sched.task_done(first, delay_seconds=0.0, success=True)

        deadline = time.monotonic() + 2.0
        second = None
        while time.monotonic() < deadline:
            second = _next_task_with_timeout(sched, stop, timeout=0.5)
            if second is not None and second.domain == "delay.example":
                break
            time.sleep(0.02)
        assert second is not None
        assert second.domain == "delay.example"


class TestResetInProgress:
    def test_new_scheduler_resets_in_progress(self, repo) -> None:
        stop = threading.Event()
        sched1, _ = _make_scheduler(repo, max_pages=10)
        sched1.add_urls([_frontier_row("https://reset.example/r1", "reset.example")])
        task = _next_task_with_timeout(sched1, stop, timeout=2.0)
        assert task is not None
        row = repo._conn.execute(
            "SELECT status FROM frontier WHERE url = ?",
            (task.url,),
        ).fetchone()
        assert row["status"] == "in_progress"

        sched2, _ = _make_scheduler(repo, max_pages=10)
        row = repo._conn.execute(
            "SELECT status FROM frontier WHERE url = ?",
            (task.url,),
        ).fetchone()
        assert row["status"] == "pending"
