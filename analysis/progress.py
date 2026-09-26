from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
GB = 1024**3
MIN_GB = 10.0
GOAL_GB = 30.0

# Host from URL in SQLite (http/https only).
_DOMAIN_SQL = """
lower(
  CASE
    WHEN instr(url, '://') = 0 THEN 'unknown'
    WHEN instr(substr(url, instr(url, '://') + 3), '/') > 0 THEN
      substr(
        substr(url, instr(url, '://') + 3),
        1,
        instr(substr(url, instr(url, '://') + 3), '/') - 1
      )
    ELSE substr(url, instr(url, '://') + 3)
  END
)
"""


def connect_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _fmt_gb(n_bytes: int | float) -> str:
    return f"{float(n_bytes) / GB:.3f}"


def _fmt_pct(part: float, whole: float) -> str:
    if whole <= 0:
        return "0.0%"
    return f"{100.0 * part / whole:.1f}%"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(sep)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def run(db_path: str) -> None:
    conn = connect_ro(db_path)
    try:
        now = datetime.now(timezone.utc)
        print(f"Progreso del repositorio  ({db_path})")
        print(f"Consulta: {now.isoformat()}\n")

        source_rows = conn.execute(
            """
            SELECT
              COALESCE(crawler_source, '(null)') AS src,
              COUNT(*) AS pages,
              COALESCE(SUM(text_bytes), 0) AS total_bytes,
              SUM(CASE WHEN julianday(fetched_at) >= julianday('now', '-10 minutes')
                  THEN 1 ELSE 0 END) AS pages_10m,
              SUM(CASE WHEN julianday(fetched_at) >= julianday('now', '-60 minutes')
                  THEN 1 ELSE 0 END) AS pages_60m,
              COALESCE(SUM(CASE WHEN julianday(fetched_at) >= julianday('now', '-60 minutes')
                  THEN text_bytes ELSE 0 END), 0) AS bytes_60m
            FROM pages
            GROUP BY crawler_source
            ORDER BY total_bytes DESC
            """
        ).fetchall()

        total_bytes = sum(int(r["total_bytes"]) for r in source_rows)
        total_pages = sum(int(r["pages"]) for r in source_rows)

        table: list[list[str]] = []
        for r in source_rows:
            b60 = int(r["bytes_60m"])
            gb_h = (b60 / GB)  # bytes in last hour ~= GB per hour
            table.append(
                [
                    str(r["src"]),
                    str(r["pages"]),
                    _fmt_gb(int(r["total_bytes"])),
                    str(int(r["pages_10m"])),
                    str(int(r["pages_60m"])),
                    f"{gb_h:.3f}",
                ]
            )
        _print_table(
            [
                "crawler_source",
                "pages",
                "text_GB",
                "pages_10m",
                "pages_60m",
                "GB/h (60m)",
            ],
            table,
        )

        total_gb = total_bytes / GB
        print()
        print(
            f"Total: {total_pages} pages, {total_gb:.3f} GB text "
            f"(minimo {MIN_GB} GB: {_fmt_pct(total_gb, MIN_GB)}; "
            f"meta {GOAL_GB} GB: {_fmt_pct(total_gb, GOAL_GB)})"
        )

        print("\nTop 15 dominios por volumen de texto:")
        domain_rows = conn.execute(
            f"""
            SELECT
              {_DOMAIN_SQL} AS domain,
              COUNT(*) AS pages,
              COALESCE(SUM(text_bytes), 0) AS total_bytes
            FROM pages
            GROUP BY domain
            ORDER BY total_bytes DESC
            LIMIT 15
            """
        ).fetchall()
        dom_table: list[list[str]] = []
        for r in domain_rows:
            tb = int(r["total_bytes"])
            dom_table.append(
                [
                    str(r["domain"]),
                    str(r["pages"]),
                    _fmt_gb(tb),
                    _fmt_pct(tb, total_bytes),
                ]
            )
        _print_table(["domain", "pages", "text_GB", "share"], dom_table)

        print("\nIdioma (pages.language):")
        lang_rows = conn.execute(
            """
            SELECT COALESCE(language, '(null)') AS lang, COUNT(*) AS n
            FROM pages
            GROUP BY language
            ORDER BY n DESC
            """
        ).fetchall()
        _print_table(
            ["language", "pages"],
            [[str(r["lang"]), str(r["n"])] for r in lang_rows],
        )

        if _table_exists(conn, "frontier"):
            print("\nFrontier (conteo por status):")
            fr_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM frontier
                GROUP BY status
                ORDER BY n DESC
                """
            ).fetchall()
            _print_table(
                ["status", "count"],
                [[str(r["status"]), str(r["n"])] for r in fr_rows],
            )
        else:
            print("\nFrontier: tabla no presente.")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estado rapido del repositorio de crawl (solo lectura)."
    )
    parser.add_argument(
        "--db",
        default="repository/crawl.db",
        help="Ruta a crawl.db (default: repository/crawl.db)",
    )
    args = parser.parse_args(argv)
    try:
        run(args.db)
    except sqlite3.OperationalError as exc:
        print(f"Error SQLite: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
