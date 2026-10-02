"""Read combined corpus size off the crawling threads, without schema changes."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_TARGET_GB = 10.0


class ProjectTotal:
    def __init__(self, db_path: str, interval: float = 30.0):
        self._uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        self._interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._total = 0
        self._rate = 0.0
        self._initial = None
        self._started_at = None

    def refresh(self):
        conn = sqlite3.connect(self._uri, uri=True, timeout=5)
        try:
            total = int(conn.execute("SELECT COALESCE(SUM(text_bytes), 0) FROM pages").fetchone()[0])
        finally:
            conn.close()
        now = time.monotonic()
        with self._lock:
            if self._initial is None:
                self._initial = total
                self._started_at = now
            elapsed = now - self._started_at
            self._rate = max(0, total - self._initial) / elapsed if elapsed > 0 else 0.0
            self._total = total
        return total

    def snapshot(self):
        with self._lock:
            return self._total, self._rate

    def start(self):
        self.refresh()
        self._thread = threading.Thread(target=self._run, name="project-total", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self.refresh()
            except sqlite3.Error:
                logging.getLogger(__name__).exception("PROJECT_TOTAL_READ_FAILED")

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
