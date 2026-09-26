from __future__ import annotations

import pytest

from crawler.extractor import content_hash, detect_language, extract


MEDIAWIKI_HTML = """
<html>
<head><title>Creeper - Minecraft Wiki</title></head>
<body>
<nav><a href="https://minecraft.wiki/w/Main_Page">Home</a></nav>
<div id="mw-content-text">
  <h1>Creeper</h1>
  <p>A creeper is an electronic game that <a href="/w/Mob">mob</a> players fear in Minecraft.</p>
  <table><tr><td>Health</td><td>20</td></tr></table>
  <br>
  <p>Second paragraph about gameplay mechanics.</p>
  <div class="navbox">
    <a href="/w/Creeper_(disambiguation)">Disambig</a>
  </div>
  <span class="mw-editsection"><a href="/w/edit">edit</a></span>
</div>
<script>window.track = true;</script>
</body>
</html>
"""

NEWS_HTML = """
<html>
<head><title>News Site</title></head>
<body>
<article><p>Short teaser one.</p></article>
<article><p>Another brief item.</p></article>
<article>
  <p>""" + ("Major launch coverage with extensive detail. " * 40) + """</p>
  <a href="/games/review">Full review</a>
</article>
</body>
</html>
"""

NESTED_CHROME_HTML = """
<html>
<head><title>Nested Chrome</title></head>
<body>
<div id="mw-content-text">
  <p>Survival gameplay on console and PC remains a core videogame experience for gamers.</p>
  <div id="toc">
    <div class="navbox">
      <a href="https://example.wiki/w/Related">Related</a>
    </div>
  </div>
</div>
</body>
</html>
"""


class TestMediaWikiExtract:
    def test_title(self) -> None:
        page = extract(MEDIAWIKI_HTML, "https://minecraft.wiki/w/Creeper")
        assert page.title == "Creeper - Minecraft Wiki"

    def test_prose_contiguous_with_inline_link(self) -> None:
        page = extract(MEDIAWIKI_HTML, "https://minecraft.wiki/w/Creeper")
        assert "is an electronic game that" in page.text.replace("\n", " ")

    def test_excludes_chrome_and_script(self) -> None:
        page = extract(MEDIAWIKI_HTML, "https://minecraft.wiki/w/Creeper")
        assert "window.track" not in page.text
        assert "Disambig" not in page.text
        assert "Home" not in page.text

    def test_links_include_nav_and_navbox(self) -> None:
        page = extract(MEDIAWIKI_HTML, "https://minecraft.wiki/w/Creeper")
        hrefs = {href for href, _ in page.links}
        assert "https://minecraft.wiki/w/Main_Page" in hrefs
        assert "https://minecraft.wiki/w/Creeper_(disambiguation)" in hrefs
        assert "https://minecraft.wiki/w/Mob" in hrefs

    def test_mailto_and_javascript_excluded(self) -> None:
        html = """
        <html><body>
        <a href="mailto:a@b.com">mail</a>
        <a href="javascript:alert(1)">js</a>
        <a href="/ok">ok</a>
        </body></html>
        """
        page = extract(html, "https://example.com/page")
        hrefs = {href for href, _ in page.links}
        assert hrefs == {"https://example.com/ok"}


class TestNewsArticlePick:
    def test_picks_largest_article(self) -> None:
        page = extract(NEWS_HTML, "https://news.example.com/")
        assert "Major launch coverage" in page.text
        assert "Short teaser one" not in page.text
        hrefs = {href for href, _ in page.links}
        assert "https://news.example.com/games/review" in hrefs


class TestRobustInput:
    def test_empty_string(self) -> None:
        page = extract("", "https://example.com/")
        assert page.title == ""
        assert page.text == ""
        assert page.links == []

    def test_garbage_input(self) -> None:
        page = extract("not <<html>> at all", "https://example.com/")
        assert page.title == ""
        assert isinstance(page.text, str)
        assert isinstance(page.links, list)


class TestDetectLanguage:
    def test_english(self) -> None:
        text = (
            "The game was released in that year and it have been popular with players "
            "on this platform for a long time in the industry and beyond for many fans."
        )
        assert detect_language(text) == "en"

    def test_spanish(self) -> None:
        text = (
            "El juego fue lanzado en ese ano y los jugadores de la consola lo han "
            "disfrutado en la plataforma con sus amigos para una experiencia unica "
            "en el mercado de entretenimiento digital para todos los usuarios."
        )
        assert detect_language(text) == "es"


class TestContentHash:
    def test_whitespace_insensitive(self) -> None:
        a = content_hash("hello   world\n\nfoo")
        b = content_hash("hello world foo")
        assert a == b


def test_nested_chrome_does_not_empty_page() -> None:
    page = extract(NESTED_CHROME_HTML, "https://example.wiki/w/Main")
    assert page.title == "Nested Chrome"
    assert "Survival gameplay" in page.text
    assert len(page.text.strip()) > 20
    hrefs = {href for href, _ in page.links}
    assert "https://example.wiki/w/Related" in hrefs
