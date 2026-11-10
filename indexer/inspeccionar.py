"""Muestra lo que hay dentro de un .idx, con los offsets para seguirlo en hexdump.

    python -m indexer.inspeccionar indices/completo.idx                # resumen
    python -m indexer.inspeccionar indices/completo.idx --termino zelda
    python -m indexer.inspeccionar indices/completo.idx --doc 0
    python -m indexer.inspeccionar indices/completo.idx --top 20       # mayor df

Para cada cosa imprime el comando de hexdump que muestra esos mismos bytes,
por ejemplo: hexdump -C -s 1234 -n 24 indices/completo.idx
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

from indexer import formato as F


def _hex(ruta, offset: int, n: int) -> str:
    return f"hexdump -C -s {offset} -n {n} {ruta}"


def resumen(ix: F.IndiceBinario) -> None:
    print(f"archivo      {ix.ruta}  ({ix.ruta.stat().st_size:,} bytes)")
    print(f"documentos   {ix.N:,}")
    print(f"vocabulario  {ix.V:,} términos")
    print(f"tokens       {ix.tokens:,}")
    print(f"banderas     0b{ix.banderas:08b}  (plegado, minúsculas, fechas, números, stopwords, tesauro, nombres, porter)")
    print("secciones:")
    for nombre, s in ix.secciones.items():
        print(f"  {nombre}  datos en {s.offset:>12,}  largo {s.largo:>14,}   {_hex(ix.ruta, s.offset - 16, 64)}")
    print("encabezado:   " + _hex(ix.ruta, 0, 128))
    print("política embebida:")
    for linea in ix.conf.splitlines():
        print("  " + linea)


def termino(ix: F.IndiceBinario, t: str, mostrar: int) -> None:
    i = ix.buscar(t)
    if i < 0:
        print(f"'{t}' no está en el vocabulario")
        return
    e = ix.voc[i]
    off_voc = ix.secciones["VOCA"].offset + i * F.VOC.itemsize
    off_term = ix.secciones["TERM"].offset + int(e["term_off"])
    off_post = ix.secciones["POST"].offset + int(e["post_off"])
    fin = ix.secciones["POST"].offset + (int(ix.voc[i + 1]["post_off"]) if i + 1 < ix.V else ix.secciones["POST"].largo)
    print(f"término #{i}: '{t}'")
    print(f"  df={int(e['df']):,}  cf={int(e['cf']):,}  idf={float(e['idf']):.4f}")
    print(f"  registro VOCA  {_hex(ix.ruta, off_voc, 24)}")
    print(f"  texto en TERM  {_hex(ix.ruta, off_term, len(t.encode()) + 1)}")
    print(f"  postings       {_hex(ix.ruta, off_post, min(fin - off_post, 64))}   ({fin - off_post:,} bytes)")
    docs, tfs = ix.postings(i)
    print(f"  primeros {min(mostrar, len(docs))} postings (doc interno, tf, page_id):")
    for d, tf in list(zip(docs, tfs))[:mostrar]:
        print(f"    {int(d):>8}  tf={int(tf):<5} page_id={int(ix.docs[d]['page_id'])}")


def documento(ix: F.IndiceBinario, d: int) -> None:
    r = ix.docs[d]
    off = ix.secciones["DOCS"].offset + d * F.DOC.itemsize
    print(f"documento interno {d}: page_id={int(r['page_id'])} largo={int(r['largo'])} "
          f"distintos={int(r['distintos'])} norma={float(r['norma']):.4f}")
    print("  " + _hex(ix.ruta, off, 16))


def top(ix: F.IndiceBinario, n: int) -> None:
    orden = np.argsort(-ix.voc["df"].astype(np.int64))[:n]
    for i in orden:
        e = ix.voc[i]
        print(f"  {ix.termino(int(i)):<25} df={int(e['df']):>10,}  cf={int(e['cf']):>12,}  idf={float(e['idf']):.3f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("indice")
    ap.add_argument("--termino")
    ap.add_argument("--doc", type=int)
    ap.add_argument("--top", type=int)
    ap.add_argument("--postings", type=int, default=10)
    ap.add_argument("--volcar", action="store_true", help="término TAB df TAB cf de todo el vocabulario")
    a = ap.parse_args(argv)
    ix = F.IndiceBinario(a.indice)
    if a.volcar:
        out = sys.stdout
        for i, t in enumerate(ix.terminos()):
            e = ix.voc[i]
            out.write(f"{t}\t{int(e['df'])}\t{int(e['cf'])}\n")
    elif a.termino:
        termino(ix, a.termino, a.postings)
    elif a.doc is not None:
        documento(ix, a.doc)
    elif a.top:
        top(ix, a.top)
    else:
        resumen(ix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
