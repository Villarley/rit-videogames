from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from lxml import html
from lxml.etree import ParserError

_WHITESPACE_RE = re.compile(r"[ \t\x0b\f\r]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ']+")

_STRIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "form",
        "nav",
        "footer",
        "header",
        "aside",
    }
)

# id/class tokens that mark chrome (MediaWiki and generic).
_CHROME_ID_CLASS = frozenset(
    {
        "toc",
        "navbox",
        "mw-editsection",
        "reference",
        "reflist",
        "printfooter",
        "catlinks",
        "mw-jump-link",
        "mw-indicators",
        "siteNotice",
        "mw-navigation",
        "footer-info",
        "footer-places",
        "footer-icons",
        "sidebar",
        "navigation",
        "nav-menu",
        "breadcrumb",
        "breadcrumbs",
        "cookie-banner",
        "cookie-notice",
        "vector-toc",
        "mw-references-wrap",
        "references",
        "navbox-styles",
        "mw-cite-backlink",
        "noprint",
        "metadata",
    }
)

_BLOCK_BREAK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "dd",
        "dt",
        "dl",
        "blockquote",
        "pre",
        "table",
        "section",
        "figure",
        "figcaption",
        "hr",
    }
)
_TABLE_CELL_TAGS = frozenset({"td", "th"})

_EN_STOPWORDS = frozenset(
    {
        "the",
        "be",
        "to",
        "of",
        "and",
        "a",
        "in",
        "that",
        "have",
        "i",
        "it",
        "for",
        "not",
        "on",
        "with",
        "he",
        "as",
        "you",
        "do",
        "at",
        "this",
        "but",
        "his",
        "by",
        "from",
    }
)

_ES_STOPWORDS = frozenset(
    {
        "de",
        "la",
        "que",
        "el",
        "en",
        "y",
        "a",
        "los",
        "del",
        "se",
        "las",
        "por",
        "un",
        "para",
        "con",
        "no",
        "una",
        "su",
        "al",
        "lo",
        "como",
        "mas",
        "más",
        "pero",
        "sus",
        "le",
        "es",
        "o",
        "este",
        "esta",
        "esto",
    }
)


@dataclass
class ExtractedPage:
    title: str
    text: str
    links: list[tuple[str, str]]
    language: str = ""


def _normalize_whitespace_flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    normalized = _normalize_whitespace_flat(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_html(raw_html: str | bytes) -> html.HtmlElement | None:
    if not raw_html:
        return None
    try:
        if isinstance(raw_html, str):
            payload = raw_html.encode("utf-8", errors="replace")
        else:
            payload = raw_html
        return html.fromstring(payload)
    except ParserError:
        return None
    except Exception:
        return None


def _element_tag(el: html.HtmlElement) -> str | None:
    tag = el.tag
    if not isinstance(tag, str):
        return None
    return tag.lower()


def _append_to_element_end(el: html.HtmlElement, suffix: str) -> None:
    if len(el):
        last = el[-1]
        last.tail = (last.tail or "") + suffix
    else:
        el.text = (el.text or "") + suffix


def _replace_br_with_newline(br: html.HtmlElement) -> None:
    parent = br.getparent()
    if parent is None:
        return
    idx = parent.index(br)
    br_tail = br.tail or ""
    br.drop_tree()
    if idx > 0:
        prev = parent[idx - 1]
        prev.tail = (prev.tail or "") + "\n" + br_tail
    else:
        parent.text = (parent.text or "") + "\n" + br_tail


def _decompose_chrome(root: html.HtmlElement) -> None:
    to_drop: list[html.HtmlElement] = []
    for el in root.iter():
        if _element_tag(el) is None:
            continue
        eid = el.get("id")
        if eid in _CHROME_ID_CLASS:
            to_drop.append(el)
            continue
        class_attr = el.get("class")
        if class_attr:
            tokens = class_attr.split()
            if _CHROME_ID_CLASS.intersection(tokens):
                to_drop.append(el)
    for el in to_drop:
        if el.getparent() is not None:
            el.drop_tree()


def _strip_structural_tags(root: html.HtmlElement) -> None:
    to_drop: list[html.HtmlElement] = []
    for el in root.iter():
        tag = _element_tag(el)
        if tag in _STRIP_TAGS:
            to_drop.append(el)
    for el in to_drop:
        if el.getparent() is not None:
            el.drop_tree()


def _collapsed_text_length(element: html.HtmlElement) -> int:
    return len(_normalize_whitespace_flat(element.text_content() or ""))


def _find_main_content(root: html.HtmlElement) -> html.HtmlElement:
    mw_list = root.xpath('//*[@id="mw-content-text"]')
    if mw_list:
        return mw_list[0]

    candidates: list[html.HtmlElement] = []
    seen: set[int] = set()

    def _add(tag: html.HtmlElement | None) -> None:
        if tag is None or id(tag) in seen:
            return
        seen.add(id(tag))
        candidates.append(tag)

    for article in root.xpath("//article"):
        _add(article)
    main_nodes = root.xpath("//main")
    if main_nodes:
        _add(main_nodes[0])
    role_main = root.xpath('//*[@role="main"]')
    if role_main:
        _add(role_main[0])
    content_nodes = root.xpath('//*[@id="content"]')
    if content_nodes:
        _add(content_nodes[0])

    body_nodes = root.xpath("//body")
    body = body_nodes[0] if body_nodes else None
    body_len = _collapsed_text_length(body) if body is not None else _collapsed_text_length(root)

    if not candidates:
        if body is not None:
            return body
        return root

    best = max(candidates, key=_collapsed_text_length)
    if body_len > 0 and _collapsed_text_length(best) < 0.25 * body_len:
        if body is not None:
            return body
        return root
    return best


def _mark_block_breaks(root: html.HtmlElement) -> None:
    for br in list(root.iter("br")):
        if br.getparent() is None:
            continue
        _replace_br_with_newline(br)
    for el in root.iter():
        tag = _element_tag(el)
        if tag is None:
            continue
        if tag in _TABLE_CELL_TAGS:
            _append_to_element_end(el, " ")
        elif tag == "tr":
            _append_to_element_end(el, "\n")
        elif tag in _BLOCK_BREAK_TAGS:
            _append_to_element_end(el, "\n")


def _readable_text(element: html.HtmlElement) -> str:
    _mark_block_breaks(element)
    raw = element.text_content() or ""
    lines: list[str] = []
    for line in raw.split("\n"):
        collapsed = _WHITESPACE_RE.sub(" ", line).strip()
        lines.append(collapsed)
    text = "\n".join(lines)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _collect_links(root: html.HtmlElement, base_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen_hrefs: set[str] = set()
    for anchor in root.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(("mailto:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if absolute in seen_hrefs:
            continue
        seen_hrefs.add(absolute)
        anchor_text = _normalize_whitespace_flat(anchor.text_content() or "")
        links.append((absolute, anchor_text))
    return links


def _extract_title(root: html.HtmlElement) -> str:
    title_nodes = root.xpath("//title")
    if not title_nodes:
        return ""
    return (title_nodes[0].text_content() or "").strip()


def _extract_text_and_language(root: html.HtmlElement) -> tuple[str, str]:
    _strip_structural_tags(root)
    _decompose_chrome(root)
    main = _find_main_content(root)
    text = _readable_text(main)
    language = detect_language(text)
    return text, language


def detect_language(text: str) -> str:
    tokens = [t.lower() for t in _WORD_RE.findall(text[:20000])][:2000]
    if len(tokens) < 20:
        return "other"
    en_hits = sum(1 for t in tokens if t in _EN_STOPWORDS)
    es_hits = sum(1 for t in tokens if t in _ES_STOPWORDS)
    n = len(tokens)
    en_ratio = en_hits / n
    es_ratio = es_hits / n
    threshold = 0.04
    if en_ratio >= threshold and en_ratio > es_ratio:
        return "en"
    if es_ratio >= threshold and es_ratio > en_ratio:
        return "es"
    return "other"


def extract(html: str, base_url: str) -> ExtractedPage:
    raw_html = html or ""
    root = _parse_html(raw_html)
    if root is None:
        return ExtractedPage(title="", text="", links=[], language="other")

    title = _extract_title(root)

    links: list[tuple[str, str]] = []
    try:
        links = _collect_links(root, base_url)
    except Exception:
        pass

    text = ""
    language = "other"
    try:
        text, language = _extract_text_and_language(root)
    except Exception:
        try:
            root_fb = _parse_html(raw_html)
            if root_fb is not None:
                text, language = _extract_text_and_language(root_fb)
        except Exception:
            text = ""
            language = "other"

    return ExtractedPage(title=title, text=text, links=links, language=language)
