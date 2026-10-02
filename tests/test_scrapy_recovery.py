import asyncio
from unittest.mock import Mock

from scrapy import Request
from scrapy.crawler import Crawler
from scrapy.http import HtmlResponse
from scrapy.settings import Settings
from scrapy.utils.request import RequestFingerprinter

from scrapy_crawler.middlewares import ResumeRepositoryMiddleware
from scrapy_crawler.recovery import RecoveryFingerprinter, omitted_link_pages, recovery_log_context
from scrapy_crawler.spiders.videogames import VideogamesSpider


def test_duplicate_links_do_not_exhaust_unique_domain_cap():
    spider = VideogamesSpider()
    spider._max_pages_per_domain = 2
    assert spider._try_schedule("example.test", "https://example.test/a?x=1&y=2")
    for _ in range(100):
        assert not spider._try_schedule("example.test", "https://example.test/a?y=2&x=1")
    assert spider._try_schedule("example.test", "https://example.test/b")
    assert not spider._try_schedule("example.test", "https://example.test/c")
    assert spider._scheduled_by_domain["example.test"] == 2


def test_max_depth_does_not_spend_quota():
    from crawler.policies import CrawlPolicies, HostRule
    spider = VideogamesSpider()
    spider.policies = CrawlPolicies(host_rules={"example.test": HostRule("domain")}, max_depth=1)
    request = Request("https://example.test/a", meta={"depth": 1})
    response = HtmlResponse(request.url, request=request, encoding="utf-8",
        body=b'<html><a href="/b">game</a><p>game</p></html>')
    assert not any(isinstance(item, Request) for item in spider.parse(response))
    assert spider._try_schedule("example.test", "https://example.test/b")


def test_recovery_fingerprints_preserve_history_and_are_separate():
    normal = Request("https://example.test/page")
    repair = normal.replace(meta={"rit_recover_links": True})
    fp = RecoveryFingerprinter()
    assert fp.fingerprint(normal) == RequestFingerprinter().fingerprint(normal)
    assert fp.fingerprint(repair) != fp.fingerprint(normal)
    assert fp.fingerprint(repair) == fp.fingerprint(repair.replace())
    crawler = Mock()
    crawler.settings = Settings()
    mw = ResumeRepositoryMiddleware(crawler)
    assert mw.process_request(repair) is None


def test_log_selection_includes_partial_and_zero_admission(tmp_path):
    log = tmp_path / "crawl.log"
    log.write_text(
        'LINKS url=https://example.test/a total=8 in_scope=5 enqueued=0\n'
        'LINKS url=https://example.test/b total=8 in_scope=5 enqueued=2\n'
        'LINKS url=https://example.test/c total=8 in_scope=5 enqueued=5\n'
        'SKIP_THIN_CONTENT url=https://example.test/a depth=2 words=0\n'
    )
    assert omitted_link_pages(str(log)) == {"https://example.test/a", "https://example.test/b"}
    assert recovery_log_context(str(log))[1] == {"https://example.test/a": 2}


def test_unique_budget_survives_spider_restart(repo, tmp_path):
    seeds = tmp_path / "seeds.csv"
    seeds.write_text('seed_id,url,scope_mode,crawler\ns1,https://example.test/,domain,scrapy\n')
    settings = Settings({"RIT_SEEDS": str(seeds), "RIT_DB": repo.db_path,
                         "RIT_TEXT_ROOT": str(repo._text_root), "RIT_MAX_PAGES_PER_DOMAIN": 3})
    async def requests(spider):
        return [r async for r in spider.start()]
    first = VideogamesSpider.from_crawler(Crawler(VideogamesSpider, settings))
    first.state = {}
    asyncio.run(requests(first))
    assert first._try_schedule("example.test", "https://example.test/a")
    second = VideogamesSpider.from_crawler(Crawler(VideogamesSpider, settings))
    import pickle
    second.state = pickle.loads(pickle.dumps(first.state))
    asyncio.run(requests(second))
    assert not second._try_schedule("example.test", "https://example.test/a")
    assert second._try_schedule("example.test", "https://example.test/b")
    assert not second._try_schedule("example.test", "https://example.test/c")
