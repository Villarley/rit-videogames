"""Pruebas del indexador propio: el .idx tiene que dar lo mismo que contar a mano."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from crawler.storage import Repository
from indexer.config import leer_configuracion
from indexer.construir import construir
from indexer.formato import IndiceBinario
from indexer.normalizacion import Analizador, normalizar_fechas, normalizar_numeros
from indexer.porter import porter
from indexer.varbyte import codificar, decodificar, decodificar_lote, decodificar_postings

RAIZ = Path(__file__).resolve().parent.parent

TEXTOS = [
    "The Legend of Zelda: Breath of the Wild came out on March 3, 2017.",
    "Elden Ring sold 25,000,000 copies. Elden Ring bosses are hard, hard, hard.",
    "El videojuego salió el 24 de febrero de 2017. Los videojuegos de rol tienen jefes.",
    "Pokémon Red and POKEMON blue: running, runner, runs.",
    "Grand Theft Auto V, 17 September 2013, 09/17/2013 and 2013-09-17 are the same date.",
    "Minecraft " * 40,
    "",
    "Call of Duty multiplayer, multi player and co op modes for PlayStation 5.",
]


def _conf(tmp_path: Path, politica: str, por_bloque: int = 2) -> Path:
    texto = (RAIZ / "config" / f"{politica}.conf").read_text(encoding="utf-8")
    texto = texto.replace("raiz = repository", f"raiz = {tmp_path / 'repo'}")
    texto = texto.replace("base = repository/crawl.db", f"base = {tmp_path / 'crawl.db'}")
    texto = texto.replace(f"salida = indices/{politica}.idx", f"salida = {tmp_path / politica}.idx")
    texto = texto.replace("temporal = indices/tmp", f"temporal = {tmp_path / 'tmp'}")
    texto = texto.replace("docs_por_bloque = 20000", f"docs_por_bloque = {por_bloque}")
    p = tmp_path / f"{politica}.conf"
    p.write_text(texto, encoding="utf-8")
    return p


@pytest.fixture
def coleccion(tmp_path):
    repo = Repository(str(tmp_path / "crawl.db"), str(tmp_path / "repo"))
    for i, t in enumerate(TEXTOS):
        h = hashlib.sha256(f"{i}{t}".encode()).hexdigest()
        repo.save_page(url=f"https://x.org/{i}", final_url=f"https://x.org/{i}", seed_id="s", depth=0,
                       parent_url=None, http_status=200, content_type="text/html", title=f"doc {i}",
                       text=t, content_hash=h, language="en", crawler_source="custom")
    repo.close()
    return tmp_path


def test_varbyte_ida_y_vuelta():
    nums = [0, 1, 127, 128, 300, 16383, 16384, 2**31, 5]
    buf = bytearray()
    for n in nums:
        codificar(n, buf)
    assert decodificar(np.frombuffer(bytes(buf), dtype=np.uint8)).tolist() == nums
    b300 = bytearray()
    codificar(300, b300)
    assert bytes(b300) == b"\xac\x02"


def test_porter_ejemplos_del_paper():
    casos = {"caresses": "caress", "ponies": "poni", "cats": "cat", "agreed": "agre",
             "plastered": "plaster", "motoring": "motor", "hopping": "hop", "filing": "file",
             "happy": "happi", "relational": "relat", "generalizations": "gener",
             "connections": "connect", "running": "run", "probate": "probat", "controll": "control"}
    for palabra, raiz in casos.items():
        assert porter(palabra) == raiz, palabra


def test_fechas_y_numeros():
    assert normalizar_fechas("March 3, 2017") == "20170303"
    assert normalizar_fechas("3rd March 2017") == "20170303"
    assert normalizar_fechas("24 de febrero de 2017") == "20170224"
    assert normalizar_fechas("2013-09-17 y 17/09/2013 y 09/17/2013") == "20130917 y 20130917 y 20130917"
    assert normalizar_fechas("Mayo 99, 2017") == "Mayo 99, 2017"  # día inválido: no se toca
    assert normalizar_fechas("Hello 5, 2017") == "Hello 5, 2017"  # no es mes
    assert normalizar_numeros("1,000,000 y 3,5 y 21st") == "1000000 y 3,5 y 21"


def test_nombres_y_tesauro():
    a = Analizador(leer_configuracion(RAIZ / "config" / "completo.conf").politica)
    assert a.analizar("Call of Duty") == ["cod"]
    assert a.analizar("the legend of zelda") == ["zelda"]
    assert a.analizar("Videojuegos y video games") == ["videogam", "videogam"]
    assert a.analizar("Pokémon") == ["pokemon"]
    # consultas mezcladas del colectivo del Proyecto 1 (sección 2.4)
    assert sorted(a.analizar("build mago elden ring")) == sorted(a.analizar("elden ring mage build"))
    assert sorted(a.analizar("receta poción")) == sorted(a.analizar("potion recipe"))


@pytest.mark.parametrize("politica", ["basico", "stop", "completo"])
def test_indice_igual_a_contar_a_mano(coleccion, politica):
    conf = leer_configuracion(_conf(coleccion, politica))
    stats = construir(conf, procesos=2, registrar=lambda *_: None)
    a = Analizador(conf.politica)
    cuentas = [Counter(a.analizar(t)) for t in TEXTOS]
    N = len(TEXTOS)
    df = Counter(t for c in cuentas for t in c)

    ix = IndiceBinario(conf.salida)
    try:
        assert ix.N == N == stats["documentos"]
        assert ix.V == len(df)
        assert ix.tokens == sum(sum(c.values()) for c in cuentas)
        vocab = list(ix.terminos())
        assert vocab == sorted(df, key=lambda s: s.encode())
        for t in df:
            i = ix.buscar(t)
            assert i >= 0 and ix.termino(i) == t
            docs, tfs = ix.postings(i)
            esperado = [(d, c[t]) for d, c in enumerate(cuentas) if t in c]
            assert list(zip(docs.tolist(), tfs.tolist())) == esperado
            assert int(ix.voc[i]["df"]) == df[t]
            assert float(ix.voc[i]["idf"]) == pytest.approx(math.log10(N / df[t]), rel=1e-6)
        assert ix.buscar("noexisteestapalabra") == -1
        for d, c in enumerate(cuentas):
            w2 = sum(((1 + math.log10(tf)) * math.log10(N / df[t])) ** 2 for t, tf in c.items())
            assert float(ix.docs[d]["norma"]) == pytest.approx(math.sqrt(w2), rel=1e-5, abs=1e-6)
            assert int(ix.docs[d]["largo"]) == sum(c.values())
        assert ix.docs["page_id"].tolist() == list(range(1, N + 1))
    finally:
        ix.cerrar()


def test_lote_igual_a_uno_por_uno():
    rng = np.random.default_rng(1)
    terminos = []
    for _ in range(30):
        df = int(rng.integers(1, 50))
        docs = np.sort(rng.choice(10_000, df, replace=False))
        tfs = rng.integers(1, 4, df)
        cod, extra, prev = bytearray(), bytearray(), 0
        for d, tf in zip(docs, tfs):
            codificar(((int(d) - prev) << 1) | (int(tf) == 1), cod)
            if tf != 1:
                codificar(int(tf), extra)
            prev = int(d)
        terminos.append((docs, tfs, bytes(cod + extra)))
    buf = b"".join(t[2] for t in terminos)
    inicios = np.cumsum([0] + [len(t[2]) for t in terminos[:-1]])
    dfs = np.array([len(t[0]) for t in terminos])
    docs, tfs, quien = decodificar_lote(np.frombuffer(buf, dtype=np.uint8), inicios, dfs)
    for k, (d, tf, b) in enumerate(terminos):
        assert docs[quien == k].tolist() == d.tolist()
        assert tfs[quien == k].tolist() == tf.tolist()
        d2, tf2 = decodificar_postings(np.frombuffer(b, dtype=np.uint8), len(d))
        assert d2.tolist() == d.tolist() and tf2.tolist() == tf.tolist()
