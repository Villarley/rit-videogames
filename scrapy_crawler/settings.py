"""Scrapy settings: broad-crawl tuned, BFS, polite."""

from __future__ import annotations

BOT_NAME = "rit_videogames_scrapy"

SPIDER_MODULES = ["scrapy_crawler.spiders"]
NEWSPIDER_MODULE = "scrapy_crawler.spiders"

USER_AGENT = (
    "RIT-TEC-VideogamesCrawler-Scrapy/1.0 "
    "(+https://github.com/Villarley/rit-videogames)"
)

ROBOTSTXT_OBEY = True

CONCURRENT_REQUESTS = 64
CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 1.0
# ±50% jitter (replaces deprecated RANDOMIZE_DOWNLOAD_DELAY in Scrapy 2.19).
DOWNLOAD_DELAY_JITTER = 0.5

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0

DEPTH_LIMIT = 6
# Positive DEPTH_PRIORITY + FIFO queues => BFS across depth.
DEPTH_PRIORITY = 1
SCHEDULER_DISK_QUEUE = "scrapy.squeues.PickleFifoDiskQueue"
SCHEDULER_MEMORY_QUEUE = "scrapy.squeues.FifoMemoryQueue"
# Scrapy 2.19 default; JOBDIR-compatible (persists per-slot queues under jobdir).
# Fair across many domains so one host does not monopolise downloader slots.
SCHEDULER_PRIORITY_QUEUE = "scrapy.pqueues.DownloaderAwarePriorityQueue"

DOWNLOAD_MAXSIZE = 5_000_000
DOWNLOAD_WARNSIZE = 0
DOWNLOAD_TIMEOUT = 20

RETRY_ENABLED = True
RETRY_TIMES = 2
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 522, 524, 408]

COOKIES_ENABLED = False
REDIRECT_ENABLED = True

REACTOR_THREADPOOL_MAXSIZE = 20

LOG_LEVEL = "INFO"
TELNETCONSOLE_ENABLED = False
HTTPCACHE_ENABLED = False

# REQUEST_FINGERPRINTER_IMPLEMENTATION removed in Scrapy 2.13+; 2.7 is default.
FEED_EXPORT_ENCODING = "utf-8"

ITEM_PIPELINES = {
    "scrapy_crawler.pipelines.RepositoryPipeline": 300,
}

EXTENSIONS = {
    "scrapy_crawler.extensions.ProgressExtension": 500,
    "scrapy_crawler.extensions.TargetSizeExtension": 510,
}

# Course / run.py overrides these via CrawlerProcess settings.
RIT_SEEDS = "seeds/seeds.csv"
RIT_DB = "repository/crawl.db"
RIT_TEXT_ROOT = "repository"
RIT_TARGET_GB = 10.0
RIT_MAX_PAGES_PER_DOMAIN = 100000
RIT_ALL_SEEDS = False
RIT_PROGRESS_EVERY = 20
