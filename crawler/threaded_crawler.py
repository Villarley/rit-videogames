from __future__ import annotations

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from crawler import USER_AGENT
from crawler.extractor import content_hash, extract
from crawler.fetcher import Fetcher, RobotsCache
from crawler.frontier import DomainScheduler, FrontierTask, domain_of
from crawler.policies import CrawlPolicies
from crawler.progress import (
    ProgressReporter,
    ProgressSnapshot,
    free_disk_bytes,
)
from crawler.storage import Repository

_GB = 1024 ** 3
_LOW_DISK_BYTES = 2 * _GB


@dataclass
class SeedSpec:
    seed_id: str
    url: str
    scope_mode: str
    idioma: str
    subtema: str = ""


class ThreadedCrawler:
    def __init__(
        self,
        policies: CrawlPolicies,
        repository: Repository,
        logger: logging.Logger,
        num_threads: int = 32,
        crawler_source: str = "custom",
        target_bytes: int = 0,
        progress_every: float = 20.0,
        text_root: str = "repository",
    ) -> None:
        self._policies = policies
        self._repository = repository
        self._logger = logger
        self._num_threads = num_threads
        self._crawler_source = crawler_source
        self._target_bytes = target_bytes
        self._progress_every = progress_every
        self._text_root = text_root

        self._fetcher = Fetcher(policies, user_agent=USER_AGENT)
        self._robots = RobotsCache(
            timeout=policies.request_timeout,
            user_agent=USER_AGENT,
        )

        self._stop_event = threading.Event()
        self._hard_stop = threading.Event()
        self._sigint_count = 0
        self._stop_reason = "finished"

        self._scheduler = DomainScheduler(
            repository=repository,
            max_pages_per_domain=policies.max_pages_per_domain,
            min_interval=policies.min_request_interval_seconds,
            logger=logger,
            stop_event=self._stop_event,
        )

        self._stats_lock = threading.Lock()
        self._pages_saved = 0
        self._pages_skipped = 0
        self._errors = 0
        self._bytes_saved_run = 0

        # Running counter initialized from DB at crawl start.
        self._text_bytes_total = 0
        self._pages_total_start = 0

        # Interval rate tracking for progress.
        self._rate_lock = threading.Lock()
        self._rate_pages = 0
        self._rate_bytes = 0
        self._rate_t0 = time.monotonic()

    def crawl(self, seeds: list[SeedSpec]) -> str:
        start = time.monotonic()
        self._text_bytes_total = self._repository.text_bytes_total(
            self._crawler_source
        )
        self._pages_total_start = self._repository.pages_count(
            self._crawler_source
        )

        self._install_sigint_handler()
        self._scheduler.start()
        self._enqueue_seeds(seeds)

        progress_stop = threading.Event()
        reporter = ProgressReporter(
            interval=self._progress_every,
            target_bytes=self._target_bytes,
            get_snapshot=self._progress_snapshot,
            stop_event=progress_stop,
        )
        reporter.start()

        try:
            with ThreadPoolExecutor(max_workers=self._num_threads) as executor:
                futures = [
                    executor.submit(self._worker)
                    for _ in range(self._num_threads)
                ]
                while True:
                    if self._hard_stop.is_set():
                        self._stop_reason = "hard_stop"
                        self._stop_event.set()
                        self._scheduler.wake_all()
                        break
                    if self._stop_event.is_set():
                        break
                    if self._target_reached():
                        self._stop_reason = "target_reached"
                        self._stop_event.set()
                        self._scheduler.wake_all()
                        break
                    if self._low_disk():
                        self._logger.warning("LOW_DISK_STOP free_bytes_lt=2GB")
                        self._stop_reason = "low_disk"
                        self._stop_event.set()
                        self._scheduler.wake_all()
                        break
                    if self._scheduler.is_finished():
                        self._stop_reason = "finished"
                        self._stop_event.set()
                        self._scheduler.wake_all()
                        break
                    # Wait a bit; workers also exit on finished/stop.
                    done, _pending = wait(futures, timeout=0.5)
                    if len(done) == len(futures):
                        if self._stop_reason == "finished" and not self._stop_event.is_set():
                            self._stop_reason = "finished"
                        break
                self._stop_event.set()
                self._scheduler.wake_all()
                for future in futures:
                    try:
                        future.result(timeout=120)
                    except Exception as exc:
                        self._logger.warning("WORKER_ERROR error=%s", exc)
        finally:
            progress_stop.set()
            reporter.stop()
            self._restore_sigint_handler()

        elapsed = time.monotonic() - start
        with self._stats_lock:
            saved = self._pages_saved
            skipped = self._pages_skipped
            errors = self._errors
        self._logger.info(
            "SUMMARY pages_saved=%d pages_skipped=%d errors=%d "
            "elapsed_s=%.2f text_bytes=%d stop_reason=%s",
            saved,
            skipped,
            errors,
            elapsed,
            self._text_bytes_total,
            self._stop_reason,
        )
        print(
            f"SUMMARY pages_saved={saved} pages_skipped={skipped} "
            f"errors={errors} elapsed_s={elapsed:.2f} "
            f"text_gb={self._text_bytes_total / _GB:.3f} "
            f"stop_reason={self._stop_reason}",
            flush=True,
        )
        return self._stop_reason

    def _enqueue_seeds(self, seeds: list[SeedSpec]) -> None:
        items: list[dict] = []
        for seed in seeds:
            normalized = self._policies.normalize_url(seed.url)
            if not normalized:
                self._logger.info("SKIP_BAD_SEED url=%s", seed.url)
                continue
            denied = self._policies.is_denied(normalized)
            if denied:
                self._logger.info(
                    "SKIP_DENIED_SEED url=%s reason=%s", normalized, denied
                )
                continue
            domain = domain_of(normalized)
            rule = self._policies.host_rule_for(normalized)
            scope_rule = rule.mode if rule else "seed"
            items.append(
                {
                    "url": normalized,
                    "domain": domain,
                    "seed_id": seed.seed_id,
                    "depth": 0,
                    "parent_url": None,
                    "anchor_text": "",
                    "scope_rule": scope_rule,
                    "parent_topical": True,
                }
            )
        inserted = self._scheduler.add_urls(items)
        self._logger.info(
            "SEEDS_ENQUEUED requested=%d inserted=%d",
            len(items),
            len(inserted),
        )

    def _worker(self) -> None:
        while not self._hard_stop.is_set():
            if self._stop_event.is_set() and self._scheduler.is_finished():
                return
            task = self._scheduler.next_task(self._stop_event)
            if task is None:
                return
            try:
                self._process_task(task)
            except Exception as exc:
                self._logger.info(
                    "FETCH_ERROR url=%s depth=%d domain=%s error=%s",
                    task.url,
                    task.depth,
                    task.domain,
                    exc,
                )
                with self._stats_lock:
                    self._errors += 1
                self._scheduler.task_done(
                    task,
                    delay_seconds=self._policies.min_request_interval_seconds,
                    status="error",
                    reason=str(exc)[:200],
                    success=False,
                )

    def _process_task(self, task: FrontierTask) -> None:
        url = task.url
        depth = task.depth
        domain = task.domain
        min_interval = self._policies.min_request_interval_seconds

        if depth > self._policies.max_depth:
            self._logger.info(
                "SKIP_DEPTH_EXCEEDED url=%s depth=%d max_depth=%d",
                url,
                depth,
                self._policies.max_depth,
            )
            self._bump_skipped()
            self._scheduler.task_done(
                task, min_interval, status="skipped", reason="depth"
            )
            return

        # Robots
        robots_delay = 0.0
        if self._policies.respect_robots_txt:
            warning = None
            allowed = self._robots.can_fetch(url, USER_AGENT)
            warning = self._robots.pop_load_warning(url)
            if warning:
                if warning.startswith("crawl_delay_capped:"):
                    self._logger.info(
                        "CRAWL_DELAY_CAPPED domain=%s raw=%s cap=30",
                        domain,
                        warning.split(":", 1)[-1],
                    )
                else:
                    self._logger.info(
                        "ROBOTS_LOAD_WARNING domain=%s detail=%s",
                        domain,
                        warning,
                    )
            if not allowed:
                self._logger.info(
                    "SKIP_ROBOTS_DISALLOWED url=%s depth=%d domain=%s",
                    url,
                    depth,
                    domain,
                )
                self._bump_skipped()
                self._scheduler.task_done(
                    task, min_interval, status="skipped", reason="robots"
                )
                return
            delay = self._robots.crawl_delay(url)
            # Cap warning may appear after crawl_delay call.
            cap_warn = self._robots.pop_load_warning(url)
            if cap_warn and cap_warn.startswith("crawl_delay_capped:"):
                self._logger.info(
                    "CRAWL_DELAY_CAPPED domain=%s raw=%s cap=30",
                    domain,
                    cap_warn.split(":", 1)[-1],
                )
            if delay is not None:
                robots_delay = delay

        politeness = max(min_interval, robots_delay)

        result = self._fetcher.fetch(url)

        # Seed redirect host registration (depth 0 only).
        if depth == 0 and result.final_url and not result.error:
            self._maybe_register_redirect_host(url, result.final_url, task)

        if result.error:
            self._logger.info(
                "FETCH_ERROR url=%s depth=%d domain=%s error=%s",
                url,
                depth,
                domain,
                result.error,
            )
            with self._stats_lock:
                self._errors += 1
            self._scheduler.task_done(
                task,
                politeness,
                status="error",
                reason=result.error[:200],
                success=False,
                retry=True,
            )
            return

        status = result.status_code or 0
        hard_fail = self._scheduler.note_http_result(
            domain, result.status_code, result.error
        )

        if status in (429, 503):
            self._logger.info(
                "FETCH_BACKOFF url=%s depth=%d status=%d retry_after=%s",
                url,
                depth,
                status,
                result.retry_after_seconds,
            )
            with self._stats_lock:
                self._errors += 1
            self._scheduler.task_done(
                task,
                politeness,
                reason=f"http_{status}",
                success=False,
                retry=True,
                retry_after=result.retry_after_seconds,
            )
            return

        if status < 200 or status >= 300:
            self._logger.info(
                "FETCH_HTTP url=%s depth=%d status=%d domain=%s",
                url,
                depth,
                status,
                domain,
            )
            if hard_fail:
                with self._stats_lock:
                    self._errors += 1
            self._bump_skipped()
            self._scheduler.task_done(
                task,
                politeness,
                status="skipped",
                reason=f"http_{status}",
                success=not hard_fail,
            )
            return

        self._logger.info(
            "FETCH_OK url=%s depth=%d status=%d domain=%s final_url=%s "
            "bytes=%d ms=%d truncated=%s",
            url,
            depth,
            status,
            domain,
            result.final_url,
            result.bytes_downloaded,
            result.elapsed_ms,
            int(result.truncated),
        )

        if result.text_body is None:
            self._logger.info(
                "SKIP_CONTENT_TYPE url=%s depth=%d content_type=%s",
                url,
                depth,
                result.content_type,
            )
            self._bump_skipped()
            self._scheduler.task_done(
                task,
                politeness,
                status="skipped",
                reason="content_type",
                success=True,
            )
            return

        extracted = extract(result.text_body, result.final_url or url)
        words = len(extracted.text.split())
        hits, density = self._policies.topical_score(extracted.text)
        host_rule = self._policies.host_rule_for(url)
        page_is_topical = self._policies.is_topical(extracted.text)

        # Decide whether to save.
        save_page = True
        if words < self._policies.min_words:
            self._logger.info(
                "SKIP_THIN_CONTENT url=%s depth=%d words=%d",
                url,
                depth,
                words,
            )
            self._bump_skipped()
            save_page = False

        if (
            save_page
            and host_rule is not None
            and host_rule.mode == "topical"
            and depth > 0
            and not page_is_topical
        ):
            self._logger.info(
                "SKIP_OFF_TOPIC url=%s depth=%d hits=%d density=%.2f",
                url,
                depth,
                hits,
                density,
            )
            self._bump_skipped()
            save_page = False

        # For link following: topical parent only if page is topical (or
        # non-topical host modes, treat as topical_parent=False so domain/
        # prefix rules apply via link_in_scope mode checks).
        parent_topical_for_children = bool(
            host_rule is not None
            and host_rule.mode == "topical"
            and page_is_topical
        )

        outlinks_total = len(extracted.links)
        child_depth = depth + 1
        to_enqueue: list[dict] = []
        in_scope_count = 0

        if child_depth <= self._policies.max_depth:
            seen_children: set[str] = set()
            for abs_url, anchor_text in extracted.links:
                normalized = self._policies.normalize_url(abs_url)
                if not normalized or normalized in seen_children:
                    continue
                seen_children.add(normalized)
                ok, rule_name = self._policies.link_in_scope(
                    normalized,
                    anchor_text,
                    parent_topical_for_children,
                )
                if not ok:
                    continue
                in_scope_count += 1
                child_domain = domain_of(normalized)
                if not child_domain:
                    continue
                to_enqueue.append(
                    {
                        "url": normalized,
                        "domain": child_domain,
                        "seed_id": task.seed_id,
                        "depth": child_depth,
                        "parent_url": url,
                        "anchor_text": anchor_text[:500],
                        "scope_rule": rule_name,
                        "parent_topical": parent_topical_for_children,
                    }
                )

        if save_page:
            page_hash = content_hash(extracted.text)
            text_bytes = len(extracted.text.encode("utf-8"))
            save_result = self._repository.save_page(
                url=url,
                final_url=result.final_url or url,
                seed_id=task.seed_id,
                depth=depth,
                parent_url=task.parent_url,
                http_status=status,
                content_type=result.content_type or "",
                title=extracted.title,
                text=extracted.text,
                content_hash=page_hash,
                language=extracted.language,
                crawler_source=self._crawler_source,
                text_bytes=text_bytes,
                outlinks_total=outlinks_total,
                outlinks_in_scope=in_scope_count,
                topical_hits=hits,
                topical_density=density,
                scope_rule=task.scope_rule,
                truncated=1 if result.truncated else 0,
                fetch_ms=result.elapsed_ms,
            )
            if save_result == "saved":
                self._logger.info(
                    "SAVE_PAGE url=%s depth=%d path_hash=%s title=%r "
                    "words=%d lang=%s bytes=%d",
                    url,
                    depth,
                    page_hash[:16],
                    extracted.title[:80],
                    words,
                    extracted.language,
                    text_bytes,
                )
                with self._stats_lock:
                    self._pages_saved += 1
                    self._bytes_saved_run += text_bytes
                    self._text_bytes_total += text_bytes
                self._note_rate(1, text_bytes)
            elif save_result == "duplicate_content":
                self._logger.info(
                    "SKIP_DUPLICATE_CONTENT url=%s depth=%d hash=%s",
                    url,
                    depth,
                    page_hash[:16],
                )
                self._bump_skipped()
            elif save_result == "unchanged":
                self._logger.info(
                    "REVISIT_UNCHANGED url=%s depth=%d hash=%s",
                    url,
                    depth,
                    page_hash[:16],
                )
            elif save_result == "updated":
                self._logger.info(
                    "REVISIT_UPDATED url=%s depth=%d hash=%s",
                    url,
                    depth,
                    page_hash[:16],
                )
                with self._stats_lock:
                    self._pages_saved += 1
                self._note_rate(1, text_bytes)

        inserted = self._scheduler.add_urls(to_enqueue)
        if inserted:
            self._repository.save_links(url, inserted)

        self._logger.info(
            "LINKS url=%s total=%d in_scope=%d enqueued=%d",
            url,
            outlinks_total,
            in_scope_count,
            len(inserted),
        )

        self._scheduler.task_done(
            task,
            politeness,
            status="done",
            reason=None,
            success=True,
        )

        if self._target_reached():
            self._stop_reason = "target_reached"
            self._stop_event.set()
            self._scheduler.wake_all()

    def _maybe_register_redirect_host(
        self,
        seed_url: str,
        final_url: str,
        task: FrontierTask,
    ) -> None:
        seed_host = domain_of(seed_url)
        final_host = domain_of(final_url)
        if not final_host or final_host == seed_host:
            return
        existing = self._policies.host_rule_for(seed_url)
        if existing is None:
            return
        if self._policies.host_rule_for(final_url) is not None:
            # Already allowed; still log if newly seen via redirect.
            pass
        self._policies.register_host(final_host, existing)
        self._logger.info(
            "SEED_REDIRECT_HOST seed_id=%s from=%s to=%s mode=%s",
            task.seed_id,
            seed_host,
            final_host,
            existing.mode,
        )

    def _bump_skipped(self) -> None:
        with self._stats_lock:
            self._pages_skipped += 1

    def _note_rate(self, pages: int, raw_bytes: int) -> None:
        with self._rate_lock:
            self._rate_pages += pages
            self._rate_bytes += raw_bytes

    def _target_reached(self) -> bool:
        if self._target_bytes <= 0:
            return False
        return self._text_bytes_total >= self._target_bytes

    def _low_disk(self) -> bool:
        try:
            return free_disk_bytes(self._text_root) < _LOW_DISK_BYTES
        except OSError:
            return False

    def _progress_snapshot(self) -> ProgressSnapshot:
        with self._rate_lock:
            now = time.monotonic()
            dt = max(0.001, now - self._rate_t0)
            pps = self._rate_pages / dt
            mbs = (self._rate_bytes / dt) / (1024 * 1024)
            self._rate_pages = 0
            self._rate_bytes = 0
            self._rate_t0 = now

        with self._stats_lock:
            saved_run = self._pages_saved
            errors = self._errors
            text_bytes = self._text_bytes_total

        pages_total = self._pages_total_start + saved_run
        eta = None
        if self._target_bytes > 0 and mbs > 0:
            remain = max(0, self._target_bytes - text_bytes)
            eta = remain / (mbs * 1024 * 1024)

        return ProgressSnapshot(
            pages_saved_run=saved_run,
            pages_saved_total=pages_total,
            text_bytes_total=text_bytes,
            target_bytes=self._target_bytes,
            pages_per_sec=pps,
            mb_per_sec=mbs,
            eta_seconds=eta,
            active_domains=self._scheduler.active_domains,
            in_flight=self._scheduler.in_flight_count,
            errors=errors,
            blocked_domains=self._scheduler.blocked_count,
        )

    def _install_sigint_handler(self) -> None:
        self._prev_handler = None
        try:
            if threading.current_thread() is not threading.main_thread():
                return
            self._prev_handler = signal.getsignal(signal.SIGINT)

            def _handler(signum: int, frame: object) -> None:
                self._sigint_count += 1
                if self._sigint_count == 1:
                    self._logger.warning(
                        "SIGINT_GRACEFUL finishing in-flight requests"
                    )
                    print(
                        "Ctrl+C: graceful stop (press again to force exit)",
                        flush=True,
                    )
                    self._stop_reason = "sigint"
                    self._stop_event.set()
                    self._scheduler.wake_all()
                else:
                    self._logger.warning("SIGINT_HARD exiting")
                    print("Ctrl+C: hard stop", flush=True)
                    self._hard_stop.set()
                    self._stop_event.set()
                    self._scheduler.wake_all()

            signal.signal(signal.SIGINT, _handler)
        except (ValueError, OSError):
            # signal only works in main thread; KeyboardInterrupt still works.
            pass

    def _restore_sigint_handler(self) -> None:
        try:
            if (
                threading.current_thread() is threading.main_thread()
                and getattr(self, "_prev_handler", None) is not None
            ):
                signal.signal(signal.SIGINT, self._prev_handler)
        except (ValueError, OSError):
            pass
