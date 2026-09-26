from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from crawler.logger import setup_logger
from crawler.policies import CrawlPolicies
from crawler.progress import free_disk_bytes, print_startup_banner
from crawler.storage import Repository
from crawler.threaded_crawler import SeedSpec, ThreadedCrawler

_GB = 1024 ** 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Custom multi-threaded RIT videogames crawler",
    )
    parser.add_argument("--seeds", default="seeds/seeds.csv")
    parser.add_argument("--db", default="repository/crawl.db")
    parser.add_argument("--text-root", default="repository")
    parser.add_argument("--log", default="repository/logs/custom/crawl.log")
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--max-pages-per-domain", type=int, default=100000)
    parser.add_argument(
        "--all-seeds",
        action="store_true",
        help="Ignore crawler column partition and load every seed row",
    )
    parser.add_argument("--min-interval", type=float, default=1.0)
    parser.add_argument(
        "--target-gb",
        type=float,
        default=20.0,
        help="Stop after this many GB of plain text (0 = no target)",
    )
    parser.add_argument("--crawler-source", default="custom")
    parser.add_argument(
        "--revisit",
        action="store_true",
        help="Re-queue stale done URLs on start (uses --revisit-days)",
    )
    parser.add_argument("--revisit-days", type=int, default=7)
    parser.add_argument("--progress-every", type=float, default=20.0)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print INFO bitacora lines to stdout",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Clear frontier rows (asks y/N). Never deletes pages/text.",
    )
    parser.add_argument(
        "--allowed-domains",
        default=None,
        help="Optional comma-separated host allowlist override",
    )
    return parser.parse_args(argv)


def _seed_host(url: str) -> str:
    return (urlparse(url).hostname or urlparse(url).netloc or "").lower()


def _read_seed_rows(seeds_path: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(seeds_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            seed_id = (row.get("seed_id") or "").strip()
            url = (row.get("url") or "").strip()
            if not seed_id or not url:
                continue
            rows.append(
                {
                    "seed_id": seed_id,
                    "url": url,
                    "subtema": (row.get("subtema") or "").strip(),
                    "scope_mode": (row.get("scope_mode") or "topical").strip(),
                    "idioma": (row.get("idioma") or "").strip(),
                    "crawler": (row.get("crawler") or "").strip().lower(),
                }
            )
    return rows


def _warn_host_crawler_conflicts(rows: list[dict[str, str]]) -> None:
    host_crawlers: dict[str, set[str]] = {}
    for row in rows:
        crawler = row.get("crawler") or ""
        if not crawler:
            continue
        host = _seed_host(row["url"])
        if not host:
            continue
        host_crawlers.setdefault(host, set()).add(crawler)
    log = logging.getLogger(__name__)
    for host, crawlers in sorted(host_crawlers.items()):
        if len(crawlers) > 1:
            log.warning(
                "SEED_HOST_PARTITION host=%s crawlers=%s",
                host,
                sorted(crawlers),
            )


def _load_seeds(
    seeds_path: str,
    *,
    for_crawler: str | None = None,
    all_seeds: bool = False,
) -> list[dict[str, str]]:
    # Hosts partitioned between crawlers to avoid duplicated downloads (Mercator/UbiCrawler-style); same information need and policies.
    all_rows = _read_seed_rows(seeds_path)
    _warn_host_crawler_conflicts(all_rows)
    if all_seeds or for_crawler is None:
        return all_rows
    allowed: list[dict[str, str]] = []
    for row in all_rows:
        crawler = row.get("crawler") or ""
        if for_crawler == "custom":
            if crawler in ("", "custom"):
                allowed.append(row)
        elif for_crawler == "scrapy":
            if crawler in ("", "scrapy"):
                allowed.append(row)
        else:
            allowed.append(row)
    return allowed


def _ensure_partition_logging() -> None:
    log = logging.getLogger(__name__)
    if log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _ensure_partition_logging()
    seed_rows = _load_seeds(
        args.seeds,
        for_crawler="custom",
        all_seeds=args.all_seeds,
    )
    if not seed_rows:
        print(f"No seeds loaded from {args.seeds}", file=sys.stderr)
        return 1

    host_rules = CrawlPolicies.build_host_rules(seed_rows)
    if args.allowed_domains:
        allowed = {
            d.strip().lower()
            for d in args.allowed_domains.split(",")
            if d.strip()
        }
        host_rules = {h: r for h, r in host_rules.items() if h in allowed}

    policies = CrawlPolicies(
        host_rules=host_rules,
        max_depth=args.max_depth,
        max_pages_per_domain=args.max_pages_per_domain,
        min_request_interval_seconds=args.min_interval,
        revisit_after_days=args.revisit_days,
    )

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    Path(args.text_root).mkdir(parents=True, exist_ok=True)
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)

    target_bytes = int(args.target_gb * _GB) if args.target_gb > 0 else 0
    free = free_disk_bytes(args.text_root)
    if target_bytes > 0 and free < target_bytes + 5 * _GB:
        print(
            f"WARNING: free disk {free / _GB:.1f}GB may be low for "
            f"target {args.target_gb:g}GB (+5GB margin)",
            flush=True,
        )

    logger = setup_logger(args.log, verbose=args.verbose)
    repository = Repository(args.db, args.text_root)

    if args.fresh:
        answer = input(
            "Delete ALL frontier rows? Pages/text are kept. [y/N] "
        ).strip().lower()
        if answer == "y":
            n = repository.clear_frontier()
            print(f"Cleared {n} frontier rows", flush=True)
            logger.info("FRONTIER_CLEARED count=%d", n)
        else:
            print("Aborted --fresh", flush=True)
            repository.close()
            return 1

    for row in seed_rows:
        repository.upsert_seed(
            row["seed_id"],
            row["url"],
            subtema=row["subtema"],
            scope_mode=row["scope_mode"],
            idioma=row["idioma"],
        )

    if args.revisit:
        n = repository.revisit_stale(args.revisit_days)
        logger.info("REVISIT_STALE days=%d requeued=%d", args.revisit_days, n)

    pending = repository.pending_count()
    resuming = pending > 0

    print_startup_banner(
        seeds_count=len(seed_rows),
        hosts_count=len(host_rules),
        threads=args.threads,
        target_gb=args.target_gb,
        db_path=args.db,
        text_root=args.text_root,
        resuming=resuming,
        free_disk_gb=free / _GB,
    )

    logger.info(
        "CRAWL_START seeds=%d hosts=%d max_depth=%d max_pages_per_domain=%d "
        "threads=%d target_gb=%s min_interval=%.2f resuming=%s",
        len(seed_rows),
        len(host_rules),
        policies.max_depth,
        policies.max_pages_per_domain,
        args.threads,
        args.target_gb,
        policies.min_request_interval_seconds,
        resuming,
    )

    seeds = [
        SeedSpec(
            seed_id=row["seed_id"],
            url=row["url"],
            scope_mode=row["scope_mode"],
            idioma=row["idioma"],
            subtema=row["subtema"],
        )
        for row in seed_rows
    ]

    crawler = ThreadedCrawler(
        policies=policies,
        repository=repository,
        logger=logger,
        num_threads=args.threads,
        crawler_source=args.crawler_source,
        target_bytes=target_bytes,
        progress_every=args.progress_every,
        text_root=args.text_root,
    )

    try:
        try:
            crawler.crawl(seeds)
        except KeyboardInterrupt:
            logger.warning("KEYBOARD_INTERRUPT graceful stop")
            print("Interrupted - frontier left resumable", flush=True)
    finally:
        repository.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
