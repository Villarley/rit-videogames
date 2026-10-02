"""Offline regressions for shared scoring and each crawler's page decisions."""

import logging
from unittest.mock import Mock

import pytest
import scrapy
from scrapy.http import HtmlResponse

from crawler.extractor import extract
from crawler.fetcher import FetchResult
from crawler.frontier import FrontierTask
from crawler.policies import CrawlPolicies, HostRule
from crawler.threaded_crawler import ThreadedCrawler
from scrapy_crawler.items import PageItem
from scrapy_crawler.spiders.videogames import VideogamesSpider


@pytest.mark.parametrize("engine", ["custom", "scrapy"])
@pytest.mark.parametrize(
    "mode,depth,body,saved,follow_unrelated",
    [
        ("topical", 1, "game " * 60, True, True),
        ("topical", 1, "recipe " * 60, False, False),
        ("topical", 0, "recipe " * 60, True, False),
        ("domain", 1, "recipe " * 60, True, True),
        ("topical", 1, "game " * 5, False, True),
        ("prefix", 1, "recipe " * 60, True, True),
    ],
)
def test_page_decisions_and_single_score(
    engine, mode, depth, body, saved, follow_unrelated, repo, monkeypatch
):
    url = "https://example.test/page"
    raw = (
        f"<html><body><main><p>{body}</p></main>"
        '<nav><a href="/unrelated">Unrelated</a>'
        '<a href="/game">game</a><a href="/game">duplicate</a>'
        '<a href="/asset.pdf">game</a></nav></body></html>'
    )
    policies = CrawlPolicies(
        host_rules={"example.test": HostRule(mode, ("/",))},
        respect_robots_txt=False,
    )
    expected_score = policies.topical_score(extract(raw, url).text)
    score = Mock(wraps=policies.topical_score)
    monkeypatch.setattr(policies, "topical_score", score)
    if engine == "custom":
        crawler = ThreadedCrawler(policies, repo, logging.getLogger(__name__))
        crawler._fetcher = Mock()
        crawler._fetcher.fetch.return_value = FetchResult(
            url, url, 200, "text/html", raw, None
        )
        crawler._scheduler = Mock()
        crawler._scheduler.add_urls.return_value = []
        save = Mock(wraps=repo.save_page)
        monkeypatch.setattr(repo, "save_page", save)
        crawler._process_task(
            FrontierTask(url, "example.test", "seed", depth, None, "", mode, False)
        )
        children = {item["url"] for item in crawler._scheduler.add_urls.call_args.args[0]}
        assert save.called == saved
        if saved:
            page = save.call_args.kwargs
    else:
        spider = VideogamesSpider()
        spider.policies = policies
        request = scrapy.Request(url, meta={"depth": depth, "seed_id": "seed"})
        response = HtmlResponse(url, body=raw.encode(), encoding="utf-8", request=request)
        output = list(spider.parse(response))
        children = {item.url for item in output if isinstance(item, scrapy.Request)}
        pages = [item for item in output if isinstance(item, PageItem)]
        assert len(pages) == int(saved)
        if saved:
            page = pages[0]
    assert score.call_count == 1
    expected_children = {"https://example.test/game"}
    if follow_unrelated:
        expected_children.add("https://example.test/unrelated")
    assert children == expected_children
    if saved:
        assert (page["topical_hits"], page["topical_density"]) == expected_score
        assert page["outlinks_in_scope"] == len(expected_children)
