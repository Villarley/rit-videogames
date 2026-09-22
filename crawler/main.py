from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.parse import urlparse

from crawler.logger import setup_logger
from crawler.policies import CrawlPolicies
from crawler.storage import Repository
from crawler.threaded_crawler import ThreadedCrawler


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Custom multi-threaded RIT videogames crawler",
    )
    parser.add_argument("--seeds", required=True, help="Path to seeds.csv")
    parser.add_argument("--db", default="repository/crawl.db")
    parser.add_argument("--text-root", default="repository/")
    parser.add_argument("--log", default="repository/crawl.log")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-pages-per-domain", type=int, default=200)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument(
        "--allowed-domains",
        default=None,
        help=(
            "Comma-separated allowed domains. "
            "If omitted, defaults to the hostnames of the seed URLs."
        ),
    )
    parser.add_argument("--crawler-source", default="custom")
    return parser.parse_args()


def _load_seeds(seeds_path: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    with open(seeds_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            seed_id = (row.get("seed_id") or "").strip()
            url = (row.get("url") or "").strip()
            subtema = (row.get("subtema") or "").strip()
            if seed_id and url:
                rows.append((seed_id, url, subtema))
    return rows


def main() -> None:
    args = _parse_args()
    seed_rows = _load_seeds(args.seeds)

    if args.allowed_domains:
        allowed_domains = {
            d.strip().lower()
            for d in args.allowed_domains.split(",")
            if d.strip()
        }
    else:
        # Keep the crawl on the curated seed hosts unless explicitly widened.
        allowed_domains = {
            urlparse(url).netloc.lower()
            for _, url, _ in seed_rows
            if urlparse(url).netloc
        }

    policies = CrawlPolicies(
        allowed_domains=allowed_domains,
        max_depth=args.max_depth,
        max_pages_per_domain=args.max_pages_per_domain,
    )

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    Path(args.text_root).mkdir(parents=True, exist_ok=True)

    logger = setup_logger(args.log)
    repository = Repository(args.db, args.text_root)

    for seed_id, url, subtema in seed_rows:
        repository.upsert_seed(seed_id, url, subtema=subtema)

    seeds = [(seed_id, url) for seed_id, url, _ in seed_rows]
    logger.info(
        "CRAWL_START seeds=%d max_depth=%d max_pages_per_domain=%d "
        "threads=%d allowed_domains=%s",
        len(seeds),
        policies.max_depth,
        policies.max_pages_per_domain,
        args.threads,
        ",".join(sorted(allowed_domains)),
    )

    crawler = ThreadedCrawler(
        policies=policies,
        repository=repository,
        logger=logger,
        num_threads=args.threads,
        crawler_source=args.crawler_source,
    )
    try:
        crawler.crawl(seeds)
    finally:
        repository.close()


if __name__ == "__main__":
    main()
