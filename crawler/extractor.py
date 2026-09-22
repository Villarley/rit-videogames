from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


@dataclass
class ExtractedPage:
    title: str
    text: str
    links: list[tuple[str, str]]


_WHITESPACE_RE = re.compile(r"\s+")
_STRIP_TAGS = ("script", "style", "nav", "footer", "header")


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def content_hash(text: str) -> str:
    normalized = _normalize_whitespace(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract(html: str, base_url: str) -> ExtractedPage:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.title
    title = title_tag.get_text(strip=True) if title_tag else ""

    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    text = _normalize_whitespace(soup.get_text(separator=" "))

    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#"):
            continue

        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if href.lower().startswith(("mailto:", "javascript:")):
            continue

        anchor_text = _normalize_whitespace(anchor.get_text())
        links.append((absolute, anchor_text))

    return ExtractedPage(title=title, text=text, links=links)
