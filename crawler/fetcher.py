from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from crawler.policies import CrawlPolicies


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int | None
    content_type: str | None
    text_body: str | None
    error: str | None


class Fetcher:
    def __init__(
        self,
        session: requests.Session,
        policies: CrawlPolicies,
        user_agent: str = (
            "RIT-TEC-VideogamesCrawler/1.0 (+contact: student project)"
        ),
        timeout: float = 10.0,
    ) -> None:
        self._session = session
        self._policies = policies
        self._user_agent = user_agent
        self._timeout = timeout

    def fetch(self, url: str) -> FetchResult:
        headers = {"User-Agent": self._user_agent}
        try:
            response = self._session.get(
                url,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=True,
            )
        except requests.Timeout as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"timeout: {exc}",
            )
        except requests.TooManyRedirects as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"too many redirects: {exc}",
            )
        except requests.ConnectionError as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"connection error: {exc}",
            )
        except requests.RequestException as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"request error: {exc}",
            )

        content_type = response.headers.get("Content-Type", "")
        content_type_main = content_type.split(";")[0].strip().lower()
        allowed = any(
            content_type_main.startswith(ct.lower())
            for ct in self._policies.allowed_content_types
        )

        text_body: str | None = None
        if allowed:
            text_body = response.text

        return FetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            content_type=content_type,
            text_body=text_body,
            error=None,
        )


class RobotsCache:
    def __init__(self, session: requests.Session, timeout: float = 10.0) -> None:
        self._session = session
        self._timeout = timeout
        self._lock = threading.Lock()
        self._parsers: dict[str, RobotFileParser] = {}
        self._crawl_delays: dict[str, float | None] = {}

    def _domain_key(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc.lower()
        return scheme, netloc

    def _ensure_loaded(self, url: str) -> RobotFileParser:
        scheme, netloc = self._domain_key(url)
        key = f"{scheme}://{netloc}"

        with self._lock:
            cached = self._parsers.get(key)
            if cached is not None:
                return cached

        robots_url = f"{key}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        crawl_delay: float | None = None

        try:
            response = self._session.get(robots_url, timeout=self._timeout)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                crawl_delay = self._extract_crawl_delay(response.text)
            else:
                # Unreachable / missing robots.txt → allow-all
                parser.parse([])
        except requests.RequestException:
            parser.parse([])

        with self._lock:
            if key not in self._parsers:
                self._parsers[key] = parser
                self._crawl_delays[key] = crawl_delay
            return self._parsers[key]

    @staticmethod
    def _extract_crawl_delay(robots_text: str) -> float | None:
        """Parse Crawl-delay from robots.txt (stdlib RobotFileParser lacks this)."""
        for line in robots_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                continue
            directive, _, value = stripped.partition(":")
            if directive.strip().lower() == "crawl-delay":
                try:
                    return float(value.strip())
                except ValueError:
                    return None
        return None

    def can_fetch(self, url: str, user_agent: str) -> bool:
        parser = self._ensure_loaded(url)
        return parser.can_fetch(user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        self._ensure_loaded(url)
        scheme, netloc = self._domain_key(url)
        key = f"{scheme}://{netloc}"
        with self._lock:
            return self._crawl_delays.get(key)


class RateLimiter:
    def __init__(self, policies: CrawlPolicies) -> None:
        self._policies = policies
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._domain_locks: dict[str, threading.Lock] = {}

    def _get_domain_lock(self, domain: str) -> threading.Lock:
        with self._lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = threading.Lock()
            return self._domain_locks[domain]

    def wait_if_needed(
        self,
        domain: str,
        override_interval: float | None = None,
    ) -> None:
        interval = (
            override_interval
            if override_interval is not None
            else self._policies.min_request_interval_seconds
        )
        domain_lock = self._get_domain_lock(domain)
        with domain_lock:
            now = time.monotonic()
            last = self._last_request.get(domain)
            if last is not None:
                elapsed = now - last
                remaining = interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request[domain] = time.monotonic()
