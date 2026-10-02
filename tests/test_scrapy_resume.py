from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scrapy import Request
from scrapy.settings import Settings
from twisted.python.failure import Failure

from crawler.extractor import content_hash
from scrapy_crawler.middlewares import AlreadyStored, ResumeRepositoryMiddleware
from scrapy_crawler.spiders.videogames import VideogamesSpider


def save_page(repo, url="https://example.test/page", source="scrapy", days=0):
    text = "game " * 60 + url
    repo.save_page(
        url=url, final_url=url, seed_id="seed", depth=2, parent_url=None,
        http_status=200, content_type="text/html", title="Game", text=text,
        content_hash=content_hash(text), language="en", crawler_source=source,
        scope_rule="domain",
    )
    fetched = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    repo._conn.execute("UPDATE pages SET fetched_at=? WHERE url=?", (fetched, url))
    repo._conn.commit()


def middleware(repo, resuming=True):
    crawler = SimpleNamespace(settings=Settings({
        "RIT_DB": repo._db_path, "RIT_TEXT_ROOT": str(repo._text_root),
        "RIT_RESUMING": resuming,
    }), stats=Mock())
    result = ResumeRepositoryMiddleware(crawler)
    result.spider_opened(SimpleNamespace(logger=Mock()))
    return result


def test_completed_resume_request_skipped_without_fetch_error(repo):
    save_page(repo)
    mw = middleware(repo)
    request = Request("https://example.test/page", meta={"depth": 2, "scope_rule": "domain"})
    with pytest.raises(AlreadyStored) as caught:
        mw.process_request(request)
    mw.crawler.stats.inc_value.assert_called_once_with("resume/already_stored")
    spider = VideogamesSpider()
    spider.errback(Failure(caught.value))
    assert spider.rit_errors == 0


@pytest.mark.parametrize("kwargs", [
    {"meta": {"depth": 0, "scope_rule": "domain"}},
    {"meta": {"depth": 1, "scope_rule": "domain"}},
    {"meta": {"depth": 2, "scope_rule": "topical"}},
    {"meta": {"depth": 2, "scope_rule": "domain", "retry_times": 1}},
    {"meta": {"depth": 2, "scope_rule": "domain", "redirect_times": 1}},
    {"meta": {"depth": 2, "scope_rule": "domain"}, "dont_filter": True},
    {"meta": {"depth": 2, "scope_rule": "domain"}, "method": "POST"},
    {"meta": {"depth": 2, "scope_rule": "domain"}, "body": b"query"},
])
def test_requests_that_must_still_run(repo, kwargs):
    save_page(repo)
    assert middleware(repo).process_request(Request("https://example.test/page", **kwargs)) is None


@pytest.mark.parametrize("source,days,resuming", [
    ("custom", 0, True), ("scrapy", 8, True), ("scrapy", 0, False),
])
def test_other_crawler_stale_and_fresh_job_are_not_skipped(repo, source, days, resuming):
    save_page(repo, source=source, days=days)
    request = Request("https://example.test/page", meta={"depth": 2, "scope_rule": "domain"})
    assert middleware(repo, resuming).process_request(request) is None


def test_unknown_missing_text_and_expired_pages_are_not_skipped(repo, monkeypatch):
    save_page(repo)
    mw = middleware(repo)
    assert mw.process_request(Request("https://example.test/new", meta={"depth": 2})) is None
    request = Request("https://example.test/page", meta={"depth": 2, "scope_rule": "domain"})
    monkeypatch.setattr("scrapy_crawler.middlewares.time.time", lambda: 10**12)
    assert mw.process_request(request) is None
    monkeypatch.undo()
    path = repo._conn.execute("SELECT text_path FROM pages").fetchone()[0]
    (repo._text_root / path).unlink()
    assert mw.process_request(request) is None


def test_unavailable_database_falls_back_to_normal_downloads(tmp_path):
    crawler = SimpleNamespace(settings=Settings({
        "RIT_DB": str(tmp_path / "missing.db"), "RIT_RESUMING": True,
    }), stats=Mock())
    mw = ResumeRepositoryMiddleware(crawler)
    spider = SimpleNamespace(logger=Mock())
    mw.spider_opened(spider)
    assert mw._recent == {}
    assert not (tmp_path / "missing.db").exists()
    spider.logger.warning.assert_called_once()
