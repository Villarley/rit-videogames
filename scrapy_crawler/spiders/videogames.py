"""Videogames broad crawler spider (Scrapy / library implementation).

Division of labour (course comparison):
  Scrapy owns scheduling/frontier, dupefilter, robots.txt, politeness
  (DOWNLOAD_DELAY, CONCURRENT_REQUESTS_PER_DOMAIN, AutoThrottle), retries,
  depth limit, pause/resume (JOBDIR), size cap (DOWNLOAD_MAXSIZE), and link
  extraction (LinkExtractor).

  We reuse from crawler/ only what defines the SAME information need and the
  SAME repository format: seeds.csv + CrawlPolicies (normalize_url, is_denied,
  link_in_scope, is_topical/topical_score, build_host_rules / scope modes),
  crawler.extractor.extract for identical text cleaning, and Repository for
  storage with crawler_source='scrapy'.
"""

from __future__ import annotations

from urllib.parse import urlparse
from w3lib.url import canonicalize_url

import scrapy
from scrapy.http import TextResponse
from scrapy.linkextractors import LinkExtractor
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from crawler.extractor import extract
from crawler.main import _load_seeds
from crawler.policies import CrawlPolicies
from crawler.storage import Repository
from scrapy_crawler.items import PageItem
from scrapy_crawler.middlewares import AlreadyStored
from scrapy_crawler.recovery import recovery_log_context


def _domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


class VideogamesSpider(scrapy.Spider):
    name = "videogames"

    # Do NOT set allowed_domains. OffsiteMiddleware (2.19) snapshots
    # allowed_domains at spider_opened; seed redirects (www.ign.com ->
    # latam.ign.com) would be blocked. Scope is enforced via link_in_scope,
    # which already requires an allowed host in CrawlPolicies.host_rules.

    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.policies: CrawlPolicies | None = None
        self._link_extractor = LinkExtractor()
        self._scheduled_by_domain: dict[str, int] = {}
        self._scheduled_urls: set[str] = set()
        self._max_pages_per_domain = 100000
        self.rit_errors = 0
        self.rit_text_bytes = 0
        self.rit_pages_saved_run = 0
        self.rit_pages_saved_total = 0
        self.rit_pages_skipped = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):  # noqa: ANN002, ANN003
        spider = super().from_crawler(crawler, *args, **kwargs)
        return spider

    async def start(self):
        # Scrapy 2.13+: start() replaces start_requests().
        settings = self.settings
        tracker = getattr(self, "rit_project_total", None)
        target = settings.getfloat("RIT_TARGET_GB", 10.0) * 1024**3
        if tracker and target > 0 and tracker.snapshot()[0] >= target:
            from scrapy.exceptions import CloseSpider
            raise CloseSpider("target_reached")
        seeds_path = settings.get("RIT_SEEDS", "seeds/seeds.csv")
        db_path = settings.get("RIT_DB", "repository/crawl.db")
        text_root = settings.get("RIT_TEXT_ROOT", "repository")
        max_depth = settings.getint("DEPTH_LIMIT", 6)
        self._max_pages_per_domain = settings.getint(
            "RIT_MAX_PAGES_PER_DOMAIN", 100000
        )
        all_seeds = settings.getbool("RIT_ALL_SEEDS", False)

        # Hosts partitioned between crawlers to avoid duplicated downloads (Mercator/UbiCrawler-style); same information need and policies.
        seed_rows = _load_seeds(
            seeds_path,
            for_crawler="scrapy",
            all_seeds=all_seeds,
        )
        if not seed_rows:
            self.logger.error("No seeds loaded from %s", seeds_path)
            return

        host_rules = CrawlPolicies.build_host_rules(seed_rows)
        self.logger.info(
            "SEEDS_OWNED count=%d hosts=%d all_seeds=%s",
            len(seed_rows),
            len(host_rules),
            all_seeds,
        )
        self.policies = CrawlPolicies(
            host_rules=host_rules,
            max_depth=max_depth,
            max_pages_per_domain=self._max_pages_per_domain,
        )

        repo = Repository(db_path, text_root)
        try:
            for row in seed_rows:
                repo.upsert_seed(
                    row["seed_id"],
                    row["url"],
                    subtema=row["subtema"],
                    scope_mode=row["scope_mode"],
                    idioma=row["idioma"],
                )
            saved_pages = repo.saved_pages("scrapy")
        finally:
            repo.close()

        # SpiderState persists admissions with JOBDIR. Count canonical URLs,
        # not repeated appearances in navigation menus or links at max depth.
        state = getattr(self, "state", {})
        self._scheduled_urls = state.setdefault("rit_admitted_urls_v1", set())
        for page in saved_pages:
            self._scheduled_urls.add(canonicalize_url(page["url"]))
        self._scheduled_by_domain = {}
        for url in self._scheduled_urls:
            domain = _domain_of(url)
            self._scheduled_by_domain[domain] = self._scheduled_by_domain.get(domain, 0) + 1

        # Recover already observed seed redirects before queued requests run.
        seeds_by_id = {row["seed_id"]: row for row in seed_rows}
        for page in saved_pages:
            seed = seeds_by_id.get(page["seed_id"])
            if seed and page["depth"] == 0:
                self._maybe_register_redirect_host(seed["url"], page["final_url"] or page["url"])

        recover = settings.getbool("RIT_RECOVER_LINKS", False)

        for row in seed_rows:
            normalized = self.policies.normalize_url(row["url"])
            if not normalized:
                self.logger.info("SKIP_BAD_SEED url=%s", row["url"])
                continue
            denied = self.policies.is_denied(normalized)
            if denied:
                self.logger.info(
                    "SKIP_DENIED_SEED url=%s reason=%s", normalized, denied
                )
                continue
            rule = self.policies.host_rule_for(normalized)
            scope_rule = rule.mode if rule else "seed"
            domain = _domain_of(normalized)
            if (domain and canonicalize_url(normalized) not in self._scheduled_urls
                    and not self._try_schedule(domain, normalized)):
                continue
            yield scrapy.Request(
                normalized,
                callback=self.parse,
                errback=self.errback,
                meta={
                    "seed_id": row["seed_id"],
                    "parent_url": None,
                    "anchor_text": "",
                    "parent_topical": True,
                    "scope_rule": scope_rule,
                    "rit_recover_links": recover,
                },
            )

        if recover:
            omitted, thin_depths = recovery_log_context(
                settings.get("RIT_RECOVERY_LOG", "repository/logs/scrapy/crawl.log"))
            # Thin/category pages were not stored, but may have useful links.
            candidates = {page["url"]: page for page in saved_pages}
            seed_by_host = {_domain_of(row["url"]): row["seed_id"] for row in seed_rows}
            for page in saved_pages:
                if page["depth"] == 0:
                    seed_by_host[_domain_of(page["url"])] = page["seed_id"]
            for url, depth in thin_depths.items():
                rule = self.policies.host_rule_for(url)
                if rule and url in omitted and url not in candidates:
                    candidates[url] = {"url": url, "depth": depth, "parent_url": None,
                        "seed_id": seed_by_host.get(_domain_of(url), ""), "scope_rule": rule.mode}
            count = 0
            for page in sorted(candidates.values(), key=lambda p: p["depth"] if p["depth"] is not None else max_depth):
                depth = page["depth"]
                url = page["url"]
                if url not in omitted or depth is None or not 0 < depth < max_depth:
                    continue
                rule = self.policies.host_rule_for(url)
                if rule is None or self.policies.is_denied(url):
                    continue
                # Prefix scope still applies; topical pages were admitted in
                # the original run, so their original parent context is kept.
                if rule.mode == "prefix" and not self.policies.link_in_scope(url, "", True)[0]:
                    continue
                if (canonicalize_url(url) not in self._scheduled_urls
                        and not self._try_schedule(_domain_of(url), url)):
                    continue
                count += 1
                yield scrapy.Request(url, callback=self.parse, errback=self.errback, meta={
                    "seed_id": page["seed_id"], "parent_url": page["parent_url"],
                    "scope_rule": page["scope_rule"], "depth": depth,
                    "rit_recover_links": True,
                })
            self.logger.info("RECOVERY_CANDIDATES pages=%d (already scheduled repairs are deduplicated)", count)

    def parse(self, response):
        assert self.policies is not None
        depth = int(response.meta.get("depth", 0))
        seed_id = response.meta.get("seed_id") or ""
        parent_url = response.meta.get("parent_url")
        scope_rule = response.meta.get("scope_rule") or ""
        request_url = response.request.url if response.request else response.url
        # Account for requests restored from a pre-fix queue once, too.
        key = canonicalize_url(request_url)
        if key not in self._scheduled_urls:
            self._scheduled_urls.add(key)
            domain = _domain_of(key)
            self._scheduled_by_domain[domain] = self._scheduled_by_domain.get(domain, 0) + 1

        # Seed redirect to a new host: inherit the same host rule.
        if depth == 0:
            self._maybe_register_redirect_host(request_url, response.url)

        if not isinstance(response, TextResponse):
            self.logger.info(
                "SKIP_CONTENT_TYPE url=%s depth=%d content_type=%s",
                request_url,
                depth,
                response.headers.get(b"Content-Type", b"").decode(
                    "latin-1", errors="replace"
                ),
            )
            self.rit_pages_skipped += 1
            return

        extracted = extract(response.text, response.url, collect_links=False)
        words = len(extracted.text.split())
        hits, density = self.policies.topical_score(extracted.text)
        host_rule = self.policies.host_rule_for(request_url)
        page_is_topical = self.policies.is_topical_score(hits, density)

        save_page = True
        if words < self.policies.min_words:
            self.logger.info(
                "SKIP_THIN_CONTENT url=%s depth=%d words=%d",
                request_url,
                depth,
                words,
            )
            self.rit_pages_skipped += 1
            save_page = False

        if (
            save_page
            and host_rule is not None
            and host_rule.mode == "topical"
            and depth > 0
            and not page_is_topical
        ):
            self.logger.info(
                "SKIP_OFF_TOPIC url=%s depth=%d hits=%d density=%.2f",
                request_url,
                depth,
                hits,
                density,
            )
            self.rit_pages_skipped += 1
            save_page = False

        parent_topical_for_children = bool(
            host_rule is not None
            and host_rule.mode == "topical"
            and page_is_topical
        )

        links = self._link_extractor.extract_links(response)
        outlinks_total = len(links)
        in_scope_count = 0
        enqueued = 0
        seen_children: set[str] = set()

        for link in links:
            normalized = self.policies.normalize_url(link.url)
            if not normalized or normalized in seen_children:
                continue
            seen_children.add(normalized)
            ok, rule_name = self.policies.link_in_scope(
                normalized,
                link.text or "",
                parent_topical_for_children,
            )
            if not ok:
                continue
            in_scope_count += 1
            child_domain = _domain_of(normalized)
            if not child_domain:
                continue
            if depth >= self.policies.max_depth:
                continue
            if not self._try_schedule(child_domain, normalized):
                continue
            enqueued += 1
            yield response.follow(
                normalized,
                callback=self.parse,
                errback=self.errback,
                meta={
                    "seed_id": seed_id,
                    "parent_url": request_url,
                    "anchor_text": (link.text or "")[:500],
                    "parent_topical": parent_topical_for_children,
                    "scope_rule": rule_name,
                },
            )

        self.logger.info(
            "LINKS url=%s total=%d in_scope=%d enqueued=%d",
            request_url,
            outlinks_total,
            in_scope_count,
            enqueued,
        )

        if not save_page:
            return

        latency = response.meta.get("download_latency")
        fetch_ms = int(float(latency) * 1000) if latency is not None else 0
        content_type = response.headers.get(b"Content-Type", b"").decode(
            "latin-1", errors="replace"
        )
        text_bytes = len(extracted.text.encode("utf-8"))

        yield PageItem(
            url=request_url,
            final_url=response.url,
            seed_id=seed_id,
            depth=depth,
            parent_url=parent_url,
            http_status=response.status,
            content_type=content_type,
            title=extracted.title,
            text=extracted.text,
            language=extracted.language,
            text_bytes=text_bytes,
            outlinks_total=outlinks_total,
            outlinks_in_scope=in_scope_count,
            topical_hits=hits,
            topical_density=density,
            scope_rule=scope_rule,
            truncated=0,
            fetch_ms=fetch_ms,
        )

    def errback(self, failure: Failure):
        if failure.check(AlreadyStored):
            return
        request = failure.request
        url = request.url if request is not None else "?"
        if failure.check(HttpError):
            response = failure.value.response
            status = response.status if response is not None else "?"
            self.logger.info("FETCH_HTTP status=%s url=%s", status, url)
        else:
            self.logger.info(
                "FETCH_ERROR url=%s error=%s",
                url,
                failure.getErrorMessage(),
            )
        self.rit_errors += 1

    def _try_schedule(self, domain: str, url: str) -> bool:
        key = canonicalize_url(url)
        if key in self._scheduled_urls:
            return False
        count = self._scheduled_by_domain.get(domain, 0)
        if count >= self._max_pages_per_domain:
            return False
        self._scheduled_by_domain[domain] = count + 1
        self._scheduled_urls.add(key)
        return True

    def _maybe_register_redirect_host(
        self, seed_url: str, final_url: str
    ) -> None:
        assert self.policies is not None
        seed_host = _domain_of(seed_url)
        final_host = _domain_of(final_url)
        if not final_host or final_host == seed_host:
            return
        existing = self.policies.host_rule_for(seed_url)
        if existing is None:
            return
        if self.policies.host_rule_for(final_url) is not None:
            return
        self.policies.register_host(final_host, existing)
        self.logger.info(
            "HOST_REGISTERED_REDIRECT from=%s to=%s mode=%s",
            seed_host,
            final_host,
            existing.mode,
        )
