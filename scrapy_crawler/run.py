"""Entry point: python -m scrapy_crawler.run

Ctrl+C: Scrapy graceful stop on first interrupt (JOBDIR persists the queue);
second interrupt forces exit. Rerun the same command to resume.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from crawler.main import _ensure_partition_logging, _load_seeds
from crawler.policies import CrawlPolicies
from crawler.progress import free_disk_bytes
from crawler.storage import Repository

_GB = 1024 ** 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrapy RIT videogames crawler (library implementation)",
    )
    parser.add_argument("--seeds", default="seeds/seeds.csv")
    parser.add_argument("--db", default="repository/crawl.db")
    parser.add_argument("--text-root", default="repository")
    parser.add_argument("--target-gb", type=float, default=10.0)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--max-pages-per-domain", type=int, default=100000)
    parser.add_argument(
        "--all-seeds",
        action="store_true",
        help="Ignore crawler column partition and load every seed row",
    )
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument(
        "--log",
        default="repository/logs/scrapy/crawl.log",
        help="Scrapy LOG_FILE path (no rotation; acceptable for course runs)",
    )
    parser.add_argument(
        "--jobdir",
        default="repository/scrapy_job",
        help="Scrapy JOBDIR for pause/resume; rerun same command to resume",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete JOBDIR only after y/N confirm (pages/text kept)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also echo Scrapy log lines to stdout",
    )
    parser.add_argument(
        "--progress-every",
        type=float,
        default=20.0,
        help="Progress print interval in seconds",
    )
    return parser.parse_args(argv)


def _print_banner(
    *,
    seeds_count: int,
    hosts_count: int,
    concurrency: int,
    target_gb: float,
    db_path: str,
    text_root: str,
    jobdir: str,
    resuming: bool,
    free_disk_gb: float,
) -> None:
    mode = "resuming" if resuming else "fresh"
    print(
        f"SCRAPY seeds={seeds_count} hosts={hosts_count} "
        f"concurrency={concurrency} target={target_gb:g}GB "
        f"db={db_path} text_root={text_root} jobdir={jobdir} "
        f"mode={mode} free_disk={free_disk_gb:.1f}GB",
        flush=True,
    )
    print(
        "Tip: Ctrl+C once for graceful stop (JOBDIR keeps queue); "
        "rerun the same command to resume.",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _ensure_partition_logging()
    seed_rows = _load_seeds(
        args.seeds,
        for_crawler="scrapy",
        all_seeds=args.all_seeds,
    )
    if not seed_rows:
        print(f"No seeds loaded from {args.seeds}", file=sys.stderr)
        return 1

    host_rules = CrawlPolicies.build_host_rules(seed_rows)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    Path(args.text_root).mkdir(parents=True, exist_ok=True)
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)

    jobdir = Path(args.jobdir)
    if args.fresh:
        answer = input(
            f"Delete JOBDIR only ({jobdir})? Pages/text are kept. [y/N] "
        ).strip().lower()
        if answer != "y":
            print("Aborted --fresh", flush=True)
            return 1
        if jobdir.exists():
            shutil.rmtree(jobdir)
            print(f"Deleted jobdir {jobdir}", flush=True)
        else:
            print(f"No jobdir at {jobdir}", flush=True)

    jobdir.mkdir(parents=True, exist_ok=True)
    # Resume if JOBDIR already has dupefilter / queue state.
    seen = jobdir / "requests.seen"
    queue_dir = jobdir / "requests.queue"
    resuming = seen.exists() or (
        queue_dir.is_dir() and any(queue_dir.iterdir())
    )

    free = free_disk_bytes(args.text_root)
    target_bytes = int(args.target_gb * _GB) if args.target_gb > 0 else 0
    if target_bytes > 0 and free < target_bytes + 5 * _GB:
        print(
            f"WARNING: free disk {free / _GB:.1f}GB may be low for "
            f"target {args.target_gb:g}GB (+5GB margin)",
            flush=True,
        )

    _print_banner(
        seeds_count=len(seed_rows),
        hosts_count=len(host_rules),
        concurrency=args.concurrency,
        target_gb=args.target_gb,
        db_path=args.db,
        text_root=args.text_root,
        jobdir=str(jobdir),
        resuming=resuming,
        free_disk_gb=free / _GB,
    )

    settings = get_project_settings()
    settings.set("RIT_SEEDS", args.seeds, priority="cmdline")
    settings.set("RIT_DB", args.db, priority="cmdline")
    settings.set("RIT_TEXT_ROOT", args.text_root, priority="cmdline")
    settings.set("RIT_TARGET_GB", args.target_gb, priority="cmdline")
    settings.set(
        "RIT_MAX_PAGES_PER_DOMAIN", args.max_pages_per_domain, priority="cmdline"
    )
    settings.set("RIT_PROGRESS_EVERY", args.progress_every, priority="cmdline")
    settings.set("RIT_ALL_SEEDS", args.all_seeds, priority="cmdline")
    settings.set("DEPTH_LIMIT", args.max_depth, priority="cmdline")
    settings.set("CONCURRENT_REQUESTS", args.concurrency, priority="cmdline")
    settings.set("JOBDIR", str(jobdir), priority="cmdline")
    # Scrapy LOG_FILE has no rotation; acceptable for course-length runs.
    settings.set("LOG_FILE", str(Path(args.log)), priority="cmdline")
    settings.set("LOG_FILE_APPEND", True, priority="cmdline")
    settings.set("LOG_LEVEL", "INFO", priority="cmdline")
    # With LOG_FILE set, Scrapy installs only a FileHandler (stdout stays quiet
    # for Scrapy logs). Progress lines use print(..., flush=True).
    if args.verbose:
        # Keep file logging; also echo to stdout via an extra handler below.
        pass

    t0 = time.monotonic()
    process = CrawlerProcess(settings)
    if args.verbose:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.INFO)
        stream.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        logging.root.addHandler(stream)

    # Keep a strong ref: process.crawlers discards the crawler when it finishes.
    crawler = process.create_crawler("videogames")
    process.crawl(crawler)
    process.start()  # blocks until finished

    elapsed = time.monotonic() - t0
    finish_reason = "finished"
    pages_saved = 0
    pages_skipped = 0
    errors = 0
    text_bytes = 0

    if crawler.stats is not None:
        finish_reason = str(
            crawler.stats.get_value("finish_reason", finish_reason) or finish_reason
        )
    spider = crawler.spider
    if spider is not None:
        pages_saved = int(getattr(spider, "rit_pages_saved_run", 0) or 0)
        pages_skipped = int(getattr(spider, "rit_pages_skipped", 0) or 0)
        errors = int(getattr(spider, "rit_errors", 0) or 0)
        text_bytes = int(getattr(spider, "rit_text_bytes", 0) or 0)

    if text_bytes == 0:
        try:
            repo = Repository(args.db, args.text_root)
            try:
                text_bytes = repo.text_bytes_total("scrapy")
            finally:
                repo.close()
        except Exception:
            pass

    stop_reason = finish_reason
    print(
        f"SUMMARY pages_saved={pages_saved} pages_skipped={pages_skipped} "
        f"errors={errors} elapsed_s={elapsed:.2f} "
        f"text_gb={text_bytes / _GB:.3f} "
        f"stop_reason={stop_reason}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
