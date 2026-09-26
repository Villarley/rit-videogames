from __future__ import annotations

import re
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
  crawler_source TEXT,
  text_bytes INTEGER,
  outlinks_total INTEGER,
  outlinks_in_scope INTEGER,
  topical_hits INTEGER,
  topical_density REAL,
  scope_rule TEXT,
  truncated INTEGER,
  fetch_ms INTEGER
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
  notes TEXT,
  scope_mode TEXT,
  idioma TEXT
);
CREATE TABLE IF NOT EXISTS frontier (
  url TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  seed_id TEXT,
  depth INTEGER,
  parent_url TEXT,
  anchor_text TEXT,
  scope_rule TEXT,
  parent_topical INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  last_reason TEXT,
  enqueued_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_pages_crawler_source ON pages(crawler_source);
CREATE INDEX IF NOT EXISTS idx_frontier_domain_status_depth
  ON frontier(domain, status, depth);
"""

_PAGES_EXTRA_COLUMNS = {
    "text_bytes": "INTEGER",
    "outlinks_total": "INTEGER",
    "outlinks_in_scope": "INTEGER",
    "topical_hits": "INTEGER",
    "topical_density": "REAL",
    "scope_rule": "TEXT",
    "truncated": "INTEGER",
    "fetch_ms": "INTEGER",
}

_SEEDS_EXTRA_COLUMNS = {
    "scope_mode": "TEXT",
    "idioma": "TEXT",
    "notes": "TEXT",
}

_UNSAFE_FS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain_from_url(url: str) -> str:
    return (urlparse(url).hostname or urlparse(url).netloc or "unknown").lower()


def _sanitize_domain(domain: str) -> str:
    cleaned = _UNSAFE_FS_RE.sub("_", domain)
    cleaned = cleaned.replace(":", "_")
    return cleaned or "unknown"


def _word_count(text: str) -> int:
    return len(text.split())


class Repository:
    def __init__(self, db_path: str, text_root: str) -> None:
        self._db_path = db_path
        self._text_root = Path(text_root)
        self._text_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=60000")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._add_missing_columns("pages", _PAGES_EXTRA_COLUMNS)
            self._add_missing_columns("seeds", _SEEDS_EXTRA_COLUMNS)
            self._conn.commit()

    def _add_missing_columns(
        self, table: str, columns: dict[str, str]
    ) -> None:
        existing = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, col_type in columns.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"
                )

    def text_path_for(self, crawler_source: str, domain: str, content_hash: str) -> str:
        safe_domain = _sanitize_domain(domain)
        return (
            f"{crawler_source}/{safe_domain}/"
            f"{content_hash[:2]}/{content_hash[:16]}.txt"
        )

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
        text_bytes: int | None = None,
        outlinks_total: int = 0,
        outlinks_in_scope: int = 0,
        topical_hits: int = 0,
        topical_density: float = 0.0,
        scope_rule: str = "",
        truncated: int = 0,
        fetch_ms: int = 0,
    ) -> str:
        """Atomically save page. Returns: saved|duplicate_content|unchanged|updated."""
        domain = _domain_from_url(final_url or url)
        relative_path = self.text_path_for(crawler_source, domain, content_hash)
        absolute_path = self._text_root / relative_path
        fetched_at = _utc_now_iso()
        words = _word_count(text)
        if text_bytes is None:
            text_bytes = len(text.encode("utf-8"))

        with self._lock:
            existing = self._conn.execute(
                "SELECT url, content_hash, text_path FROM pages WHERE url = ?",
                (url,),
            ).fetchone()

            if existing is not None:
                old_hash = existing["content_hash"]
                old_path = existing["text_path"]
                if old_hash == content_hash:
                    self._conn.execute(
                        "UPDATE pages SET fetched_at = ?, http_status = ?, "
                        "fetch_ms = ? WHERE url = ?",
                        (fetched_at, http_status, fetch_ms, url),
                    )
                    self._conn.commit()
                    return "unchanged"

                # Same URL, new content: overwrite.
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                absolute_path.write_text(text, encoding="utf-8")
                if old_path and old_path != relative_path:
                    old_abs = self._text_root / old_path
                    try:
                        if old_abs.is_file() and old_abs != absolute_path:
                            old_abs.unlink()
                    except OSError:
                        pass
                self._conn.execute(
                    """
                    UPDATE pages SET
                      final_url = ?, seed_id = ?, depth = ?, parent_url = ?,
                      fetched_at = ?, http_status = ?, content_type = ?,
                      title = ?, text_path = ?, word_count = ?, content_hash = ?,
                      language = ?, crawler_source = ?, text_bytes = ?,
                      outlinks_total = ?, outlinks_in_scope = ?,
                      topical_hits = ?, topical_density = ?, scope_rule = ?,
                      truncated = ?, fetch_ms = ?
                    WHERE url = ?
                    """,
                    (
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
                        text_bytes,
                        outlinks_total,
                        outlinks_in_scope,
                        topical_hits,
                        topical_density,
                        scope_rule,
                        truncated,
                        fetch_ms,
                        url,
                    ),
                )
                self._conn.commit()
                return "updated"

            # New URL: reject if another URL already has this hash.
            other = self._conn.execute(
                "SELECT url FROM pages WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            ).fetchone()
            if other is not None:
                return "duplicate_content"

            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            created_file = not absolute_path.exists()
            absolute_path.write_text(text, encoding="utf-8")
            try:
                self._conn.execute(
                    """
                    INSERT INTO pages (
                      url, final_url, seed_id, depth, parent_url, fetched_at,
                      http_status, content_type, title, text_path, word_count,
                      content_hash, language, crawler_source, text_bytes,
                      outlinks_total, outlinks_in_scope, topical_hits,
                      topical_density, scope_rule, truncated, fetch_ms
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
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
                        text_bytes,
                        outlinks_total,
                        outlinks_in_scope,
                        topical_hits,
                        topical_density,
                        scope_rule,
                        truncated,
                        fetch_ms,
                    ),
                )
                self._conn.commit()
            except Exception:
                if created_file:
                    try:
                        absolute_path.unlink()
                    except OSError:
                        pass
                raise
            return "saved"

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
        scope_mode: str = "",
        idioma: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO seeds (seed_id, url, subtema, notes, scope_mode, idioma)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(seed_id) DO UPDATE SET
                  url = excluded.url,
                  subtema = excluded.subtema,
                  notes = excluded.notes,
                  scope_mode = excluded.scope_mode,
                  idioma = excluded.idioma
                """,
                (seed_id, url, subtema, notes, scope_mode, idioma),
            )
            self._conn.commit()

    # --- Frontier helpers ---

    def enqueue_many(self, rows: list[dict]) -> list[str]:
        """INSERT OR IGNORE frontier rows. Returns newly inserted urls."""
        if not rows:
            return []
        now = _utc_now_iso()
        inserted: list[str] = []
        with self._lock:
            for row in rows:
                url = row["url"]
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO frontier (
                      url, domain, seed_id, depth, parent_url, anchor_text,
                      scope_rule, parent_topical, status, attempts, last_reason,
                      enqueued_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                    """,
                    (
                        url,
                        row["domain"],
                        row.get("seed_id"),
                        row.get("depth", 0),
                        row.get("parent_url"),
                        row.get("anchor_text", ""),
                        row.get("scope_rule", ""),
                        1 if row.get("parent_topical") else 0,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount > 0:
                    inserted.append(url)
            self._conn.commit()
        return inserted

    def claim_batch(self, domain: str, limit: int = 200) -> list[dict]:
        """Claim pending rows for a domain (pending -> in_progress)."""
        now = _utc_now_iso()
        with self._lock:
            selected = self._conn.execute(
                """
                SELECT url, domain, seed_id, depth, parent_url, anchor_text,
                       scope_rule, parent_topical, attempts
                FROM frontier
                WHERE domain = ? AND status = 'pending'
                ORDER BY depth ASC, rowid ASC
                LIMIT ?
                """,
                (domain, limit),
            ).fetchall()
            if not selected:
                return []
            urls = [row["url"] for row in selected]
            placeholders = ",".join("?" for _ in urls)
            self._conn.execute(
                f"""
                UPDATE frontier
                SET status = 'in_progress', updated_at = ?
                WHERE url IN ({placeholders}) AND status = 'pending'
                """,
                [now, *urls],
            )
            self._conn.commit()
            # Re-read claimed rows (only those still in_progress by us).
            claimed = self._conn.execute(
                f"""
                SELECT url, domain, seed_id, depth, parent_url, anchor_text,
                       scope_rule, parent_topical, attempts
                FROM frontier
                WHERE url IN ({placeholders}) AND status = 'in_progress'
                """,
                urls,
            ).fetchall()
            return [dict(row) for row in claimed]

    def mark_frontier(
        self,
        url: str,
        status: str,
        reason: str | None = None,
        *,
        bump_attempts: bool = False,
    ) -> None:
        now = _utc_now_iso()
        with self._lock:
            if bump_attempts:
                self._conn.execute(
                    """
                    UPDATE frontier
                    SET status = ?, last_reason = ?, updated_at = ?,
                        attempts = attempts + 1
                    WHERE url = ?
                    """,
                    (status, reason, now, url),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE frontier
                    SET status = ?, last_reason = ?, updated_at = ?
                    WHERE url = ?
                    """,
                    (status, reason, now, url),
                )
            self._conn.commit()

    def requeue_frontier(self, url: str, reason: str | None = None) -> int:
        """Put URL back to pending and bump attempts. Returns new attempts."""
        now = _utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE frontier
                SET status = 'pending', last_reason = ?, updated_at = ?,
                    attempts = attempts + 1
                WHERE url = ?
                """,
                (reason, now, url),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT attempts FROM frontier WHERE url = ?",
                (url,),
            ).fetchone()
            return int(row["attempts"]) if row else 0

    def reset_in_progress(self) -> int:
        now = _utc_now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE frontier
                SET status = 'pending', updated_at = ?
                WHERE status = 'in_progress'
                """,
                (now,),
            )
            self._conn.commit()
            return cursor.rowcount

    def frontier_domain_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT domain, COUNT(*) AS n FROM frontier GROUP BY domain"
            ).fetchall()
        return {row["domain"]: int(row["n"]) for row in rows}

    def pending_domains(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT domain FROM frontier
                WHERE status = 'pending'
                ORDER BY domain
                """
            ).fetchall()
        return [row["domain"] for row in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM frontier WHERE status = 'pending'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def has_pending_domain(self, domain: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM frontier WHERE domain = ? AND status = 'pending' LIMIT 1",
                (domain,),
            ).fetchone()
        return row is not None

    def revisit_stale(self, days: int) -> int:
        """Re-queue done frontier rows whose page fetched_at is older than N days."""
        now = _utc_now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE frontier
                SET status = 'pending', updated_at = ?, last_reason = 'revisit'
                WHERE status = 'done'
                  AND url IN (
                    SELECT p.url FROM pages p
                    WHERE p.fetched_at IS NOT NULL
                      AND julianday('now') - julianday(p.fetched_at) >= ?
                  )
                """,
                (now, float(days)),
            )
            self._conn.commit()
            return cursor.rowcount

    def text_bytes_total(self, crawler_source: str | None = None) -> int:
        with self._lock:
            if crawler_source is None:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(text_bytes), 0) AS n FROM pages"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(text_bytes), 0) AS n FROM pages "
                    "WHERE crawler_source = ?",
                    (crawler_source,),
                ).fetchone()
        return int(row["n"]) if row else 0

    def pages_count(self, crawler_source: str | None = None) -> int:
        with self._lock:
            if crawler_source is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM pages"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM pages WHERE crawler_source = ?",
                    (crawler_source,),
                ).fetchone()
        return int(row["n"]) if row else 0

    def pages_count_by_domain(self, crawler_source: str) -> dict[str, int]:
        """Return {domain: page_count} for pages of a crawler_source (host from url)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT url FROM pages WHERE crawler_source = ?",
                (crawler_source,),
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            domain = _domain_from_url(row["url"])
            counts[domain] = counts.get(domain, 0) + 1
        return counts

    def clear_frontier(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM frontier")
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
