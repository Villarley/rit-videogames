from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
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
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_url TEXT,
  to_url TEXT,
  discovered_at TEXT
);
CREATE TABLE IF NOT EXISTS seeds (
  seed_id TEXT PRIMARY KEY,
  url TEXT,
  subtema TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_pages_crawler_source ON pages(crawler_source);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain_from_url(url: str) -> str:
    return urlparse(url).netloc or "unknown"


def _word_count(text: str) -> int:
    return len(text.split())


class Repository:
    def __init__(self, db_path: str, text_root: str) -> None:
        self._db_path = db_path
        self._text_root = Path(text_root)
        self._text_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    def is_duplicate(self, content_hash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM pages WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            ).fetchone()
        return row is not None

    def save_page(
        self,
        *,
        url: str,
        final_url: str,
        seed_id: str,
        depth: int,
        parent_url: str | None,
        http_status: int,
        content_type: str,
        title: str,
        text: str,
        content_hash: str,
        language: str,
        crawler_source: str,
    ) -> str | None:
        domain = _domain_from_url(url)
        relative_path = f"{crawler_source}/{domain}/{content_hash[:16]}.txt"
        absolute_path = self._text_root / relative_path
        fetched_at = _utc_now_iso()
        words = _word_count(text)

        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO pages (
                  url, final_url, seed_id, depth, parent_url, fetched_at,
                  http_status, content_type, title, text_path, word_count,
                  content_hash, language, crawler_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    final_url,
                    seed_id,
                    depth,
                    parent_url,
                    fetched_at,
                    http_status,
                    content_type,
                    title,
                    relative_path,
                    words,
                    content_hash,
                    language,
                    crawler_source,
                ),
            )
            if cursor.rowcount == 0:
                return None

            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_text(text, encoding="utf-8")
            self._conn.commit()

        return relative_path

    def save_links(self, from_url: str, to_url_list: list[str]) -> None:
        if not to_url_list:
            return

        discovered_at = _utc_now_iso()
        rows = [(from_url, to_url, discovered_at) for to_url in to_url_list]

        with self._lock:
            self._conn.executemany(
                "INSERT INTO links (from_url, to_url, discovered_at) VALUES (?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def upsert_seed(
        self,
        seed_id: str,
        url: str,
        subtema: str = "",
        notes: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO seeds (seed_id, url, subtema, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(seed_id) DO UPDATE SET
                  url = excluded.url,
                  subtema = excluded.subtema,
                  notes = excluded.notes
                """,
                (seed_id, url, subtema, notes),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
