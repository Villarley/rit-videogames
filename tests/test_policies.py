from __future__ import annotations

import pytest

from crawler.policies import CrawlPolicies, HostRule


@pytest.fixture
def policies() -> CrawlPolicies:
    return CrawlPolicies()


class TestNormalizeUrl:
    def test_drops_fragment(self, policies: CrawlPolicies) -> None:
        url = "https://Example.com/path/page#section"
        assert policies.normalize_url(url) == "https://example.com/path/page"

    def test_removes_utm_params(self, policies: CrawlPolicies) -> None:
        url = "https://example.com/a?utm_source=x&utm_medium=y&keep=1"
        assert policies.normalize_url(url) == "https://example.com/a?keep=1"

    def test_lowercases_host(self, policies: CrawlPolicies) -> None:
        assert (
            policies.normalize_url("HTTPS://WWW.Example.COM/foo")
            == "https://www.example.com/foo"
        )

    def test_removes_default_https_port(self, policies: CrawlPolicies) -> None:
        assert (
            policies.normalize_url("https://example.com:443/foo")
            == "https://example.com/foo"
        )

    def test_non_http_scheme_returns_none(self, policies: CrawlPolicies) -> None:
        assert policies.normalize_url("ftp://example.com/file") is None
        assert policies.normalize_url("javascript:void(0)") is None


class TestIsDenied:
    def test_image_extension(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://x.com/photo.jpg") == "extension:.jpg"

    def test_pdf_extension(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://x.com/doc.pdf") == "extension:.pdf"

    def test_css_extension(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://x.com/style.css") == "extension:.css"

    def test_mediawiki_action_edit(self, policies: CrawlPolicies) -> None:
        reason = policies.is_denied(
            "https://en.wikipedia.org/w/index.php?title=Foo&action=edit"
        )
        assert reason == "query:action"

    def test_mediawiki_oldid(self, policies: CrawlPolicies) -> None:
        assert (
            policies.is_denied("https://wiki.example/w/Foo?oldid=123")
            == "query:oldid"
        )

    def test_special_namespace(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://en.wikipedia.org/wiki/Special:Search") is not None

    def test_especial_namespace(self, policies: CrawlPolicies) -> None:
        assert (
            policies.is_denied("https://es.wikipedia.org/wiki/Especial:Buscar")
            is not None
        )

    def test_user_namespace(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://en.wikipedia.org/wiki/User:Alice") is not None

    def test_archivo_namespace(self, policies: CrawlPolicies) -> None:
        assert (
            policies.is_denied("https://es.wikipedia.org/wiki/Archivo:Logo.png")
            is not None
        )

    def test_percent_encoded_archivo(self, policies: CrawlPolicies) -> None:
        assert (
            policies.is_denied("https://es.wikipedia.org/wiki/Archivo%3ALogo.png")
            is not None
        )

    def test_login_path(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://example.com/login") == "junk_path:/login"

    def test_allowed_minecraft_wiki_article(self, policies: CrawlPolicies) -> None:
        assert policies.is_denied("https://minecraft.wiki/w/Creeper") is None

    def test_allowed_es_wikipedia_article(self, policies: CrawlPolicies) -> None:
        assert (
            policies.is_denied("https://es.wikipedia.org/wiki/Super_Mario_Bros.")
            is None
        )


@pytest.fixture
def scoped_policies() -> CrawlPolicies:
    seeds = [
        {
            "url": "https://minecraft.wiki/w/Main_Page",
            "scope_mode": "domain",
        },
        {
            "url": "https://as.com/meristation/",
            "scope_mode": "prefix",
        },
        {
            "url": "https://en.wikipedia.org/wiki/Video_game",
            "scope_mode": "topical",
        },
    ]
    rules = CrawlPolicies.build_host_rules(seeds)
    return CrawlPolicies(host_rules=rules)


class TestLinkInScope:
    def test_domain_mode_same_host(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://minecraft.wiki/w/Creeper",
            "",
            parent_is_topical=False,
        )
        assert ok is True
        assert reason == "domain"

    def test_prefix_accepts_meristation(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://as.com/meristation/noticias/juego",
            "",
            parent_is_topical=False,
        )
        assert ok is True
        assert reason == "prefix"

    def test_prefix_rejects_futbol(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://as.com/futbol/partido",
            "",
            parent_is_topical=False,
        )
        assert ok is False
        assert reason == "prefix_miss"

    def test_topical_parent_accepts_any(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://en.wikipedia.org/wiki/Japan",
            "Japan",
            parent_is_topical=True,
        )
        assert ok is True
        assert reason == "topical_parent"

    def test_topical_keyword_anchor(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://en.wikipedia.org/wiki/History_of_computing",
            "Video game history",
            parent_is_topical=False,
        )
        assert ok is True
        assert reason == "keyword"

    def test_topical_keyword_url(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://en.wikipedia.org/wiki/Videojuego",
            "read more",
            parent_is_topical=False,
        )
        assert ok is True
        assert reason == "keyword"

    def test_topical_rejects_unrelated(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://en.wikipedia.org/wiki/Japan",
            "Japan",
            parent_is_topical=False,
        )
        assert ok is False
        assert reason == "no_keyword"

    def test_foreign_host_rejected(self, scoped_policies: CrawlPolicies) -> None:
        ok, reason = scoped_policies.link_in_scope(
            "https://bbc.co.uk/news/games",
            "games",
            parent_is_topical=False,
        )
        assert ok is False
        assert reason == "host_not_allowed"


class TestIsTopical:
    GAME_PARAGRAPH = (
        "Modern video games and console gaming attract millions of gamers worldwide. "
        "Nintendo, PlayStation, and Xbox platforms deliver rich gameplay experiences. "
        "Multiplayer esports titles on Steam showcase competitive videogame culture. "
        "RPG and arcade classics remain popular among dedicated gamers."
    )

    COOKING_PARAGRAPH = (
        "This recipe uses fresh basil, olive oil, and garlic simmered slowly. "
        "Season with salt and pepper, then serve the pasta with grated cheese. "
        "The sauce pairs well with crusty bread and a simple green salad."
    )

    def test_game_paragraph_is_topical(self, policies: CrawlPolicies) -> None:
        assert policies.is_topical(self.GAME_PARAGRAPH) is True

    def test_cooking_paragraph_not_topical(self, policies: CrawlPolicies) -> None:
        assert policies.is_topical(self.COOKING_PARAGRAPH) is False
