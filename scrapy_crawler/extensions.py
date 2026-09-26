"""Progress and target-size stop extensions for the Scrapy crawler."""

from __future__ import annotations

import time

from scrapy import signals
from scrapy.exceptions import NotConfigured
from twisted.internet import task

from crawler.progress import free_disk_bytes

_GB = 1024 ** 3
_LOW_DISK_BYTES = 2 * _GB


class TargetSizeExtension:
    """Close spider when scrapy text bytes hit RIT_TARGET_GB or disk is low."""

    def __init__(self, crawler) -> None:  # noqa: ANN001
        self.crawler = crawler
        target_gb = float(crawler.settings.getfloat("RIT_TARGET_GB") or 0.0)
        self._target_bytes = int(target_gb * _GB) if target_gb > 0 else 0
        self._text_root = crawler.settings.get("RIT_TEXT_ROOT", "repository")
        self._closed = False
        crawler.signals.connect(self._on_response, signal=signals.response_received)
        crawler.signals.connect(self._on_item, signal=signals.item_scraped)

    @classmethod
    def from_crawler(cls, crawler):  # noqa: ANN001
        return cls(crawler)

    def _on_response(self, response, request, spider) -> None:  # noqa: ANN001
        self._maybe_stop(spider)

    def _on_item(self, item, spider) -> None:  # noqa: ANN001
        self._maybe_stop(spider)

    def _maybe_stop(self, spider) -> None:  # noqa: ANN001
        if self._closed:
            return
        text_bytes = int(getattr(spider, "rit_text_bytes", 0) or 0)
        if self._target_bytes > 0 and text_bytes >= self._target_bytes:
            self._closed = True
            self.crawler.engine.close_spider(spider, "target_reached")
            return
        try:
            free = free_disk_bytes(self._text_root)
        except OSError:
            return
        if free < _LOW_DISK_BYTES:
            self._closed = True
            spider.logger.warning("LOW_DISK_STOP free_bytes_lt=2GB")
            self.crawler.engine.close_spider(spider, "low_disk")


class ProgressExtension:
    """Print a one-line PROGRESS status every RIT_PROGRESS_EVERY seconds."""

    def __init__(self, crawler) -> None:  # noqa: ANN001
        interval = float(crawler.settings.getfloat("RIT_PROGRESS_EVERY") or 20.0)
        if interval <= 0:
            raise NotConfigured("RIT_PROGRESS_EVERY <= 0")
        self.crawler = crawler
        self._interval = max(1.0, interval)
        target_gb = float(crawler.settings.getfloat("RIT_TARGET_GB") or 0.0)
        self._target_bytes = int(target_gb * _GB) if target_gb > 0 else 0
        self._loop: task.LoopingCall | None = None
        self._t0 = time.monotonic()
        crawler.signals.connect(self.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(self.spider_closed, signal=signals.spider_closed)

    @classmethod
    def from_crawler(cls, crawler):  # noqa: ANN001
        return cls(crawler)

    def spider_opened(self, spider) -> None:  # noqa: ANN001
        spider.rit_rate_pages = 0
        spider.rit_rate_bytes = 0
        spider.rit_rate_t0 = time.monotonic()
        self._t0 = time.monotonic()
        self._loop = task.LoopingCall(self._tick, spider)
        self._loop.start(self._interval, now=False)

    def spider_closed(self, spider, reason) -> None:  # noqa: ANN001
        if self._loop is not None and self._loop.running:
            self._loop.stop()
        self._loop = None

    def _tick(self, spider) -> None:  # noqa: ANN001
        try:
            self._print_line(spider)
        except Exception as exc:
            print(f"PROGRESS error={exc}", flush=True)

    def _print_line(self, spider) -> None:  # noqa: ANN001
        text_bytes = int(getattr(spider, "rit_text_bytes", 0) or 0)
        saved_run = int(getattr(spider, "rit_pages_saved_run", 0) or 0)
        saved_total = int(getattr(spider, "rit_pages_saved_total", 0) or 0)
        errors = int(getattr(spider, "rit_errors", 0) or 0)
        if self.crawler.stats is not None:
            # Prefer spider counter; fall back to Scrapy log-error count.
            stats_errors = int(self.crawler.stats.get_value("log_count/ERROR", 0) or 0)
            errors = max(errors, stats_errors)

        now_mono = time.monotonic()
        rate_t0 = float(getattr(spider, "rit_rate_t0", now_mono) or now_mono)
        elapsed_rate = max(0.001, now_mono - rate_t0)
        rate_pages = int(getattr(spider, "rit_rate_pages", 0) or 0)
        rate_bytes = int(getattr(spider, "rit_rate_bytes", 0) or 0)
        pages_per_sec = rate_pages / elapsed_rate
        mb_per_sec = (rate_bytes / (1024 * 1024)) / elapsed_rate
        # Reset interval window for next tick.
        spider.rit_rate_pages = 0
        spider.rit_rate_bytes = 0
        spider.rit_rate_t0 = now_mono

        gb = text_bytes / _GB
        if self._target_bytes > 0:
            pct = 100.0 * text_bytes / self._target_bytes
            remaining = max(0, self._target_bytes - text_bytes)
            # ETA from overall average since spider open.
            overall_elapsed = max(0.001, now_mono - self._t0)
            overall_bps = text_bytes / overall_elapsed if text_bytes > 0 else 0.0
            if overall_bps > 0 and remaining > 0:
                eta = _fmt_duration(remaining / overall_bps)
            elif remaining <= 0:
                eta = "0s"
            else:
                eta = "?"
            target_gb = self._target_bytes / _GB
            target_part = f"{gb:.2f}/{target_gb:.1f}GB ({pct:.1f}%) eta={eta}"
        else:
            target_part = f"{gb:.2f}GB (no target)"

        pending = _pending_requests(self.crawler)
        now = time.strftime("%H:%M:%S")
        print(
            f"PROGRESS {now} saved={saved_run}/{saved_total} "
            f"text={target_part} "
            f"rate={pages_per_sec:.2f}p/s {mb_per_sec:.2f}MB/s "
            f"pending_requests={pending} errors={errors}",
            flush=True,
        )


def _pending_requests(crawler) -> int:  # noqa: ANN001
    """Scheduler queue + engine in-progress (Scrapy 2.19: engine._slot)."""
    try:
        engine = crawler.engine
        n = 0
        scheduler = getattr(engine, "scheduler", None)
        if scheduler is not None:
            n += len(scheduler)
        slot = getattr(engine, "_slot", None)
        if slot is not None:
            n += len(slot.inprogress)
        return int(n)
    except Exception:
        return 0


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
