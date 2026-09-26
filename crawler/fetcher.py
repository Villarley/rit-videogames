from __future__ import annotations

import email.utils
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from crawler import USER_AGENT
from crawler.policies import CrawlPolicies

_MAX_CRAWL_DELAY = 30.0


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int | None
    content_type: str | None
    text_body: str | None
    error: str | None
    truncated: bool = False
    retry_after_seconds: float | None = None
    elapsed_ms: int = 0
    bytes_downloaded: int = 0


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when is None:
            return None
        delay = when.timestamp() - time.time()
        return max(0.0, delay)
    except (TypeError, ValueError, OverflowError):
        return None


class Fetcher:
    def __init__(
        self,
        policies: CrawlPolicies,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._policies = policies
        self._user_agent = user_agent
        self._local = threading.local()

    def _get_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_maxsize=4, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def fetch(self, url: str) -> FetchResult:
        headers = {"User-Agent": self._user_agent}
        timeout = self._policies.request_timeout
        started = time.monotonic()
        try:
            response = self._get_session().get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
        except requests.Timeout as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"timeout: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except requests.TooManyRedirects as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"too many redirects: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except requests.ConnectionError as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"connection error: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except requests.RequestException as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=None,
                content_type=None,
                text_body=None,
                error=f"request error: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        content_type = response.headers.get("Content-Type", "")
        content_type_main = content_type.split(";")[0].strip().lower()
        allowed = any(
            content_type_main.startswith(ct.lower())
            for ct in self._policies.allowed_content_types
        )
        retry_after = None
        if response.status_code in (429, 503):
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))

        status = response.status_code
        final_url = str(response.url)
        elapsed_ms = 0
        bytes_downloaded = 0
        truncated = False
        text_body: str | None = None

        try:
            if status is None or status < 200 or status >= 300:
                # Non-2xx: do not read body.
                response.close()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return FetchResult(
                    url=url,
                    final_url=final_url,
                    status_code=status,
                    content_type=content_type,
                    text_body=None,
                    error=None,
                    truncated=False,
                    retry_after_seconds=retry_after,
                    elapsed_ms=elapsed_ms,
                    bytes_downloaded=0,
                )

            if not allowed:
                response.close()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return FetchResult(
                    url=url,
                    final_url=final_url,
                    status_code=status,
                    content_type=content_type,
                    text_body=None,
                    error=None,
                    truncated=False,
                    retry_after_seconds=retry_after,
                    elapsed_ms=elapsed_ms,
                    bytes_downloaded=0,
                )

            max_bytes = self._policies.max_response_bytes
            chunks: list[bytes] = []
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                bytes_downloaded += len(chunk)
                if bytes_downloaded > max_bytes:
                    truncated = True
                    break
            raw = b"".join(chunks)
            if truncated:
                raw = raw[:max_bytes]
                bytes_downloaded = len(raw)

            encoding = response.encoding or response.apparent_encoding or "utf-8"
            try:
                text_body = raw.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                text_body = raw.decode("utf-8", errors="replace")
        finally:
            response.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FetchResult(
            url=url,
            final_url=final_url,
            status_code=status,
            content_type=content_type,
            text_body=text_body,
            error=None,
            truncated=truncated,
            retry_after_seconds=retry_after,
            elapsed_ms=elapsed_ms,
            bytes_downloaded=bytes_downloaded,
        )


class RobotsCache:
    """Per-host robots.txt cache with single-flight loading."""

    def __init__(self, timeout: float = 20.0, user_agent: str = USER_AGENT) -> None:
        self._timeout = timeout
        self._user_agent = user_agent
        self._lock = threading.Lock()
        self._parsers: dict[str, RobotFileParser] = {}
        self._load_events: dict[str, threading.Event] = {}
        self._load_warnings: dict[str, str] = {}
        self._crawl_delay_capped: set[str] = set()

    def _domain_key(self, url: str) -> str:
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        netloc = (parsed.netloc or "").lower()
        return f"{scheme}://{netloc}"

    def _ensure_loaded(self, url: str) -> RobotFileParser:
        key = self._domain_key(url)

        with self._lock:
            cached = self._parsers.get(key)
            if cached is not None:
                return cached
            event = self._load_events.get(key)
            if event is None:
                event = threading.Event()
                self._load_events[key] = event
                is_loader = True
            else:
                is_loader = False

        if not is_loader:
            event.wait(timeout=self._timeout + 5.0)
            with self._lock:
                return self._parsers.get(key) or self._allow_all_parser()

        parser = RobotFileParser()
        robots_url = f"{key}/robots.txt"
        parser.set_url(robots_url)
        warning: str | None = None

        try:
            response = requests.get(
                robots_url,
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
            status = response.status_code
            if 200 <= status < 300:
                parser.parse(response.text.splitlines())
            elif 400 <= status < 500:
                # 4xx -> allow all
                parser.parse([])
            else:
                # 5xx -> allow all, warn once
                parser.parse([])
                warning = f"robots_http_{status}"
        except requests.RequestException as exc:
            parser.parse([])
            warning = f"robots_network: {exc}"

        with self._lock:
            self._parsers[key] = parser
            if warning:
                self._load_warnings[key] = warning
            self._load_events.pop(key, None)
            event.set()
            return parser

    @staticmethod
    def _allow_all_parser() -> RobotFileParser:
        parser = RobotFileParser()
        parser.parse([])
        return parser

    def pop_load_warning(self, url: str) -> str | None:
        key = self._domain_key(url)
        with self._lock:
            return self._load_warnings.pop(key, None)

    def can_fetch(self, url: str, user_agent: str) -> bool:
        parser = self._ensure_loaded(url)
        return parser.can_fetch(user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        parser = self._ensure_loaded(url)
        delay = parser.crawl_delay(self._user_agent)
        if delay is None:
            delay = parser.crawl_delay("*")
        if delay is None:
            return None
        try:
            value = float(delay)
        except (TypeError, ValueError):
            return None
        if value > _MAX_CRAWL_DELAY:
            key = self._domain_key(url)
            with self._lock:
                capped = key not in self._crawl_delay_capped
                self._crawl_delay_capped.add(key)
            # Caller may log CRAWL_DELAY_CAPPED when seeing >30 before cap;
            # we return the capped value and expose whether it was first cap.
            if capped:
                # Stash a warning the crawler can log once.
                with self._lock:
                    self._load_warnings.setdefault(
                        key, f"crawl_delay_capped:{value}"
                    )
            return _MAX_CRAWL_DELAY
        return value
