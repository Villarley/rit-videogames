from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Scope keywords for topical focused crawling (bilingual).
_DEFAULT_SCOPE_KEYWORDS = [
    "game",
    "games",
    "gaming",
    "gameplay",
    "gamer",
    "gamers",
    "videogame",
    "videogames",
    "video_game",
    "video-game",
    "juego",
    "juegos",
    "videojuego",
    "videojuegos",
    "jugabilidad",
    "consola",
    "consolas",
    "console",
    "consoles",
    "nintendo",
    "playstation",
    "xbox",
    "sega",
    "steam",
    "esports",
    "multiplayer",
    "multijugador",
    "rpg",
    "arcade",
    "speedrun",
    "dlc",
]

_DENIED_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".bmp",
        ".tif",
        ".pdf",
        ".zip",
        ".rar",
        ".7z",
        ".gz",
        ".tar",
        ".exe",
        ".dmg",
        ".apk",
        ".mp3",
        ".mp4",
        ".m4a",
        ".ogg",
        ".wav",
        ".webm",
        ".avi",
        ".mov",
        ".mkv",
        ".flv",
        ".css",
        ".js",
        ".json",
        ".xml",
        ".rss",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
    }
)

# MediaWiki query keys that mark non-article / utility URLs.
_DENIED_QUERY_KEYS = frozenset(
    {
        "action",
        "oldid",
        "diff",
        "curid",
        "printable",
        "veaction",
        "redlink",
        "returnto",
        "search",
    }
)

# MediaWiki / wiki namespaces (raw forms; encoded forms derived below).
_MW_NAMESPACES = (
    "Special:",
    "Especial:",
    "User:",
    "Usuario:",
    "User_talk:",
    "Usuario_discusión:",
    "Talk:",
    "Discusión:",
    "File:",
    "Archivo:",
    "Image:",
    "Template:",
    "Plantilla:",
    "Template_talk:",
    "Help:",
    "Ayuda:",
    "MediaWiki:",
    "Module:",
    "Módulo:",
    "Wikipedia:",
    "Project:",
    "Draft:",
    "Thread:",
    "Board:",
    "Message_Wall:",
    "Forum:",
)

_JUNK_PATH_PREFIXES = (
    "/login",
    "/signin",
    "/signup",
    "/register",
    "/account",
    "/cart",
    "/checkout",
    "/search",
    "/feed",
    "/wp-json",
    "/cdn-cgi/",
    "/amp/",
)

_MODE_RANK = {"domain": 3, "prefix": 2, "topical": 1}

_TRACKING_PARAMS = frozenset({"fbclid", "gclid"})


def _percent_encode_namespace(ns: str) -> str:
    """Encode ':' and non-ASCII for path matching."""
    out: list[str] = []
    for ch in ns:
        if ch == ":":
            out.append("%3A")
        elif ord(ch) > 127:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
    return "".join(out)


_MW_NAMESPACE_PATTERNS: tuple[str, ...] = tuple(
    sorted(
        {
            *(ns.lower() for ns in _MW_NAMESPACES),
            *(_percent_encode_namespace(ns).lower() for ns in _MW_NAMESPACES),
            # Common alternate encoding for discusión (ó = %C3%B3).
            "usuario_discusi%c3%b3n:",
            "discusi%c3%b3n:",
            "m%c3%b3dulo:",
        },
        key=len,
        reverse=True,
    )
)

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.IGNORECASE)


@dataclass(frozen=True)
class HostRule:
    mode: str  # domain | prefix | topical
    prefixes: tuple[str, ...] = ()


@dataclass
class CrawlPolicies:
    host_rules: dict[str, HostRule] = field(default_factory=dict)
    max_depth: int = 6
    max_pages_per_domain: int = 100000
    min_request_interval_seconds: float = 1.0
    respect_robots_txt: bool = True
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
    )
    max_response_bytes: int = 5_000_000
    min_words: int = 50
    topical_min_hits: int = 5
    topical_min_density: float = 3.0
    revisit_after_days: int = 7
    request_timeout: float = 20.0
    scope_keywords: list[str] = field(
        default_factory=lambda: list(_DEFAULT_SCOPE_KEYWORDS)
    )

    def normalize_url(self, url: str) -> str | None:
        try:
            parsed = urlparse(url.strip())
        except Exception:
            return None
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if not host:
            return None

        port = parsed.port
        if port is not None:
            if (scheme == "http" and port == 80) or (
                scheme == "https" and port == 443
            ):
                netloc = host
            else:
                netloc = f"{host}:{port}"
        else:
            # Preserve explicit non-default port already in netloc if any.
            netloc = host
            if parsed.netloc and "@" in parsed.netloc:
                # Drop userinfo; keep host:port only.
                netloc = parsed.netloc.split("@", 1)[-1].lower()
                if netloc.endswith(":80") and scheme == "http":
                    netloc = netloc[:-3]
                elif netloc.endswith(":443") and scheme == "https":
                    netloc = netloc[:-4]

        # Drop tracking params; keep the rest (stable order).
        kept: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            low = key.lower()
            if low.startswith("utm_") or low in _TRACKING_PARAMS:
                continue
            kept.append((key, value))
        query = urlencode(kept, doseq=True)

        path = parsed.path or "/"
        return urlunparse((scheme, netloc, path, "", query, ""))

    def is_denied(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return "bad_url"

        path = parsed.path or "/"
        path_lower = path.lower()

        # Extension deny (last path segment).
        last = path_lower.rsplit("/", 1)[-1]
        if "." in last:
            ext = "." + last.rsplit(".", 1)[-1]
            # Ignore query-looking suffixes; check bare extension.
            if ext in _DENIED_EXTENSIONS:
                return f"extension:{ext}"

        # MediaWiki utility query params.
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            low_key = key.lower()
            if low_key in _DENIED_QUERY_KEYS:
                return f"query:{low_key}"
            if low_key == "title" and value.lower().startswith("special"):
                return "query:title=Special"

        # MediaWiki namespaces in path (raw + percent-encoded).
        # Also check unquoted form of the path for encoded matches.
        path_for_ns = path_lower
        for ns in _MW_NAMESPACE_PATTERNS:
            if ns in path_for_ns:
                return f"namespace:{ns.rstrip(':')}"

        # Generic junk paths (/tag/ is intentionally allowed).
        for junk in _JUNK_PATH_PREFIXES:
            if _path_is_junk(path_lower, junk):
                return f"junk_path:{junk}"

        return None

    def host_rule_for(self, url: str) -> HostRule | None:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return None
        if not host:
            return None
        return self.host_rules.get(host)

    def register_host(self, host: str, rule: HostRule) -> None:
        """Merge a host rule (most permissive wins; prefix unions)."""
        host = host.lower()
        existing = self.host_rules.get(host)
        if existing is None:
            self.host_rules[host] = rule
            return
        self.host_rules[host] = _merge_host_rules(existing, rule)

    def link_in_scope(
        self,
        url: str,
        anchor_text: str,
        parent_is_topical: bool,
    ) -> tuple[bool, str]:
        rule = self.host_rule_for(url)
        if rule is None:
            return False, "host_not_allowed"

        denied = self.is_denied(url)
        if denied is not None:
            return False, f"denied:{denied}"

        if rule.mode == "domain":
            return True, "domain"

        if rule.mode == "prefix":
            path = urlparse(url).path or "/"
            for prefix in rule.prefixes:
                if path.startswith(prefix):
                    return True, "prefix"
            return False, "prefix_miss"

        # topical
        if parent_is_topical:
            return True, "topical_parent"
        if self._keyword_hit(url, anchor_text):
            return True, "keyword"
        return False, "no_keyword"

    def _keyword_hit(self, url: str, anchor_text: str) -> bool:
        haystack = f"{url} {anchor_text}".lower()
        # Treat underscores/hyphens as spaces for matching.
        haystack_spaced = haystack.replace("_", " ").replace("-", " ")
        for kw in self.scope_keywords:
            low = kw.lower()
            if low in haystack or low.replace("_", " ").replace("-", " ") in haystack_spaced:
                return True
            # Also check spaced form of multi-word keywords.
            spaced = low.replace("_", " ").replace("-", " ")
            if spaced != low and spaced in haystack_spaced:
                return True
        return False

    def topical_score(self, text: str) -> tuple[int, float]:
        tokens = _WORD_RE.findall(text.lower())
        if not tokens:
            return 0, 0.0
        # Keyword variants as whole-word sets.
        kw_words: set[str] = set()
        for kw in self.scope_keywords:
            parts = _WORD_RE.findall(kw.lower().replace("_", " ").replace("-", " "))
            if len(parts) == 1:
                kw_words.add(parts[0])
            elif len(parts) > 1:
                # Multi-word: count as hit when the joined phrase appears.
                kw_words.add(" ".join(parts))

        hits = 0
        # Single-token keywords.
        single = {w for w in kw_words if " " not in w}
        multi = [w for w in kw_words if " " in w]
        for tok in tokens:
            if tok in single:
                hits += 1
        if multi:
            joined = " ".join(tokens)
            for phrase in multi:
                # Count non-overlapping occurrences roughly.
                start = 0
                while True:
                    idx = joined.find(phrase, start)
                    if idx < 0:
                        break
                    hits += 1
                    start = idx + len(phrase)

        density = (hits / len(tokens)) * 1000.0
        return hits, density

    def is_topical(self, text: str) -> bool:
        hits, density = self.topical_score(text)
        return hits >= self.topical_min_hits and density >= self.topical_min_density

    @classmethod
    def build_host_rules(
        cls,
        seed_rows: list[dict[str, str]],
    ) -> dict[str, HostRule]:
        """Build host_rules from seed CSV rows (most permissive wins)."""
        rules: dict[str, HostRule] = {}
        for row in seed_rows:
            url = (row.get("url") or "").strip()
            mode = (row.get("scope_mode") or "topical").strip().lower()
            if mode not in _MODE_RANK:
                mode = "topical"
            try:
                parsed = urlparse(url)
            except Exception:
                continue
            host = (parsed.hostname or "").lower()
            if not host:
                continue
            prefixes: tuple[str, ...] = ()
            if mode == "prefix":
                path = parsed.path or "/"
                prefixes = (path,)
            new_rule = HostRule(mode=mode, prefixes=prefixes)
            if host not in rules:
                rules[host] = new_rule
            else:
                rules[host] = _merge_host_rules(rules[host], new_rule)
        return rules


def _path_is_junk(path_lower: str, junk: str) -> bool:
    if junk.endswith("/"):
        return path_lower.startswith(junk) or path_lower == junk.rstrip("/")
    return path_lower == junk or path_lower.startswith(junk + "/")


def _merge_host_rules(a: HostRule, b: HostRule) -> HostRule:
    rank_a = _MODE_RANK.get(a.mode, 0)
    rank_b = _MODE_RANK.get(b.mode, 0)
    if rank_a > rank_b:
        winner_mode = a.mode
    elif rank_b > rank_a:
        winner_mode = b.mode
    else:
        winner_mode = a.mode

    prefixes: tuple[str, ...] = ()
    if winner_mode == "prefix":
        merged: set[str] = set()
        if a.mode == "prefix":
            merged.update(a.prefixes)
        if b.mode == "prefix":
            merged.update(b.prefixes)
        prefixes = tuple(sorted(merged))
    return HostRule(mode=winner_mode, prefixes=prefixes)
