from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urldefrag, urlparse

import requests

from crawler.extractor import content_hash, extract
from crawler.fetcher import Fetcher, RateLimiter, RobotsCache
from crawler.policies import CrawlPolicies
from crawler.storage import Repository

USER_AGENT = "RIT-TEC-VideogamesCrawler/1.0 (+contact: student project)"


@dataclass(frozen=True)
class FrontierItem:
    url: str
    seed_id: str
    depth: int
    parent_url: str | None
    anchor_text: str


def _normalize_url(url: str) -> str:
    return urldefrag(url)[0]


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


class ThreadedCrawler:
    def __init__(
        self,
        policies: CrawlPolicies,
        repository: Repository,
        logger: logging.Logger,
        num_threads: int = 8,
        crawler_source: str = "custom",
    ) -> None:
        self._policies = policies
        self._repository = repository
        self._logger = logger
        self._num_threads = num_threads
        self._crawler_source = crawler_source

        self._session = requests.Session()
        self._fetcher = Fetcher(self._session, policies, user_agent=USER_AGENT)
        self._robots = RobotsCache(self._session)
        self._rate_limiter = RateLimiter(policies)

        self._frontier: queue.Queue[FrontierItem | None] = queue.Queue()
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._domain_counts: dict[str, int] = {}
        self._domain_lock = threading.Lock()

        self._stats_lock = threading.Lock()
        self._pages_saved = 0
        self._pages_skipped = 0
        self._errors = 0

    def crawl(self, seeds: list[tuple[str, str]]) -> None:
        start = time.monotonic()

        for seed_id, url in seeds:
            self._try_enqueue(
                FrontierItem(
                    url=url,
                    seed_id=seed_id,
                    depth=0,
                    parent_url=None,
                    anchor_text="",
                )
            )

        with ThreadPoolExecutor(max_workers=self._num_threads) as executor:
            futures = [
                executor.submit(self._worker)
                for _ in range(self._num_threads)
            ]
            self._frontier.join()
            for _ in range(self._num_threads):
                self._frontier.put(None)
            for future in futures:
                future.result()

        elapsed = time.monotonic() - start
        self._logger.info(
            "SUMMARY pages_saved=%d pages_skipped=%d errors=%d elapsed_s=%.2f",
            self._pages_saved,
            self._pages_skipped,
            self._errors,
            elapsed,
        )
        self._session.close()

    def _try_enqueue(self, item: FrontierItem) -> bool:
        normalized = _normalize_url(item.url)
        with self._seen_lock:
            if normalized in self._seen:
                self._logger.info(
                    "SKIP_DUPLICATE_URL url=%s depth=%d",
                    normalized,
                    item.depth,
                )
                with self._stats_lock:
                    self._pages_skipped += 1
                return False
            self._seen.add(normalized)

        if not self._reserve_domain(normalized):
            self._logger.info(
                "SKIP_DOMAIN_CAP url=%s depth=%d domain=%s max=%d",
                normalized,
                item.depth,
                _domain(normalized),
                self._policies.max_pages_per_domain,
            )
            with self._stats_lock:
                self._pages_skipped += 1
            return False

        self._frontier.put(
            FrontierItem(
                url=normalized,
                seed_id=item.seed_id,
                depth=item.depth,
                parent_url=item.parent_url,
                anchor_text=item.anchor_text,
            )
        )
        return True

    def _worker(self) -> None:
        while True:
            item = self._frontier.get()
            try:
                if item is None:
                    return
                self._process_item(item)
            finally:
                self._frontier.task_done()

    def _reserve_domain(self, url: str) -> bool:
        domain = _domain(url)
        with self._domain_lock:
            count = self._domain_counts.get(domain, 0)
            if count >= self._policies.max_pages_per_domain:
                return False
            self._domain_counts[domain] = count + 1
            return True

    def _release_domain_slot(self, url: str) -> None:
        domain = _domain(url)
        with self._domain_lock:
            count = self._domain_counts.get(domain, 0)
            if count > 0:
                self._domain_counts[domain] = count - 1

    def _process_item(self, item: FrontierItem) -> None:
        url = item.url
        depth = item.depth
        domain = _domain(url)

        if depth > self._policies.max_depth:
            self._logger.info(
                "SKIP_DEPTH_EXCEEDED url=%s depth=%d max_depth=%d",
                url,
                depth,
                self._policies.max_depth,
            )
            self._release_domain_slot(url)
            with self._stats_lock:
                self._pages_skipped += 1
            return

        # Depth-0 seeds are always accepted so traversal can start even when
        # the seed URL itself lacks scope keywords (e.g. store.steampowered.com).
        if depth > 0 and not self._policies.is_in_scope(url, item.anchor_text):
            self._logger.info(
                "SKIP_OUT_OF_SCOPE url=%s depth=%d anchor=%r",
                url,
                depth,
                item.anchor_text[:80],
            )
            self._release_domain_slot(url)
            with self._stats_lock:
                self._pages_skipped += 1
            return

        if self._policies.respect_robots_txt:
            if not self._robots.can_fetch(url, USER_AGENT):
                self._logger.info(
                    "SKIP_ROBOTS_DISALLOWED url=%s depth=%d domain=%s",
                    url,
                    depth,
                    domain,
                )
                self._release_domain_slot(url)
                with self._stats_lock:
                    self._pages_skipped += 1
                return

        robots_delay = (
            self._robots.crawl_delay(url)
            if self._policies.respect_robots_txt
            else None
        )
        override = None
        if robots_delay is not None:
            override = max(
                robots_delay,
                self._policies.min_request_interval_seconds,
            )

        self._rate_limiter.wait_if_needed(domain, override_interval=override)

        result = self._fetcher.fetch(url)
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
            return

        self._logger.info(
            "FETCH_OK url=%s depth=%d status=%d domain=%s final_url=%s",
            url,
            depth,
            result.status_code,
            domain,
            result.final_url,
        )

        if result.text_body is None:
            self._logger.info(
                "SKIP_CONTENT_TYPE url=%s depth=%d content_type=%s",
                url,
                depth,
                result.content_type,
            )
            with self._stats_lock:
                self._pages_skipped += 1
            return

        extracted = extract(result.text_body, result.final_url or url)
        page_hash = content_hash(extracted.text)

        if self._repository.is_duplicate(page_hash):
            self._logger.info(
                "SKIP_DUPLICATE_CONTENT url=%s depth=%d hash=%s",
                url,
                depth,
                page_hash[:16],
            )
            with self._stats_lock:
                self._pages_skipped += 1
        else:
            saved = self._repository.save_page(
                url=url,
                final_url=result.final_url or url,
                seed_id=item.seed_id,
                depth=depth,
                parent_url=item.parent_url,
                http_status=result.status_code or 0,
                content_type=result.content_type or "",
                title=extracted.title,
                text=extracted.text,
                content_hash=page_hash,
                language="",
                crawler_source=self._crawler_source,
            )
            if saved:
                self._logger.info(
                    "SAVE_PAGE url=%s depth=%d path=%s title=%r",
                    url,
                    depth,
                    saved,
                    extracted.title[:80],
                )
                with self._stats_lock:
                    self._pages_saved += 1
            else:
                self._logger.info(
                    "SKIP_DUPLICATE_URL_DB url=%s depth=%d",
                    url,
                    depth,
                )
                with self._stats_lock:
                    self._pages_skipped += 1

        link_urls = [abs_url for abs_url, _ in extracted.links]
        self._repository.save_links(url, link_urls)

        child_depth = depth + 1
        if child_depth > self._policies.max_depth:
            return

        enqueued_this_page: set[str] = set()
        for abs_url, anchor_text in extracted.links:
            normalized_child = _normalize_url(abs_url)
            if normalized_child in enqueued_this_page:
                continue
            enqueued_this_page.add(normalized_child)
            if not self._policies.is_in_scope(abs_url, anchor_text):
                continue
            self._try_enqueue(
                FrontierItem(
                    url=abs_url,
                    seed_id=item.seed_id,
                    depth=child_depth,
                    parent_url=url,
                    anchor_text=anchor_text,
                )
            )
