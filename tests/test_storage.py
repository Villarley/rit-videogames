from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crawler.extractor import content_hash
from crawler.storage import Repository, _PAGES_EXTRA_COLUMNS


def _save(
    repo: Repository,
    *,
    url: str,
    text: str,
    crawler_source: str = "custom",
) -> str:
    ch = content_hash(text)
    return repo.save_page(
        url=url,
        final_url=url,
        seed_id="seed01",
        depth=0,
        parent_url=None,
        http_status=200,
        content_type="text/html",
        title="Title",
        text=text,
        content_hash=ch,
        language="en",
        crawler_source=crawler_source,
    )


class TestSavePageOutcomes:
    def test_saved_then_duplicate_content(self, repo: Repository) -> None:
        text = "Unique body text for duplicate hash test."
        assert _save(repo, url="https://a.example/one", text=text) == "saved"
        assert (
            _save(repo, url="https://a.example/two", text=text)
            == "duplicate_content"
        )

    def test_unchanged_same_url_same_text(self, repo: Repository) -> None:
        text = "Stable page body for unchanged test."
        url = "https://b.example/page"
        assert _save(repo, url=url, text=text) == "saved"
        assert _save(repo, url=url, text=text) == "unchanged"

    def test_updated_same_url_new_text(self, repo: Repository) -> None:
        url = "https://c.example/page"
        assert _save(repo, url=url, text="Version one text.") == "saved"
        assert _save(repo, url=url, text="Version two text.") == "updated"


class TestTextFilesAndCounts:
    def test_text_file_at_sharded_path(self, repo: Repository) -> None:
        text = "Sharded storage verification content."
        url = "https://shard.example/article"
        ch = content_hash(text)
        status = _save(repo, url=url, text=text, crawler_source="custom")
        assert status == "saved"
        rel = repo.text_path_for("custom", "shard.example", ch)
        abs_path = Path(repo._text_root) / rel
        assert abs_path.is_file()
        assert abs_path.read_text(encoding="utf-8") == text

    def test_bytes_and_pages_per_crawler_source(self, repo: Repository) -> None:
        _save(repo, url="https://d.example/1", text="Alpha text body.", crawler_source="custom")
        _save(repo, url="https://d.example/2", text="Beta text body here.", crawler_source="scrapy")
        assert repo.pages_count("custom") == 1
        assert repo.pages_count("scrapy") == 1
        assert repo.text_bytes_total("custom") > 0
        assert repo.text_bytes_total("scrapy") > 0


class TestSchemaMigration:
    def test_old_pages_table_gets_new_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE pages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              url TEXT UNIQUE NOT NULL,
              final_url TEXT,
              seed_id TEXT,
              depth INTEGER,
              parent_url TEXT,
              fetched_at TEXT,
              http_status INTEGER,
              content_type TEXT,
              title TEXT,
              text_path TEXT,
              word_count INTEGER,
              content_hash TEXT,
              language TEXT,
              crawler_source TEXT
            );
            """
        )
        conn.close()

        repo = Repository(str(db_path), str(tmp_path / "text"))
        try:
            rows = repo._conn.execute("PRAGMA table_info(pages)").fetchall()
            names = {row[1] for row in rows}
            for col in _PAGES_EXTRA_COLUMNS:
                assert col in names
        finally:
            repo.close()
