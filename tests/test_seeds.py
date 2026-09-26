from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEEDS_PATH = PROJECT_ROOT / "seeds" / "seeds.csv"

ALLOWED_SCOPE = {"domain", "prefix", "topical"}
ALLOWED_CRAWLER = {"custom", "scrapy"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def test_seeds_csv_loads_and_validates() -> None:
    assert SEEDS_PATH.is_file(), "seeds/seeds.csv is missing"

    with SEEDS_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 10

    hosts_by_crawler: dict[str, set[str]] = {"custom": set(), "scrapy": set()}

    for row in rows:
        scope = (row.get("scope_mode") or "").strip().lower()
        crawler = (row.get("crawler") or "").strip().lower()
        url = (row.get("url") or "").strip()

        assert scope in ALLOWED_SCOPE, f"bad scope_mode: {scope!r} in {row}"
        assert crawler in ALLOWED_CRAWLER, f"bad crawler: {crawler!r} in {row}"

        parsed = urlparse(url)
        assert parsed.scheme in ("http", "https"), f"non-http url: {url}"

        host = _host(url)
        assert host
        hosts_by_crawler[crawler].add(host)

    overlap = hosts_by_crawler["custom"] & hosts_by_crawler["scrapy"]
    assert not overlap, f"hosts assigned to both crawlers: {sorted(overlap)}"
