"""Indexador propio: recorre la colección y escribe el índice TF-IDF en un .idx.

Uso:
    python -m indexer.construir config/completo.conf
    python -m indexer.construir config/completo.conf --limite 5000   # prueba corta

Es un programa aparte de la araña: solo lee repository/crawl.db (para saber qué
documentos hay y su page_id) y los .txt que dejó el arañador.

Cómo funciona (SPIMI, Heinz y Zobel 2003; Manning et al. 2008, cap. 4.3):

1. Se leen los documentos de crawl.db en orden de id y se reparten en bloques
   de "docs_por_bloque" documentos. A cada documento se le da un número interno
   (0, 1, 2, ...) en ese orden.
2. Cada proceso toma un bloque, analiza sus documentos con la política del
   .conf y arma en memoria el diccionario término -> postings ya comprimidos en
   variable byte. Al terminar el bloque lo escribe ordenado en un archivo
   temporal. Así la memoria depende del tamaño del bloque y no de la colección.
3. Se mezclan todos los bloques en orden alfabético (heapq.merge). Como los
   bloques cubren rangos de documentos consecutivos, las listas de un término
   se pueden pegar una detrás de otra: solo hay que recalcular la primera
   distancia de cada pedazo, sin descomprimir el resto.
4. Con el df final se calcula el idf de cada término y, en una pasada con numpy
   sobre los postings, la norma de cada documento.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import time
from array import array
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from indexer import formato as F
from indexer.config import Configuracion, leer_configuracion
from indexer.normalizacion import Analizador
from indexer.varbyte import codificar, codificar_uno, decodificar_lote, primero

REGISTRO_BLOQUE = struct.Struct("<IIIIII")  # df, cf, primer doc, último doc, bytes códigos, bytes tf

# --- 1. lista de documentos ---------------------------------------------------


def listar_documentos(base: Path, limite: int | None = None) -> list[tuple[int, str]]:
    conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    try:
        sql = (
            "SELECT id, text_path FROM pages "
            "WHERE text_path IS NOT NULL AND text_path != '' ORDER BY id"
        )
        if limite:
            sql += f" LIMIT {int(limite)}"
        return [(int(i), p) for i, p in conn.execute(sql)]
    finally:
        conn.close()


# --- 2. un bloque por proceso -------------------------------------------------

_analizador: Analizador | None = None
_conf: Configuracion | None = None


def _iniciar(ruta_conf: str) -> None:
    global _analizador, _conf
    _conf = leer_configuracion(ruta_conf)
    _analizador = Analizador(_conf.politica)


def procesar_bloque(tarea: tuple[int, int, list[tuple[int, str]]]) -> dict:
    numero, primer_doc, docs = tarea
    assert _conf is not None and _analizador is not None
    raiz = _conf.raiz_coleccion
    analizar = _analizador.analizar
    # término -> [códigos, tf > 1, último doc, df, cf, primer doc]
    postings: dict[str, list] = {}
    largos = array("I")
    distintos = array("I")
    leidos = 0
    faltantes = 0
    t0 = time.perf_counter()
    for k, (_page_id, ruta_rel) in enumerate(docs):
        doc = primer_doc + k
        try:
            with open(raiz / ruta_rel, "rb") as fh:
                crudo = fh.read()
        except OSError:
            faltantes += 1
            largos.append(0)
            distintos.append(0)
            continue
        leidos += len(crudo)
        terminos = analizar(crudo.decode("utf-8", errors="replace"))
        cuenta = Counter(terminos)
        largos.append(len(terminos))
        distintos.append(len(cuenta))
        for t, tf in cuenta.items():
            # Código de cada posting: (distancia << 1) | (tf == 1). Si tf es 1
            # (la mayoría) no se guarda nada más; si no, el tf va aparte, en la
            # lista de tf del término. Es el mismo truco de Lucene (formato
            # Lucene50, "DocDelta" con el bit bajo para freq == 1).
            e = postings.get(t)
            if e is None:
                cod = bytearray()
                tfs = bytearray()
                codificar((doc << 1) | (tf == 1), cod)  # la primera distancia es el doc absoluto
                if tf != 1:
                    codificar(tf, tfs)
                postings[t] = [cod, tfs, doc, 1, tf, doc]
            else:
                codificar(((doc - e[2]) << 1) | (tf == 1), e[0])
                if tf != 1:
                    codificar(tf, e[1])
                e[2] = doc
                e[3] += 1
                e[4] += tf
    t_analisis = time.perf_counter() - t0

    ruta = _conf.temporal / f"bloque_{numero:05d}.bin"
    with open(ruta, "wb") as out:
        for t in sorted(postings, key=lambda s: s.encode("utf-8")):
            cod, tfs, ultimo, df, cf, primero_doc = postings[t]
            tb = t.encode("utf-8")
            out.write(struct.pack("<H", len(tb)))
            out.write(tb)
            out.write(REGISTRO_BLOQUE.pack(df, cf, primero_doc, ultimo, len(cod), len(tfs)))
            out.write(cod)
            out.write(tfs)
    return {
        "numero": numero,
        "ruta": str(ruta),
        "largos": largos.tobytes(),
        "distintos": distintos.tobytes(),
        "bytes_texto": leidos,
        "faltantes": faltantes,
        "terminos_bloque": len(postings),
        "segundos_analisis": t_analisis,
        "segundos_total": time.perf_counter() - t0,
    }


# --- 3. mezcla ------------------------------------------------------------------


def _leer_bloque(ruta: str, numero: int):
    with open(ruta, "rb", buffering=1 << 20) as fh:
        leer = fh.read
        while True:
            cab = leer(2)
            if not cab:
                return
            (n,) = struct.unpack("<H", cab)
            termino = leer(n)
            df, cf, primer, ultimo, ncod, ntf = REGISTRO_BLOQUE.unpack(leer(REGISTRO_BLOQUE.size))
            yield termino, numero, df, cf, primer, ultimo, leer(ncod), leer(ntf)


def _escribir_seccion(out, etiqueta: str, elementos: int, datos: bytes | memoryview) -> tuple[int, int]:
    out.write(F.CABECERA_SECCION.pack(etiqueta.encode(), elementos, len(datos)))
    offset = out.tell()
    out.write(datos)
    out.write(b"\0" * F.relleno(len(datos)))
    return offset, len(datos)


def construir(conf: Configuracion, limite: int | None = None, procesos: int | None = None,
              registrar=print) -> dict:
    tiempos: dict[str, float] = {}
    t_inicio = time.perf_counter()
    procesos = procesos or conf.procesos

    docs = listar_documentos(conf.base, limite)
    N = len(docs)
    if N == 0:
        raise SystemExit(f"No hay documentos con texto en {conf.base}")
    registrar(f"[{conf.politica.nombre}] {N:,} documentos, política {conf.politica.resumen()}, "
              f"{procesos} procesos")

    if conf.temporal.exists():
        shutil.rmtree(conf.temporal)
    conf.temporal.mkdir(parents=True)
    conf.salida.parent.mkdir(parents=True, exist_ok=True)

    tam = conf.docs_por_bloque
    tareas = [(i, ini, docs[ini : ini + tam]) for i, ini in enumerate(range(0, N, tam))]

    # Fase 1: bloques en paralelo
    t0 = time.perf_counter()
    resultados = []
    bytes_texto = 0
    with Pool(procesos, initializer=_iniciar, initargs=(str(conf.archivo),)) as pool:
        for r in pool.imap(procesar_bloque, tareas):
            resultados.append(r)
            bytes_texto += r["bytes_texto"]
            hechos = min(len(resultados) * tam, N)
            seg = time.perf_counter() - t0
            registrar(f"  bloque {r['numero'] + 1}/{len(tareas)}: {hechos:,} docs, "
                      f"{bytes_texto / 2**20:,.0f} MiB, {bytes_texto / 2**20 / max(seg, 1e-9):,.1f} MiB/s")
    tiempos["bloques"] = time.perf_counter() - t0

    largos = np.frombuffer(b"".join(r["largos"] for r in resultados), dtype=np.uint32)
    distintos = np.frombuffer(b"".join(r["distintos"] for r in resultados), dtype=np.uint32)
    total_tokens = int(largos.sum(dtype=np.uint64))

    # Fase 2: mezcla de bloques, escribiendo POST directo al archivo final
    t0 = time.perf_counter()
    term_off = array("I")
    dfs = array("I")
    cfs = array("I")
    post_off = array("Q")
    terminos = bytearray()
    tmp_salida = conf.salida.with_suffix(conf.salida.suffix + ".parcial")
    out = open(tmp_salida, "wb+")
    out.write(b"\0" * F.TAM_ENCABEZADO)
    # Se guarda el .conf sin comentarios: así el índice dice con qué política se
    # hizo y el buscador analiza las consultas igual que los documentos.
    conf_bytes = conf_compacto(conf.texto).encode("utf-8")
    secciones = {"CONF": _escribir_seccion(out, "CONF", 1, conf_bytes)}

    pos_cab_post = out.tell()
    out.write(b"\0" * F.CABECERA_SECCION.size)
    post_ini = out.tell()
    escritos = 0
    lectores = [_leer_bloque(r["ruta"], r["numero"]) for r in resultados]
    actual = None
    ultimo_doc = 0
    df = cf = 0
    escribir = out.write
    pendientes: list[bytes] = []  # listas de tf del término actual, van después de los códigos

    def cerrar_termino() -> None:
        nonlocal escritos
        for p in pendientes:
            escribir(p)
            escritos += len(p)
        pendientes.clear()
        dfs.append(df)
        cfs.append(cf)

    for termino, _n, b_df, b_cf, b_primer, b_ultimo, cod, tfs in heapq.merge(*lectores):
        if termino != actual:
            if actual is not None:
                cerrar_termino()
            actual = termino
            term_off.append(len(terminos))
            terminos += termino
            terminos.append(0)
            post_off.append(escritos)
            df = cf = 0
            escribir(cod)
            escritos += len(cod)
        else:
            # El primer código del pedazo trae el doc absoluto; se cambia por
            # la distancia al último doc del pedazo anterior.
            valor, n = primero(cod)
            nueva = codificar_uno(((b_primer - ultimo_doc) << 1) | (valor & 1))
            escribir(nueva)
            escribir(memoryview(cod)[n:])
            escritos += len(nueva) + len(cod) - n
        if tfs:
            pendientes.append(tfs)
        ultimo_doc = b_ultimo
        df += b_df
        cf += b_cf
    if actual is not None:
        cerrar_termino()
    V = len(term_off)
    out.write(b"\0" * F.relleno(escritos))
    fin_post = out.tell()
    out.seek(pos_cab_post)
    out.write(F.CABECERA_SECCION.pack(b"POST", V, escritos))
    out.seek(fin_post)
    secciones["POST"] = (post_ini, escritos)
    tiempos["mezcla"] = time.perf_counter() - t0
    shutil.rmtree(conf.temporal, ignore_errors=True)

    # Fase 3: idf y normas
    t0 = time.perf_counter()
    df_np = np.frombuffer(dfs.tobytes(), dtype=np.uint32)
    idf = np.log2(N / df_np.astype(np.float64)).astype(np.float32)
    post_off_np = np.frombuffer(post_off.tobytes(), dtype=np.uint64).astype(np.int64)
    out.flush()
    normas = calcular_normas(tmp_salida, post_ini, escritos, post_off_np, df_np, idf, N)
    tiempos["normas"] = time.perf_counter() - t0

    # Fase 4: TERM, VOCA, DOCS y encabezado
    t0 = time.perf_counter()
    secciones["TERM"] = _escribir_seccion(out, "TERM", V, terminos)
    voc = np.zeros(V, dtype=F.VOC)
    voc["term_off"] = np.frombuffer(term_off.tobytes(), dtype=np.uint32)
    voc["df"] = df_np
    voc["cf"] = np.frombuffer(cfs.tobytes(), dtype=np.uint32)
    voc["idf"] = idf
    voc["post_off"] = post_off_np
    secciones["VOCA"] = _escribir_seccion(out, "VOCA", V, voc.tobytes())
    tabla_docs = np.zeros(N, dtype=F.DOC)
    tabla_docs["page_id"] = np.array([d[0] for d in docs], dtype=np.uint32)
    tabla_docs["largo"] = largos
    tabla_docs["distintos"] = distintos
    tabla_docs["norma"] = normas
    secciones["DOCS"] = _escribir_seccion(out, "DOCS", N, tabla_docs.tobytes())

    out.seek(0)
    out.write(F.ENCABEZADO.pack(F.MAGICO, F.VERSION, conf.politica.banderas(), N, V, 0, total_tokens))
    plano = []
    for nombre in F.SECCIONES:
        plano.extend(secciones[nombre])
    out.write(F.TABLA.pack(*plano))
    out.write(F.COLA.pack(int(time.time()), 0))
    out.close()
    os.replace(tmp_salida, conf.salida)
    tiempos["escritura"] = time.perf_counter() - t0
    tiempos["total"] = time.perf_counter() - t_inicio

    tam_archivo = conf.salida.stat().st_size
    stats = {
        "indexador": "propio",
        "politica": conf.politica.nombre,
        "normalizacion": conf.politica.resumen(),
        "archivo": str(conf.salida),
        "documentos": N,
        "documentos_sin_archivo": sum(r["faltantes"] for r in resultados),
        "vocabulario": V,
        "tokens": total_tokens,
        "postings": int(df_np.sum(dtype=np.uint64)),
        "bytes_texto": bytes_texto,
        "bytes_indice": tam_archivo,
        "proporcion_indice_texto": tam_archivo / bytes_texto if bytes_texto else None,
        "bytes_por_seccion": {k: v[1] for k, v in secciones.items()},
        "bytes_por_posting": escritos / max(int(df_np.sum(dtype=np.uint64)), 1),
        "largo_promedio_doc": float(largos.mean()) if N else 0.0,
        "procesos": procesos,
        "docs_por_bloque": tam,
        "bloques": len(tareas),
        "segundos": tiempos,
        "mib_por_segundo": bytes_texto / 2**20 / tiempos["total"] if tiempos["total"] else None,
    }
    Path(str(conf.salida) + ".json").write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    registrar(
        f"[{conf.politica.nombre}] listo: {V:,} términos, {stats['postings']:,} postings, "
        f"{tam_archivo / 2**20:,.1f} MiB ({100 * (stats['proporcion_indice_texto'] or 0):.1f}% del texto), "
        f"{tiempos['total']:.1f} s"
    )
    return stats


def conf_compacto(texto: str) -> str:
    lineas = []
    for linea in texto.splitlines():
        linea = linea.split("#", 1)[0].split(";", 1)[0].rstrip()
        if linea.strip():
            lineas.append(linea.strip())
    return "\n".join(lineas) + "\n"


def calcular_normas(ruta: Path, post_ini: int, largo_post: int, post_off: np.ndarray,
                    df: np.ndarray, idf: np.ndarray, N: int, lote_bytes: int = 32 << 20) -> np.ndarray:
    """|d| = raíz de la suma de w², con w = (1 + log2 tf) * idf, como en la clase del modelo vectorial."""
    norma2 = np.zeros(N, dtype=np.float64)
    V = len(df)
    if V == 0:
        return norma2.astype(np.float32)
    fines = np.empty(V, dtype=np.int64)
    fines[:-1] = post_off[1:]
    fines[-1] = largo_post
    mm = np.memmap(ruta, dtype=np.uint8, mode="r", offset=post_ini, shape=(largo_post,))
    i = 0
    while i < V:
        # lote de términos consecutivos que quepa en lote_bytes (al menos uno)
        j = int(np.searchsorted(fines, post_off[i] + lote_bytes, side="right"))
        j = min(max(j, i + 1), V)
        docs, tfs, quien = decodificar_lote(np.asarray(mm[post_off[i] : fines[j - 1]]),
                                            post_off[i:j] - post_off[i], df[i:j])
        w = (1.0 + np.log2(tfs)) * idf[i:j].astype(np.float64)[quien]
        norma2 += np.bincount(docs, weights=w * w, minlength=N)
        i = j
    del mm
    return np.sqrt(norma2).astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Indexador propio (archivo binario .idx)")
    ap.add_argument("conf", nargs="+", help="uno o más .conf (config/basico.conf ...)")
    ap.add_argument("--limite", type=int, help="indexar solo los primeros N documentos")
    ap.add_argument("--procesos", type=int, help="cambia el valor del .conf")
    a = ap.parse_args(argv)
    for c in a.conf:
        construir(leer_configuracion(c), limite=a.limite, procesos=a.procesos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
