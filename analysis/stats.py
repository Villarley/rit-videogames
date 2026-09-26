from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

GB = 1024**3
TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

STOPWORDS: frozenset[str] = frozenset(
    w.lower()
    for w in """
    a al algo algunas algunos ante aquel aquella aquellas aquello aquellos aqui
    arriba atras bajo bien cada como con contra cual cuando de del desde donde
    dos e el ella ellas ellos en entre era eramos eran eras es esa esas ese eso esos
    esta estaba estaban estamos estan este esto estos fin fue fuera fueron fui
    ha habia habian hace hacia han hasta hay he hemos hoy hubo la las le lo los
    mas me mi mis mucho muy nada ni no nos nosotros nunca o os otro para pero
    poco por porque que quien se ser si sin sobre so solamente solo somos son soy
    su sus tambien te ti tiene tienen todo tu tus u un una uno usted ustedes y ya yo
    the a an and are as at be been being but by could did do does doing for
    from had has have having he her here hers herself him himself his how i if in
    into is it its itself me more most my myself no nor not of off on once only
    or other our ours ourselves out over own same she should so some such than
    that their theirs them themselves then there these they this those through to
    too under until up very was we were what when where which while who whom why
    with would you your yours yourself yourselves
    """.split()
)

_MIN_NO_STOPWORD_LEN = 3


def _keep_for_no_stopwords(word: str) -> bool:
    return len(word) >= _MIN_NO_STOPWORD_LEN and word not in STOPWORDS


def connect_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def domain_from_url(url: str) -> str:
    return (urlparse(url).hostname or urlparse(url).netloc or "unknown").lower()


def tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


@dataclass
class AggBucket:
    docs: int = 0
    words: int = 0
    bytes: int = 0

    def add(self, words: int, nbytes: int) -> None:
        self.docs += 1
        self.words += words
        self.bytes += nbytes


@dataclass
class ChunkResult:
    counter: Counter[str] = field(default_factory=Counter)
    total_words: int = 0
    docs: int = 0
    bytes_on_disk: int = 0
    missing: int = 0
    by_source: dict[str, AggBucket] = field(default_factory=dict)
    by_language: dict[str, AggBucket] = field(default_factory=dict)
    by_domain: dict[str, AggBucket] = field(default_factory=dict)


def _process_chunk(
    args: tuple[str, Sequence[tuple[str, str | None, str | None, str]]],
) -> ChunkResult:
    text_root, rows = args
    root = Path(text_root)
    out = ChunkResult()
    for text_path, crawler_source, language, url in rows:
        src = crawler_source or "(null)"
        lang = language or "(null)"
        dom = domain_from_url(url)
        abs_path = root / text_path
        if not abs_path.is_file():
            out.missing += 1
            continue
        try:
            nbytes = os.path.getsize(abs_path)
            with abs_path.open("r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            out.missing += 1
            continue
        tokens = tokenize(text)
        nwords = len(tokens)
        out.counter.update(tokens)
        out.total_words += nwords
        out.docs += 1
        out.bytes_on_disk += nbytes
        for key, bucket_map in (
            (src, out.by_source),
            (lang, out.by_language),
            (dom, out.by_domain),
        ):
            if key not in bucket_map:
                bucket_map[key] = AggBucket()
            bucket_map[key].add(nwords, nbytes)
    return out


def _merge_bucket(
    target: dict[str, AggBucket], source: dict[str, AggBucket]
) -> None:
    for key, bucket in source.items():
        if key not in target:
            target[key] = AggBucket()
        target[key].docs += bucket.docs
        target[key].words += bucket.words
        target[key].bytes += bucket.bytes


def _bucket_to_dict(buckets: dict[str, AggBucket]) -> dict[str, dict[str, int]]:
    return {
        k: {"docs": v.docs, "words": v.words, "bytes": v.bytes}
        for k, v in sorted(buckets.items())
    }


def _iter_page_rows(
    conn: sqlite3.Connection,
    source: str | None,
    limit: int | None,
) -> Iterator[tuple[str, str | None, str | None, str]]:
    sql = (
        "SELECT text_path, crawler_source, language, url FROM pages "
        "WHERE text_path IS NOT NULL AND text_path != ''"
    )
    params: list[Any] = []
    if source is not None:
        sql += " AND crawler_source = ?"
        params.append(source)
    sql += " ORDER BY rowid"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            break
        for row in batch:
            yield (row[0], row[1], row[2], row[3])


def _chunk_tasks(
    db_path: str,
    text_root: str,
    source: str | None,
    limit: int | None,
    chunk_size: int,
) -> Iterator[tuple[str, list[tuple[str, str | None, str | None, str]]]]:
    conn = connect_ro(db_path)
    try:
        chunk: list[tuple[str, str | None, str | None, str]] = []
        for row in _iter_page_rows(conn, source, limit):
            chunk.append(row)
            if len(chunk) >= chunk_size:
                yield (text_root, chunk)
                chunk = []
        if chunk:
            yield (text_root, chunk)
    finally:
        conn.close()


def export_metadata_csv_gz(conn: sqlite3.Connection, out_path: Path) -> None:
    cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(pages)").fetchall()
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", newline="") as gz:
        writer = csv.writer(gz)
        writer.writerow(cols)
        cur = conn.execute(f"SELECT {', '.join(cols)} FROM pages")
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            writer.writerows(rows)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _write_plots(
    out_dir: Path,
    word_counter: Counter[str],
    domain_buckets: dict[str, AggBucket],
) -> None:
    freqs = sorted(word_counter.values(), reverse=True)
    if freqs:
        ranks = list(range(1, len(freqs) + 1))
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.loglog(ranks, freqs, ".", markersize=2, alpha=0.5, label="Corpus")
        f1 = freqs[0]
        ref = [f1 / r for r in ranks]
        ax.loglog(ranks, ref, "--", color="gray", label="Referencia pendiente -1")
        ax.set_title("Curva de frecuencia de palabras (Zipf) - repositorio completo")
        ax.set_xlabel("Rango (log)")
        ax.set_ylabel("Frecuencia (log)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "zipf.png", dpi=120)
        plt.close(fig)

    no_sw = Counter(
        {w: c for w, c in word_counter.items() if _keep_for_no_stopwords(w)}
    )
    top30 = no_sw.most_common(30)
    if top30:
        words, counts = zip(*reversed(top30))
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.barh(range(len(words)), counts)
        ax.set_yticks(range(len(words)))
        ax.set_yticklabels(words)
        ax.set_xlabel("Frecuencia")
        ax.set_title("Top 30 palabras (sin stopwords)")
        fig.tight_layout()
        fig.savefig(out_dir / "top_words.png", dpi=120)
        plt.close(fig)

    top_dom = sorted(
        domain_buckets.items(), key=lambda x: x[1].bytes, reverse=True
    )[:20]
    if top_dom:
        labels = [d[0] for d in reversed(top_dom)]
        gbs = [d[1].bytes / GB for d in reversed(top_dom)]
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.barh(range(len(labels)), gbs)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels)
        ax.set_xlabel("GB en disco")
        ax.set_title("Top 20 dominios por volumen")
        fig.tight_layout()
        fig.savefig(out_dir / "domains.png", dpi=120)
        plt.close(fig)


def run_stats(
    db_path: str,
    text_root: str,
    out_dir: str,
    workers: int,
    source: str | None,
    limit: int | None,
    export_metadata: bool,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    conn = connect_ro(db_path)
    try:
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE text_path IS NOT NULL AND text_path != ''"
            + (" AND crawler_source = ?" if source else ""),
            ([source] if source else []),
        ).fetchone()[0]
        if limit is not None:
            total_rows = min(total_rows, limit)

        if export_metadata:
            export_metadata_csv_gz(conn, out / "metadata.csv.gz")

        db_bytes = os.path.getsize(db_path)
    finally:
        conn.close()

    if total_rows == 0:
        print("No hay documentos que procesar.", file=sys.stderr)
        return {}

    chunk_size = max(50, total_rows // (workers * 4) or 1)
    tasks = _chunk_tasks(db_path, text_root, source, limit, chunk_size)

    merged = Counter()
    by_source: dict[str, AggBucket] = {}
    by_language: dict[str, AggBucket] = {}
    by_domain: dict[str, AggBucket] = {}
    total_words = 0
    docs_done = 0
    bytes_on_disk = 0
    missing = 0
    next_pct = 5
    processed_docs = 0
    processed_bytes = 0

    with Pool(processes=workers) as pool:
        for result in pool.imap_unordered(_process_chunk, tasks, chunksize=1):
            merged.update(result.counter)
            total_words += result.total_words
            docs_done += result.docs
            bytes_on_disk += result.bytes_on_disk
            missing += result.missing
            _merge_bucket(by_source, result.by_source)
            _merge_bucket(by_language, result.by_language)
            _merge_bucket(by_domain, result.by_domain)

            processed_docs += result.docs + result.missing
            processed_bytes += result.bytes_on_disk
            pct = int(100 * processed_docs / max(total_rows, 1))
            if pct >= next_pct or processed_docs >= total_rows:
                elapsed = time.perf_counter() - t0
                rate = processed_docs / elapsed if elapsed > 0 else 0.0
                eta = (total_rows - processed_docs) / rate if rate > 0 else 0.0
                print(
                    f"  {pct}%  docs={processed_docs}/{total_rows}  "
                    f"GB={processed_bytes / GB:.3f}  "
                    f"elapsed={elapsed:.1f}s  ETA={eta:.1f}s"
                )
                while next_pct <= pct:
                    next_pct += 5

    distinct_words = len(merged)
    avg_words = total_words / docs_done if docs_done else 0.0
    elapsed = time.perf_counter() - t0

    top_words = merged.most_common(50)
    no_sw_counter = Counter(
        {w: c for w, c in merged.items() if _keep_for_no_stopwords(w)}
    )
    top_words_no_sw = no_sw_counter.most_common(50)
    top_domains = sorted(by_domain.items(), key=lambda x: x[1].bytes, reverse=True)[
        :20
    ]

    stats: dict[str, Any] = {
        "db_path": db_path,
        "text_root": text_root,
        "source_filter": source,
        "limit": limit,
        "elapsed_seconds": round(elapsed, 2),
        "db_bytes": db_bytes,
        "text_bytes_on_disk": bytes_on_disk,
        "missing_files": missing,
        "documents": docs_done,
        "total_words": total_words,
        "distinct_words": distinct_words,
        "average_words_per_document": round(avg_words, 2),
        "by_crawler_source": _bucket_to_dict(by_source),
        "by_language": _bucket_to_dict(by_language),
        "by_domain": _bucket_to_dict(by_domain),
        "top_50_words": top_words,
        "top_50_words_no_stopwords": top_words_no_sw,
    }

    with (out / "stats.json").open("w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)

    with (out / "word_freq_top1000.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rank", "word", "freq"])
        for rank, (word, freq) in enumerate(merged.most_common(1000), start=1):
            writer.writerow([rank, word, freq])

    md_parts = [
        "## Resumen general\n",
        _md_table(
            ["Metrica", "Valor"],
            [
                ["Documentos", str(docs_done)],
                ["Palabras totales", str(total_words)],
                ["Palabras distintas", str(distinct_words)],
                ["Promedio palabras/doc", f"{avg_words:.2f}"],
                ["Bytes texto en disco", str(bytes_on_disk)],
                ["GB texto en disco", f"{bytes_on_disk / GB:.4f}"],
                ["Bytes base de datos", str(db_bytes)],
                ["Archivos faltantes", str(missing)],
            ],
        ),
        "\n## Por arañador\n",
        _md_table(
            ["crawler_source", "docs", "words", "bytes", "GB"],
            [
                [
                    k,
                    str(v.docs),
                    str(v.words),
                    str(v.bytes),
                    f"{v.bytes / GB:.4f}",
                ]
                for k, v in sorted(by_source.items())
            ],
        ),
        "\n## Por idioma\n",
        _md_table(
            ["language", "docs", "words", "bytes", "GB"],
            [
                [
                    k,
                    str(v.docs),
                    str(v.words),
                    str(v.bytes),
                    f"{v.bytes / GB:.4f}",
                ]
                for k, v in sorted(by_language.items())
            ],
        ),
        "\n## Top 20 dominios\n",
        _md_table(
            ["domain", "docs", "words", "bytes", "GB"],
            [
                [
                    d,
                    str(b.docs),
                    str(b.words),
                    str(b.bytes),
                    f"{b.bytes / GB:.4f}",
                ]
                for d, b in top_domains
            ],
        ),
        "\n## Top 50 palabras\n",
        _md_table(
            ["palabra", "freq"],
            [[w, str(c)] for w, c in top_words],
        ),
        "\n## Top 50 palabras sin stopwords\n",
        _md_table(
            ["palabra", "freq"],
            [[w, str(c)] for w, c in top_words_no_sw],
        ),
    ]
    (out / "stats.md").write_text("\n".join(md_parts), encoding="utf-8")

    _write_plots(out, merged, by_domain)

    print(
        f"\nListo en {elapsed:.1f}s: {docs_done} docs, "
        f"{total_words} palabras, {distinct_words} tipos, "
        f"{bytes_on_disk / GB:.3f} GB texto, {missing} faltantes. "
        f"Salida: {out.resolve()}"
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estadisticas del repositorio (tokenizacion y reportes)."
    )
    parser.add_argument("--db", default="repository/crawl.db")
    parser.add_argument("--text-root", default="repository")
    parser.add_argument("--out", default="analysis/output")
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
    )
    parser.add_argument(
        "--source",
        choices=("custom", "scrapy"),
        default=None,
        help="Filtrar por crawler_source",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Procesar solo N documentos (pruebas)",
    )
    parser.add_argument(
        "--export-metadata",
        action="store_true",
        help="Exportar tabla pages a metadata.csv.gz",
    )
    args = parser.parse_args(argv)
    try:
        run_stats(
            args.db,
            args.text_root,
            args.out,
            args.workers,
            args.source,
            args.limit,
            args.export_metadata,
        )
    except sqlite3.OperationalError as exc:
        print(f"Error SQLite: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
