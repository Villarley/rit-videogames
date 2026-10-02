"""Run a real Scrapy engine against a persisted queue, with no network."""

import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize("recover", [False, True])
def test_persisted_queue_skips_completed_page_and_keeps_new_links(tmp_path, recover):
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from scrapy import Request
from scrapy.crawler import CrawlerProcess
from scrapy.core.scheduler import Scheduler
from scrapy.http import HtmlResponse
from scrapy.settings import Settings
from scrapy.utils.request import RequestFingerprinter
from crawler.extractor import content_hash
from crawler.storage import Repository
from scrapy_crawler.spiders.videogames import VideogamesSpider

root = Path(sys.argv[1])
recover = sys.argv[2] == 'True'
db = root / 'crawl.db'
text_root = root / 'text'
job = root / 'job'
seed = root / 'seeds.csv'
recovery_log = root / 'old.log'
recovery_log.write_text('LINKS url=https://example.test/done total=3 in_scope=2 enqueued=0\n'
    'SKIP_THIN_CONTENT url=https://example.test/thin depth=1 words=0\n'
    'LINKS url=https://example.test/thin total=3 in_scope=2 enqueued=0\n')
seed.write_text('seed_id,url,scope_mode,crawler,subtema,idioma\ns1,https://example.test/,domain,scrapy,test,en\n')
repo = Repository(str(db), str(text_root))
text = 'saved game ' * 60
repo.save_page(url='https://example.test/done', final_url='https://example.test/done',
    seed_id='s1', depth=1, parent_url=None, http_status=200,
    content_type='text/html', title='Saved', text=text, content_hash=content_hash(text),
    language='en', crawler_source='scrapy', scope_rule='domain')
repo.close()

downloaded = []
class OfflineHandler:
    lazy = True
    def __init__(self, crawler):
        pass
    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)
    async def download_request(self, request):
        downloaded.append(request.url)
        if request.url.endswith('/done') and not request.meta.get('rit_recover_links'):
            raise AssertionError('Already completed page reached the downloader')
        if request.url.endswith('/robots.txt'):
            body = b'User-agent: *\nAllow: /\n'
        else:
            body = ('<html><body><main><p>' + request.url + ' game ' * 60 +
                '</p><a href="/child">game child</a><a href="/new">game new</a></main></body></html>').encode()
        return HtmlResponse(request.url, body=body, encoding='utf-8', request=request)
    async def close(self):
        pass

settings = Settings()
settings.setmodule('scrapy_crawler.settings')
settings.setdict({
    'JOBDIR': str(job), 'RIT_DB': str(db), 'RIT_TEXT_ROOT': str(text_root),
    'RIT_SEEDS': str(seed), 'RIT_RESUMING': True, 'RIT_PROGRESS_EVERY': 0,
    'RIT_RECOVER_LINKS': recover, 'RIT_RECOVERY_LOG': str(recovery_log),
    'RIT_TARGET_GB': 0, 'LOG_ENABLED': True, 'REMOTE_CONTROL_ENABLED': False,
    'DOWNLOAD_HANDLERS': {'https': '__main__.OfflineHandler'},
    'DOWNLOAD_DELAY': 0, 'AUTOTHROTTLE_ENABLED': False,
})
process = CrawlerProcess(settings)
crawler = process.create_crawler(VideogamesSpider)
# Prepare a JOBDIR using the same scheduler and request serialization as production.
crawler.request_fingerprinter = RequestFingerprinter()
from scrapy.statscollectors import MemoryStatsCollector
crawler.stats = MemoryStatsCollector(crawler)
crawler.engine = SimpleNamespace(downloader=SimpleNamespace(
    slots={}, get_slot_key=lambda request: urlsplit(request.url).hostname))
spider = VideogamesSpider.from_crawler(crawler)
crawler.spider = spider
scheduler = Scheduler.from_crawler(crawler)
scheduler.open(spider)
for path in (['done', ''] if recover else ['done', 'new']):
    assert scheduler.enqueue_request(Request('https://example.test/' + path,
        callback=spider.parse, errback=spider.errback,
        meta={'depth': 1, 'scope_rule': 'domain', 'seed_id': 's1'}))
assert crawler.stats.get_value('scheduler/enqueued/disk') == 2
if recover:
    # Model an exhausted old crawl: seeds and done were seen, queue now empty.
    while scheduler.next_request() is not None:
        pass
scheduler.close('shutdown')
process.crawl(crawler)
process.start()
assert crawler.stats.get_value('resume/already_stored', 0) == (0 if recover else 1), crawler.stats.get_stats()
assert crawler.stats.get_value('spider_exceptions/count', 0) == 0
assert crawler.spider.rit_errors == 0
assert 'https://example.test/new' in downloaded, downloaded
assert 'https://example.test/child' in downloaded, downloaded
assert 'https://example.test/robots.txt' in downloaded, downloaded
repo = Repository(str(db), str(text_root))
assert repo.pages_count('scrapy') == (5 if recover else 4)
repo.close()
if recover:
    assert 'https://example.test/done' in downloaded
    assert 'https://example.test/thin' in downloaded
    from scrapy.dupefilters import RFPDupeFilter
    from scrapy_crawler.recovery import RecoveryFingerprinter
    df = RFPDupeFilter(str(job), fingerprinter=RecoveryFingerprinter())
    assert df.request_seen(Request('https://example.test/done', meta={'rit_recover_links': True}))
    df.close('test')
print('RESUME_OK')
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), str(recover)],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESUME_OK" in result.stdout
