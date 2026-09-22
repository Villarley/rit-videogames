from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class CrawlPolicies:
    allowed_domains: set[str] | None = None
    scope_keywords: list[str] = field(
        default_factory=lambda: ["game", "videogame", "juego", "videojuego"]
    )
    max_depth: int = 3
    max_pages_per_domain: int = 200
    min_request_interval_seconds: float = 1.0
    respect_robots_txt: bool = True
    allowed_content_types: tuple[str, ...] = ("text/html",)
    revisit_after_days: int = 7

    def is_in_scope(self, url: str, anchor_text: str = "") -> bool:
        if self.allowed_domains is not None:
            domain = urlparse(url).netloc.lower()
            if domain not in self.allowed_domains:
                return False

        if not self.scope_keywords:
            return True

        haystack = f"{url} {anchor_text}".lower()
        return any(keyword.lower() in haystack for keyword in self.scope_keywords)
