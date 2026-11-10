"""Formato del archivo binario .idx (ver FORMATO_INDICE.md) y su lector.

Todo es little-endian. El archivo tiene un encabezado de 128 bytes y cinco
secciones. Cada sección empieza con 16 bytes propios (etiqueta de 4 letras,
cantidad de elementos y largo en bytes) y termina rellenada con ceros hasta
múltiplo de 16, para que en "hexdump -C" cada sección arranque en una línea
nueva y la etiqueta se lea en la columna de texto.
"""
from __future__ import annotations

import json
import mmap
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from indexer.varbyte import decodificar_postings

MAGICO = b"RITIDX01"
VERSION = 1
TAM_ENCABEZADO = 128
SECCIONES = ("CONF", "POST", "TERM", "VOCA", "DOCS")

# magico, version, banderas, N, V, reservado, tokens
ENCABEZADO = struct.Struct("<8sHHIII Q")
# offset y largo de cada sección (5 x 16 bytes) empiezan en el byte 32
TABLA = struct.Struct("<" + "QQ" * len(SECCIONES))
# fecha de construcción y relleno hasta 128
COLA = struct.Struct("<QQ")
CABECERA_SECCION = struct.Struct("<4sIQ")  # etiqueta, elementos, bytes

# Un registro por documento: page_id de crawl.db, largo (términos), cantidad de
# términos distintos y la norma del vector TF-IDF.
DOC = np.dtype([("page_id", "<u4"), ("largo", "<u4"), ("distintos", "<u4"), ("norma", "<f4")])
# Un registro por término: dónde empieza su texto en TERM, df, frecuencia en la
# colección, idf y dónde empieza su lista de postings en POST.
VOC = np.dtype([("term_off", "<u4"), ("df", "<u4"), ("cf", "<u4"), ("idf", "<f4"), ("post_off", "<u8")])

assert ENCABEZADO.size + TABLA.size + COLA.size == TAM_ENCABEZADO
assert DOC.itemsize == 16 and VOC.itemsize == 24


def relleno(n: int) -> int:
    return (-n) % 16


@dataclass
class Seccion:
    offset: int  # donde empiezan los datos (después de sus 16 bytes)
    largo: int


class IndiceBinario:
    """Abre un .idx con mmap: no se carga a memoria más que el vocabulario."""

    def __init__(self, ruta: str | Path) -> None:
        self.ruta = Path(ruta)
        self._f = open(self.ruta, "rb")
        self.mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        magico, version, banderas, n, v, _res, tokens = ENCABEZADO.unpack_from(self.mm, 0)
        if magico != MAGICO:
            raise ValueError(f"{ruta} no es un índice RIT (mágico {magico!r})")
        if version != VERSION:
            raise ValueError(f"versión {version} no soportada")
        self.banderas = banderas
        self.N = n
        self.V = v
        self.tokens = tokens
        tabla = TABLA.unpack_from(self.mm, ENCABEZADO.size)
        self.secciones = {
            nombre: Seccion(tabla[2 * i], tabla[2 * i + 1])
            for i, nombre in enumerate(SECCIONES)
        }
        self.creado, _ = COLA.unpack_from(self.mm, ENCABEZADO.size + TABLA.size)
        s = self.secciones
        self.conf = bytes(self.mm[s["CONF"].offset : s["CONF"].offset + s["CONF"].largo]).decode("utf-8")
        self.docs = np.frombuffer(self.mm, dtype=DOC, count=n, offset=s["DOCS"].offset)
        self.voc = np.frombuffer(self.mm, dtype=VOC, count=v, offset=s["VOCA"].offset)
        self._term0 = s["TERM"].offset
        self._term_fin = s["TERM"].offset + s["TERM"].largo
        self._post0 = s["POST"].offset
        self._post_fin = s["POST"].offset + s["POST"].largo
        self._term_off = self.voc["term_off"].astype(np.int64)
        self._post_off = self.voc["post_off"].astype(np.int64)

    def cerrar(self) -> None:
        self.docs = self.voc = None  # sueltan las vistas antes de cerrar el mmap
        self.mm.close()
        self._f.close()

    # --- vocabulario
    def termino(self, i: int) -> str:
        ini = self._term0 + int(self._term_off[i])
        fin = self._term0 + int(self._term_off[i + 1]) - 1 if i + 1 < self.V else self._term_fin - 1
        return self.mm[ini:fin].decode("utf-8")

    def _termino_bytes(self, i: int) -> bytes:
        ini = self._term0 + int(self._term_off[i])
        fin = self._term0 + int(self._term_off[i + 1]) - 1 if i + 1 < self.V else self._term_fin - 1
        return self.mm[ini:fin]

    def buscar(self, termino: str) -> int:
        """Búsqueda binaria en el vocabulario ordenado por bytes. -1 si no está."""
        objetivo = termino.encode("utf-8")
        lo, hi = 0, self.V - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            t = self._termino_bytes(mid)
            if t == objetivo:
                return mid
            if t < objetivo:
                lo = mid + 1
            else:
                hi = mid - 1
        return -1

    def postings(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        """(documentos, tf) del término i, decodificando el variable byte."""
        ini = self._post0 + int(self._post_off[i])
        fin = self._post0 + int(self._post_off[i + 1]) if i + 1 < self.V else self._post_fin
        buf = np.frombuffer(self.mm, dtype=np.uint8, count=fin - ini, offset=ini)
        return decodificar_postings(buf, int(self.voc[i]["df"]))

    def terminos(self):
        for i in range(self.V):
            yield self.termino(i)

    def estadisticas(self) -> dict:
        tam = self.ruta.stat().st_size
        return {
            "archivo": str(self.ruta),
            "bytes": tam,
            "documentos": self.N,
            "vocabulario": self.V,
            "tokens": self.tokens,
            "secciones": {k: v.largo for k, v in self.secciones.items()},
        }


def leer_estadisticas_json(ruta_idx: str | Path) -> dict:
    p = Path(str(ruta_idx) + ".json")
    return json.loads(p.read_text()) if p.exists() else {}
