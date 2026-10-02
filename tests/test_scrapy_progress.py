from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scrapy.settings import Settings

from scrapy_crawler.extensions import ProgressExtension, _pending_requests


@pytest.mark.parametrize("added_mb,expected_eta", [(0, "?"), (10, "1h39m"), (1010, "0s")])
def test_eta_uses_only_new_bytes_since_resume(monkeypatch, capsys, added_mb, expected_eta):
    monkeypatch.setattr("scrapy_crawler.extensions.task.LoopingCall", Mock())
    monkeypatch.setattr("scrapy_crawler.extensions.time.monotonic", lambda: 100)
    crawler = SimpleNamespace(
        settings=Settings({"RIT_TARGET_GB": 2}), signals=Mock(), stats=Mock(),
        engine=SimpleNamespace(_slot=SimpleNamespace(scheduler=[], inprogress=set())),
    )
    crawler.stats.get_value.return_value = 0
    spider = SimpleNamespace(rit_text_bytes=1048 * 1024**2)
    ext = ProgressExtension(crawler)
    ext.spider_opened(spider)
    spider.rit_text_bytes += added_mb * 1024**2
    monkeypatch.setattr("scrapy_crawler.extensions.time.monotonic", lambda: 160)
    ext._print_line(spider)
    # 2 GiB - 1048 MiB - 10 MiB = 990 MiB at 10 MiB/min = 99 min.
    assert f"eta={expected_eta}" in capsys.readouterr().out


def test_pending_counts_persistent_queue_and_inflight():
    slot = SimpleNamespace(scheduler=[1, 2, 3], inprogress={4, 5})
    assert _pending_requests(SimpleNamespace(engine=SimpleNamespace(_slot=slot))) == 5
