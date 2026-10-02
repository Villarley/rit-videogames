"""Avoid replaying recently completed requests from a resumed JOBDIR."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrapy import signals
from scrapy.exceptions import IgnoreRequest

from crawler.policies import CrawlPolicies


class AlreadyStored(IgnoreRequest):
    """A resumed request already completed; this is not a fetch error."""


class ResumeRepositoryMiddleware:
    def __init__(self, crawler) -> None:
        self.crawler = crawler
        self._recent: dict[str, tuple[int, str, str, float]] = {}
        self._text_root = Path(crawler.settings.get("RIT_TEXT_ROOT", "repository"))

    @classmethod
    def from_crawler(cls, crawler):
        middleware = cls(crawler)
        crawler.signals.connect(middleware.spider_opened, signal=signals.spider_opened)
        return middleware

    def spider_opened(self, spider) -> None:
        # A fresh JOBDIR must still discover links, even when pages exist in DB.
        if not self.crawler.settings.getbool("RIT_RESUMING", False):
            return
        days = CrawlPolicies().revisit_after_days
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).isoformat()
        db = Path(self.crawler.settings.get("RIT_DB", "repository/crawl.db"))
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                # Only this crawler's completed pages have had their outgoing
                # requests scheduled in this job. Never borrow custom's pages.
                rows = conn.execute(
                    "SELECT url, depth, scope_rule, text_path, fetched_at FROM pages "
                    "WHERE crawler_source = 'scrapy' AND fetched_at >= ? "
                    "AND http_status >= 200 AND http_status < 300",
                    (cutoff,),
                )
                recent = {}
                for url, depth, rule, text_path, fetched_at in rows:
                    if depth is None or not rule or not text_path:
                        continue
                    try:
                        fetched = datetime.fromisoformat(fetched_at)
                        if fetched.tzinfo is None or fetched > now:
                            continue
                        expires = (fetched + timedelta(days=days)).timestamp()
                    except (TypeError, ValueError):
                        continue
                    recent[url] = (depth, rule, text_path, expires)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            spider.logger.warning("RESUME_INDEX_UNAVAILABLE error=%s", exc)
            return
        self._recent = recent
        spider.logger.info("RESUME_INDEX recent_pages=%d", len(recent))

    def process_request(self, request):
        if request.meta.get("rit_recover_links"):
            return None
        if request.method != "GET" or request.body or request.dont_filter:
            return None
        depth = request.meta.get("depth", 0)
        if depth <= 0 or request.meta.get("retry_times") or request.meta.get("redirect_times"):
            return None
        previous = self._recent.get(request.url)
        if previous is None:
            return None
        saved_depth, rule, text_path, expires = previous
        if (
            depth < saved_depth
            or request.meta.get("scope_rule") != rule
            or time.time() >= expires
            or not (self._text_root / text_path).is_file()
        ):
            return None
        self.crawler.stats.inc_value("resume/already_stored")
        raise AlreadyStored(request.url)
