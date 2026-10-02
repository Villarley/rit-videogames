import logging
from types import SimpleNamespace
from unittest.mock import Mock

from scrapy.settings import Settings

from crawler.main import _parse_args
from crawler.extractor import content_hash
from crawler.project_total import ProjectTotal
from crawler.threaded_crawler import ThreadedCrawler
from scrapy_crawler.extensions import TargetSizeExtension
from scrapy_crawler.run import _parse_args as scrapy_args


def store(repo, url, source, size):
    return repo.save_page(
        url=url, final_url=url, seed_id="s", depth=0, parent_url=None,
        http_status=200, content_type="text/html", title="", text=url,
        content_hash=content_hash(url), language="en", crawler_source=source, text_bytes=size,
    )


def test_total_observes_other_writer_and_counts_no_duplicate(repo):
    from crawler.storage import Repository
    store(repo, "https://example.test/a", "custom", 60)
    tracker = ProjectTotal(repo.db_path)
    assert tracker.refresh() == 60
    other = Repository(repo.db_path, str(repo._text_root))
    try:
        store(other, "https://example.test/b", "scrapy", 45)
        assert tracker.refresh() == 105
        assert store(other, "https://example.test/b", "scrapy", 45) == "unchanged"
        assert tracker.refresh() == 105
    finally:
        other.close()


def test_custom_stops_at_combined_target_before_starting_workers(repo):
    from crawler.policies import CrawlPolicies
    store(repo, "https://example.test/a", "custom", 60)
    store(repo, "https://example.test/b", "scrapy", 45)
    crawler = ThreadedCrawler(CrawlPolicies(), repo, logging.getLogger(__name__), target_bytes=100)
    crawler._scheduler = Mock()
    assert crawler.crawl([]) == "target_reached"
    crawler._scheduler.start.assert_not_called()


def test_scrapy_stops_on_other_crawlers_contribution():
    crawler = SimpleNamespace(settings=Settings({"RIT_TARGET_GB": 10}), signals=Mock(), engine=Mock())
    ext = TargetSizeExtension(crawler)
    tracker = Mock()
    tracker.snapshot.return_value = (10 * 1024**3, 100)
    spider = SimpleNamespace(rit_text_bytes=1024, rit_project_total=tracker)
    ext._maybe_stop(spider)
    ext._maybe_stop(spider)
    crawler.engine.close_spider.assert_called_once_with(spider, "target_reached")


def test_both_defaults_are_ten_gib_combined():
    assert _parse_args([]).target_gb == scrapy_args([]).target_gb == 10


def test_scrapy_start_does_not_enqueue_anything_when_project_is_complete():
    import asyncio
    import pytest
    from scrapy.crawler import Crawler
    from scrapy.exceptions import CloseSpider
    from scrapy_crawler.spiders.videogames import VideogamesSpider
    spider = VideogamesSpider.from_crawler(Crawler(VideogamesSpider, Settings({"RIT_TARGET_GB": 10})))
    spider.rit_project_total = Mock()
    spider.rit_project_total.snapshot.return_value = (10 * 1024**3, 0)
    async def start():
        return [request async for request in spider.start()]
    with pytest.raises(CloseSpider) as caught:
        asyncio.run(start())
    assert caught.value.reason == "target_reached"
