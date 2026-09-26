from __future__ import annotations

import pytest

from crawler.storage import Repository


@pytest.fixture
def repo(tmp_path):
    """Fresh SQLite DB and text root under pytest tmp_path."""
    db_path = tmp_path / "crawl.db"
    text_root = tmp_path / "repo"
    r = Repository(str(db_path), str(text_root))
    yield r
    r.close()
