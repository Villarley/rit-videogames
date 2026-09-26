"""Persist PageItems into the shared Repository (crawler_source='scrapy')."""

from __future__ import annotations

from itemadapter import ItemAdapter
from scrapy import Spider
from scrapy.crawler import Crawler

from crawler.extractor import content_hash
from crawler.storage import Repository


class RepositoryPipeline:
    """Write pages to SQLite + text files via crawler.storage.Repository.

    Short SQLite writes run on the Twisted reactor thread. Acceptable here
    because each save is a single short transaction (WAL + busy_timeout);
    deferToThread would avoid rare reactor stalls under load but adds
    complexity for little gain at this write rate. Scrapy's dupefilter owns
    the frontier, so we skip the links table for this crawler.
    """

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self._repo: Repository | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> RepositoryPipeline:
        return cls(crawler)

    def open_spider(self, spider: Spider) -> None:
        settings = self.crawler.settings
        db = settings.get("RIT_DB", "repository/crawl.db")
        text_root = settings.get("RIT_TEXT_ROOT", "repository")
        self._repo = Repository(db, text_root)

        spider.rit_text_bytes = self._repo.text_bytes_total("scrapy")
        spider.rit_pages_saved_total = self._repo.pages_count("scrapy")
        spider.rit_pages_saved_run = 0
        spider.rit_pages_skipped = 0
        if not hasattr(spider, "rit_errors"):
            spider.rit_errors = 0

    def close_spider(self, spider: Spider) -> None:
        if self._repo is not None:
            self._repo.close()
            self._repo = None

    def process_item(self, item, spider: Spider):  # noqa: ANN001
        assert self._repo is not None
        adapter = ItemAdapter(item)
        text = adapter.get("text") or ""
        page_hash = content_hash(text)
        depth = int(adapter.get("depth") or 0)
        url = adapter.get("url") or ""
        text_bytes = int(adapter.get("text_bytes") or len(text.encode("utf-8")))

        result = self._repo.save_page(
            url=url,
            final_url=adapter.get("final_url") or url,
            seed_id=adapter.get("seed_id") or "",
            depth=depth,
            parent_url=adapter.get("parent_url"),
            http_status=int(adapter.get("http_status") or 0),
            content_type=adapter.get("content_type") or "",
            title=adapter.get("title") or "",
            text=text,
            content_hash=page_hash,
            language=adapter.get("language") or "",
            crawler_source="scrapy",
            text_bytes=text_bytes,
            outlinks_total=int(adapter.get("outlinks_total") or 0),
            outlinks_in_scope=int(adapter.get("outlinks_in_scope") or 0),
            topical_hits=int(adapter.get("topical_hits") or 0),
            topical_density=float(adapter.get("topical_density") or 0.0),
            scope_rule=adapter.get("scope_rule") or "",
            truncated=int(adapter.get("truncated") or 0),
            fetch_ms=int(adapter.get("fetch_ms") or 0),
        )

        if result == "saved":
            spider.logger.info(
                "SAVE_PAGE url=%s depth=%d path_hash=%s title=%r "
                "words=%d lang=%s bytes=%d",
                url,
                depth,
                page_hash[:16],
                (adapter.get("title") or "")[:80],
                len(text.split()),
                adapter.get("language") or "",
                text_bytes,
            )
            spider.rit_pages_saved_run = getattr(spider, "rit_pages_saved_run", 0) + 1
            spider.rit_pages_saved_total = (
                getattr(spider, "rit_pages_saved_total", 0) + 1
            )
            spider.rit_text_bytes = getattr(spider, "rit_text_bytes", 0) + text_bytes
            _note_rate(spider, 1, text_bytes)
        elif result == "duplicate_content":
            spider.logger.info(
                "SKIP_DUPLICATE_CONTENT url=%s depth=%d hash=%s",
                url,
                depth,
                page_hash[:16],
            )
            spider.rit_pages_skipped = getattr(spider, "rit_pages_skipped", 0) + 1
        elif result == "unchanged":
            spider.logger.info(
                "REVISIT_UNCHANGED url=%s depth=%d hash=%s",
                url,
                depth,
                page_hash[:16],
            )
        elif result == "updated":
            spider.logger.info(
                "REVISIT_UPDATED url=%s depth=%d hash=%s",
                url,
                depth,
                page_hash[:16],
            )
            spider.rit_pages_saved_run = getattr(spider, "rit_pages_saved_run", 0) + 1
            _note_rate(spider, 1, text_bytes)

        return item


def _note_rate(spider: Spider, pages: int, text_bytes: int) -> None:
    spider.rit_rate_pages = getattr(spider, "rit_rate_pages", 0) + pages
    spider.rit_rate_bytes = getattr(spider, "rit_rate_bytes", 0) + text_bytes
