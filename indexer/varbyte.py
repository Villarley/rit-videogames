"""Compresión variable byte de los postings (Manning, Raghavan y Schütze 2008, 5.3).

Cada número se parte en grupos de 7 bits, del menos significativo al más
significativo. El bit alto de cada byte dice si viene otro byte (1) o si ese
es el último (0). Es el mismo esquema de LEB128 que usan DWARF y protobuf, y
el mismo que usa Lucene para sus VInt. Como los documentos se guardan como
distancias (gaps) y casi todas son chicas, la mayoría ocupa un solo byte.

Ejemplo: 300 = 0b10_0101100 -> bytes AC 02.
"""
from __future__ import annotations

import numpy as np


def codificar(n: int, salida: bytearray) -> None:
    while n >= 0x80:
        salida.append((n & 0x7F) | 0x80)
        n >>= 7
    salida.append(n)


def codificar_uno(n: int) -> bytes:
    b = bytearray()
    codificar(n, b)
    return bytes(b)


def primero(buf: bytes | bytearray | memoryview) -> tuple[int, int]:
    """Decodifica el primer número. Devuelve (valor, bytes que ocupó)."""
    valor = 0
    corrimiento = 0
    i = 0
    while True:
        b = buf[i]
        valor |= (b & 0x7F) << corrimiento
        i += 1
        if b < 0x80:
            return valor, i
        corrimiento += 7


def decodificar(buf: np.ndarray) -> np.ndarray:
    """Decodifica un arreglo de bytes completo de una vez, con numpy.

    1. Los bytes < 0x80 son los finales de cada número.
    2. Con eso sé dónde empieza cada número y en qué posición está cada byte
       dentro de su número.
    3. Cada byte aporta (b & 0x7F) << (7 * posición) y reduceat los suma por
       número.
    """
    if buf.size == 0:
        return np.zeros(0, dtype=np.int64)
    fines = np.flatnonzero(buf < 0x80)
    inicios = np.empty_like(fines)
    inicios[0] = 0
    inicios[1:] = fines[:-1] + 1
    largos = fines - inicios + 1
    if int(largos.max()) == 1:
        return buf[fines].astype(np.int64)
    pos = np.arange(buf.size, dtype=np.int64) - np.repeat(inicios, largos)
    valores = (buf & 0x7F).astype(np.int64) << (7 * pos)
    return np.add.reduceat(valores, inicios)


def decodificar_postings(buf: np.ndarray, df: int) -> tuple[np.ndarray, np.ndarray]:
    """Postings de un término: df códigos (distancia << 1 | tf==1) y luego los tf > 1."""
    v = decodificar(buf)
    cod = v[:df]
    docs = np.cumsum(cod >> 1)
    tfs = np.ones(df, dtype=np.int64)
    multiples = (cod & 1) == 0
    tfs[multiples] = v[df:]
    return docs, tfs


def decodificar_lote(buf: np.ndarray, inicios_bytes: np.ndarray, dfs: np.ndarray):
    """Lo mismo pero para varios términos seguidos de una vez (para las normas).

    Devuelve docs, tf y a qué término (0..k-1) pertenece cada posting.
    """
    v = decodificar(buf)
    fines = np.flatnonzero(buf < 0x80)
    # índice (en números, no en bytes) donde empieza cada término
    ini = np.searchsorted(fines, inicios_bytes)
    dfs = dfs.astype(np.int64)
    k = len(dfs)
    quien = np.repeat(np.arange(k), dfs)
    base_cod = np.cumsum(dfs) - dfs
    pos_cod = np.repeat(ini, dfs) + (np.arange(int(dfs.sum())) - np.repeat(base_cod, dfs))
    es_cod = np.zeros(v.size, dtype=bool)
    es_cod[pos_cod] = True
    cod = v[pos_cod]
    gaps = cod >> 1
    acum = np.cumsum(gaps)
    base = acum[base_cod] - gaps[base_cod]
    docs = acum - base[quien]
    tfs = np.ones(cod.size, dtype=np.int64)
    tfs[(cod & 1) == 0] = v[~es_cod]
    return docs, tfs, quien
