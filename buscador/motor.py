"""Motor de búsqueda sobre el índice propio (.idx del Proyecto 2).

No depende de la araña ni del indexador: solo abre el .idx (con mmap), la base
crawl.db para el título y la URL original de cada página, y los .txt para armar
el fragmento que se muestra en los resultados.

Modelo vectorial con pesos ltc.ltc de SMART (Salton y Buckley 1988):
    w(t, d) = (1 + log10 tf) * idf,  idf = log10(N / df)
    puntaje(q, d) = suma de w(t, q) * w(t, d) / (|q| * |d|)     (coseno)
Las normas |d| ya vienen calculadas en el índice.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from indexer.config import RAIZ_REPO, politica_desde_texto
from indexer.formato import IndiceBinario
from indexer.normalizacion import Analizador

_OPERADOR = re.compile(r'(?:^|\s)([+-]?)(?:(site|idioma|fuente):(\S+)|"([^"]+)"|(\S+))')
_LETRAS = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass
class Consulta:
    texto: str
    libres: list[str] = field(default_factory=list)       # términos analizados con peso
    obligatorios: list[str] = field(default_factory=list)
    excluidos: list[str] = field(default_factory=list)
    sitios: list[str] = field(default_factory=list)
    sin_sitios: list[str] = field(default_factory=list)
    idioma: str | None = None
    fuente: str | None = None


@dataclass
class Resultado:
    doc: int
    page_id: int
    puntaje: float
    titulo: str = ""
    url: str = ""
    dominio: str = ""
    idioma: str = ""
    fragmento: str = ""
    rango: int = 0


@dataclass
class Respuesta:
    consulta: Consulta
    resultados: list[Resultado]
    total: int
    ms: float
    pesos: dict[str, float]
    desconocidos: list[str]
    sugerencia: str | None = None
    expansion: list[tuple[str, float]] = field(default_factory=list)


class Motor:
    def __init__(self, ruta_idx: str | Path, base: str | Path | None = None,
                 raiz_textos: str | Path | None = None) -> None:
        self.ruta = Path(ruta_idx)
        self.ix = IndiceBinario(self.ruta)
        self.politica = politica_desde_texto(self.ix.conf, RAIZ_REPO)
        self.nombre = self.politica.nombre
        self.analizador = Analizador(self.politica)
        conf = _conf_dict(self.ix.conf)
        self.base = Path(base) if base else _resolver(conf.get("coleccion", {}).get("base", "repository/crawl.db"))
        self.raiz = Path(raiz_textos) if raiz_textos else _resolver(conf.get("coleccion", {}).get("raiz", "repository"))
        self._db = sqlite3.connect(f"file:{self.base}?mode=ro", uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self.idf = self.ix.voc["idf"].astype(np.float64)
        self.normas = self.ix.docs["norma"].astype(np.float64)
        self._cargar_metadatos()

    # --- metadatos por documento (dominio, idioma, arañador) para filtrar rápido
    def _cargar_metadatos(self) -> None:
        cache = Path(str(self.ruta) + ".meta.npz")
        if cache.exists() and cache.stat().st_mtime >= self.ruta.stat().st_mtime:
            z = np.load(cache, allow_pickle=False)
            self.dom_id = z["dom_id"]
            self.dominios = list(z["dominios"])
            self.idiomas_id = z["idioma_id"]
            self.idiomas = list(z["idiomas"])
            self.fuente_id = z["fuente_id"]
            self.fuentes = list(z["fuentes"])
            return
        page_ids = self.ix.docs["page_id"]
        pos = {int(p): i for i, p in enumerate(page_ids)}
        dom_id = np.zeros(self.ix.N, dtype=np.int32)
        idioma_id = np.zeros(self.ix.N, dtype=np.int16)
        fuente_id = np.zeros(self.ix.N, dtype=np.int16)
        dominios: dict[str, int] = {}
        idiomas: dict[str, int] = {}
        fuentes: dict[str, int] = {}
        for row in self._db.execute("SELECT id, COALESCE(final_url, url), language, crawler_source FROM pages"):
            i = pos.get(int(row[0]))
            if i is None:
                continue
            d = (urlparse(row[1]).hostname or "").lower()
            dom_id[i] = dominios.setdefault(d, len(dominios))
            idioma_id[i] = idiomas.setdefault(row[2] or "", len(idiomas))
            fuente_id[i] = fuentes.setdefault(row[3] or "", len(fuentes))
        self.dom_id, self.idiomas_id, self.fuente_id = dom_id, idioma_id, fuente_id
        self.dominios, self.idiomas, self.fuentes = list(dominios), list(idiomas), list(fuentes)
        try:
            np.savez(cache, dom_id=dom_id, dominios=np.array(self.dominios or [""]),
                     idioma_id=idioma_id, idiomas=np.array(self.idiomas or [""]),
                     fuente_id=fuente_id, fuentes=np.array(self.fuentes or [""]))
        except OSError:
            pass

    # --- consulta
    def interpretar(self, texto: str) -> Consulta:
        """Separa operadores y analiza el resto con la misma política del índice.

        Las palabras sueltas seguidas se analizan juntas, así "17 de septiembre
        de 2013" se vuelve una sola fecha y "call of duty" el nombre "cod".
        """
        c = Consulta(texto=texto)
        libres: list[str] = []

        def vaciar() -> None:
            if libres:
                c.libres.extend(self.analizador.analizar(" ".join(libres)))
                libres.clear()

        for signo, campo, valor, frase, palabra in _OPERADOR.findall(texto):
            if not campo and not signo and not frase:
                libres.append(palabra)
                continue
            vaciar()
            if campo:
                valor = valor.lower()
                if campo == "site":
                    (c.sin_sitios if signo == "-" else c.sitios).append(valor)
                elif campo == "idioma":
                    c.idioma = valor
                else:
                    c.fuente = valor
                continue
            terminos = self.analizador.analizar(frase or palabra)
            if signo == "+":
                c.obligatorios.extend(terminos)
                c.libres.extend(terminos)
            elif signo == "-":
                c.excluidos.extend(terminos)
            else:
                c.libres.extend(terminos)
        vaciar()
        return c

    def _filtro(self, c: Consulta) -> np.ndarray | None:
        mask = None

        def y(m):
            nonlocal mask
            mask = m if mask is None else (mask & m)

        if c.sitios:
            ids = [i for i, d in enumerate(self.dominios) if any(d == s or d.endswith("." + s) for s in c.sitios)]
            y(np.isin(self.dom_id, ids))
        if c.sin_sitios:
            ids = [i for i, d in enumerate(self.dominios) if any(d == s or d.endswith("." + s) for s in c.sin_sitios)]
            y(~np.isin(self.dom_id, ids))
        if c.idioma:
            ids = [i for i, x in enumerate(self.idiomas) if x == c.idioma]
            y(np.isin(self.idiomas_id, ids))
        if c.fuente:
            ids = [i for i, x in enumerate(self.fuentes) if x == c.fuente]
            y(np.isin(self.fuente_id, ids))
        return mask

    def buscar(self, texto: str, k: int = 10, desde: int = 0, pesos_extra: dict[str, float] | None = None,
               fragmentos: bool = True) -> Respuesta:
        t0 = time.perf_counter()
        c = self.interpretar(texto)
        tf_q = Counter(c.libres)
        pesos: dict[str, float] = {}
        desconocidos = []
        ids: dict[str, int] = {}
        for t, tf in tf_q.items():
            i = self.ix.buscar(t)
            if i < 0:
                desconocidos.append(t)
                continue
            ids[t] = i
            pesos[t] = (1 + math.log10(tf)) * self.idf[i]
        if pesos_extra:
            for t, w in pesos_extra.items():
                i = ids.get(t, self.ix.buscar(t))
                if i >= 0:
                    ids[t] = i
                    pesos[t] = pesos.get(t, 0.0) + w
        norma_q = math.sqrt(sum(w * w for w in pesos.values())) or 1.0

        puntajes = np.zeros(self.ix.N, dtype=np.float64)
        tocados = np.zeros(self.ix.N, dtype=bool)
        for t, w_q in pesos.items():
            if w_q <= 0:
                continue
            docs, tfs = self.ix.postings(ids[t])
            w_d = (1 + np.log10(tfs.astype(np.float64))) * self.idf[ids[t]]
            puntajes[docs] += w_q * w_d
            tocados[docs] = True
        con_norma = self.normas > 0
        puntajes[con_norma] /= self.normas[con_norma] * norma_q

        candidatos = tocados
        for t in c.obligatorios:
            i = ids.get(t, self.ix.buscar(t))
            m = np.zeros(self.ix.N, dtype=bool)
            if i >= 0:
                m[self.ix.postings(i)[0]] = True
            candidatos &= m
        for t in c.excluidos:
            i = self.ix.buscar(t)
            if i >= 0:
                candidatos[self.ix.postings(i)[0]] = False
        filtro = self._filtro(c)
        if filtro is not None:
            if not pesos and not c.obligatorios:
                candidatos = filtro.copy()  # solo filtros: se listan las páginas del sitio
            else:
                candidatos &= filtro
        idx = np.flatnonzero(candidatos)
        total = int(idx.size)
        cuantos = min(desde + k, total)
        if cuantos:
            sub = puntajes[idx]
            if cuantos < total:
                part = np.argpartition(-sub, cuantos - 1)[:cuantos]
            else:
                part = np.arange(total)
            orden = part[np.lexsort((idx[part], -sub[part]))]
            elegidos = idx[orden][desde:cuantos]
        else:
            elegidos = np.zeros(0, dtype=np.int64)
        resultados = [
            Resultado(doc=int(d), page_id=int(self.ix.docs[d]["page_id"]), puntaje=float(puntajes[d]), rango=desde + r + 1)
            for r, d in enumerate(elegidos)
        ]
        self._completar(resultados, set(pesos) | set(c.obligatorios), fragmentos)
        sugerencia = self.sugerir(texto, c) if (desconocidos or total == 0) else None
        return Respuesta(consulta=c, resultados=resultados, total=total, ms=(time.perf_counter() - t0) * 1000,
                         pesos=pesos, desconocidos=desconocidos, sugerencia=sugerencia)

    def _completar(self, resultados: list[Resultado], terminos: set[str], fragmentos: bool) -> None:
        if not resultados:
            return
        marcas = ",".join("?" * len(resultados))
        filas = {
            int(r["id"]): r
            for r in self._db.execute(
                f"SELECT id, url, final_url, title, text_path, language FROM pages WHERE id IN ({marcas})",
                [r.page_id for r in resultados],
            )
        }
        for r in resultados:
            f = filas.get(r.page_id)
            if f is None:
                continue
            r.url = f["final_url"] or f["url"]
            r.titulo = (f["title"] or "").strip() or r.url
            r.dominio = (urlparse(r.url).hostname or "").lower()
            r.idioma = f["language"] or ""
            if fragmentos and f["text_path"]:
                r.fragmento = self.fragmento(self.raiz / f["text_path"], terminos)

    # --- fragmento con los términos resaltados
    def fragmento(self, ruta: Path, terminos: set[str], ancho: int = 32) -> str:
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            return ""
        palabras = texto.split()
        if not palabras:
            return ""
        coincide = np.zeros(len(palabras), dtype=np.int32)
        cache: dict[str, bool] = {}
        for i, p in enumerate(palabras):
            v = cache.get(p)
            if v is None:
                v = cache[p] = bool(terminos.intersection(self.analizador.analizar(p)))
            coincide[i] = v
        if coincide.any():
            ventana = np.convolve(coincide, np.ones(ancho, dtype=np.int32), mode="valid") if len(palabras) >= ancho else coincide
            ini = int(np.argmax(ventana))
        else:
            ini = 0
        pedazo = palabras[ini : ini + ancho]
        html = []
        for p in pedazo:
            esc = _escapar(p)
            html.append(f"<mark>{esc}</mark>" if cache.get(p) else esc)
        prefijo = "… " if ini > 0 else ""
        sufijo = " …" if ini + ancho < len(palabras) else ""
        return prefijo + " ".join(html) + sufijo

    # --- "¿quisiste decir?" (Norvig 2007): ediciones a distancia 1 que estén en el vocabulario
    def sugerir(self, texto: str, c: Consulta) -> str | None:
        cambios = False
        salida = []
        for palabra in texto.split():
            if ":" in palabra or palabra[:1] in "+-":
                salida.append(palabra)
                continue
            limpia = palabra.lower()
            terms = self.analizador.analizar(limpia)
            if not terms or all(self.ix.buscar(t) >= 0 for t in terms):
                salida.append(palabra)
                continue
            mejor, mejor_df = None, 0
            for cand in _ediciones(limpia):
                ts = self.analizador.analizar(cand)
                if len(ts) != 1:
                    continue
                i = self.ix.buscar(ts[0])
                if i >= 0 and int(self.ix.voc[i]["df"]) > mejor_df:
                    mejor, mejor_df = cand, int(self.ix.voc[i]["df"])
            if mejor:
                salida.append(mejor)
                cambios = True
            else:
                salida.append(palabra)
        return " ".join(salida) if cambios else None

    # --- vectores de documentos (para Rocchio)
    def vector_documento(self, page_id: int) -> dict[str, float]:
        fila = self._db.execute("SELECT text_path FROM pages WHERE id = ?", (page_id,)).fetchone()
        if not fila or not fila[0]:
            return {}
        try:
            texto = (self.raiz / fila[0]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        cuenta = Counter(self.analizador.analizar(texto))
        vec = {}
        for t, tf in cuenta.items():
            i = self.ix.buscar(t)
            if i >= 0:
                vec[t] = (1 + math.log10(tf)) * self.idf[i]
        n = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        return {t: w / n for t, w in vec.items()}

    def rocchio(self, texto: str, relevantes: list[int], no_relevantes: list[int],
                alfa: float = 1.0, beta: float = 0.75, gamma: float = 0.15, terminos: int = 10,
                k: int = 10, desde: int = 0) -> Respuesta:
        """Retroalimentación de relevancia de Rocchio (1971), con los valores de Manning et al. 2008, 9.1.1.

        q' = alfa q + beta * promedio(relevantes) - gamma * promedio(no relevantes)
        Se agregan a la consulta los "terminos" términos nuevos de más peso.
        """
        base = self.buscar(texto, k=1, fragmentos=False)
        nq = math.sqrt(sum(w * w for w in base.pesos.values())) or 1.0
        q = {t: alfa * w / nq for t, w in base.pesos.items()}
        suma: Counter = Counter()
        for pid in relevantes:
            for t, w in self.vector_documento(pid).items():
                suma[t] += beta * w / len(relevantes)
        for pid in no_relevantes:
            for t, w in self.vector_documento(pid).items():
                suma[t] -= gamma * w / len(no_relevantes)
        nuevos = [(t, w) for t, w in suma.most_common() if t not in q and w > 0][:terminos]
        extra = {t: suma[t] for t in q if suma[t]} | dict(nuevos)
        # buscar() suma estos pesos a los de la consulta (que ya vienen sin normalizar)
        escala = nq
        r = self.buscar(texto, k=k, desde=desde, pesos_extra={t: w * escala for t, w in extra.items()})
        r.expansion = nuevos
        return r

    def documento(self, page_id: int) -> dict | None:
        f = self._db.execute("SELECT id, url, final_url, title, language, crawler_source FROM pages WHERE id = ?",
                             (page_id,)).fetchone()
        return dict(f) if f else None


def _ediciones(p: str) -> set[str]:
    partes = [(p[:i], p[i:]) for i in range(len(p) + 1)]
    borrar = [a + b[1:] for a, b in partes if b]
    cambiar = [a + b[1] + b[0] + b[2:] for a, b in partes if len(b) > 1]
    reemplazar = [a + c + b[1:] for a, b in partes if b for c in _LETRAS]
    insertar = [a + c + b for a, b in partes for c in _LETRAS]
    return set(borrar + cambiar + reemplazar + insertar) - {p}


def _escapar(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _conf_dict(texto: str) -> dict[str, dict[str, str]]:
    import configparser

    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read_string(texto)
    return {s: dict(cp[s]) for s in cp.sections()}


def _resolver(v: str) -> Path:
    p = Path(v)
    return p if p.is_absolute() else RAIZ_REPO / p
