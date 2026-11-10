"""Pruebas del buscador (Proyecto 3): búsqueda, evaluación y métricas."""
from __future__ import annotations

import math

import pytest

from buscador import metricas as M
from buscador.app import crear_app
from indexer.config import leer_configuracion
from indexer.construir import construir
from tests.test_indexer import _conf, coleccion  # noqa: F401


@pytest.fixture
def app(coleccion, tmp_path):  # noqa: F811
    rutas = []
    for p in ("completo", "basico"):
        conf = leer_configuracion(_conf(coleccion, p))
        construir(conf, procesos=1, registrar=lambda *_: None)
        rutas.append(str(conf.salida))
    a = crear_app(rutas, tmp_path / "eval.db")
    a.testing = True
    return a


def test_busqueda_y_enlace_original(app):
    c = app.test_client()
    r = c.get("/?q=elden+ring+bosses")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "https://x.org/1" in html
    assert "<mark>" in html
    ir = c.get("/ir/1/1/2")
    assert ir.status_code == 302 and ir.headers["Location"] == "https://x.org/1"


def test_operadores_y_fechas(app):
    c = app.test_client()
    # La fecha escrita en español encuentra el documento que la tiene en otro formato.
    html = c.get("/?q=17+de+septiembre+de+2013").get_data(as_text=True)
    assert "https://x.org/4" in html
    html = c.get("/?q=pokemon+-blue").get_data(as_text=True)
    assert "https://x.org/3" not in html
    html = c.get("/?q=minecrafr").get_data(as_text=True)
    assert "Quisiste decir" in html and "minecraft" in html


def test_evaluacion_y_metricas(app):
    c = app.test_client()
    r = c.post("/evaluar", data={"evaluador": "Ana", "necesidad": "", "consulta": "zelda", "indice": "completo"})
    assert r.status_code == 302
    cid = int(r.headers["Location"].rsplit("/", 1)[1])
    pagina = c.get(f"/evaluar/{cid}").get_data(as_text=True)
    assert "Muy relevante" in pagina
    assert c.post("/api/juicio", json={"corrida": cid, "page_id": 1, "relevancia": 2}).json["juzgados"] == 1
    r2 = c.post("/evaluar", data={"evaluador": "Beto", "consulta": "zelda", "indice": "completo"})
    cid2 = int(r2.headers["Location"].rsplit("/", 1)[1])
    c.post("/api/juicio", json={"corrida": cid2, "page_id": 1, "relevancia": 1})
    m = c.get("/api/metricas").json
    assert m["global"]["corridas"] == 2
    assert m["global"]["P@5"] == pytest.approx(0.2)
    assert m["acuerdo"][0]["kappa"] == pytest.approx(1.0)
    assert c.get("/metricas").status_code == 200
    # Rocchio usa los juicios de esa consulta
    html = c.get("/?q=zelda&rocchio=1").get_data(as_text=True)
    assert "Consulta ampliada con Rocchio" in html


def test_formulas():
    assert M.precision_en([2, 0, 1, 0, 0], 5) == pytest.approx(0.4)
    assert M.precision_promedio([1, 0, 1], R=3) == pytest.approx((1 + 2 / 3) / 3)
    assert M.rango_reciproco([0, 0, 1]) == pytest.approx(1 / 3)
    ideal = (3 / math.log2(2)) + (1 / math.log2(3))
    assert M.ndcg([1, 2], [2, 1]) == pytest.approx((1 + 3 / math.log2(3)) / ideal)
    assert M.kappa([(1, 1), (0, 0), (1, 0), (0, 0)]) == pytest.approx(0.5)


def test_frase_y_prefijo(coleccion, tmp_path):  # noqa: F811
    from buscador.motor import Motor

    conf = leer_configuracion(_conf(coleccion, "basico"))
    construir(conf, procesos=1, registrar=lambda *_: None)
    m = Motor(conf.salida)
    assert [r.url for r in m.buscar('"bosses are hard"').resultados] == ["https://x.org/1"]
    assert m.buscar('"hard bosses"').total == 0          # están los dos términos, pero no seguidos
    assert m.buscar("bosses hard").total == 1
    r = m.buscar("multi*")
    assert set(r.consulta.prefijos["multi*"]) == {"multi", "multiplayer"}
    assert [x.url for x in r.resultados] == ["https://x.org/7"]
    assert m.buscar("pokem*").resultados[0].url == "https://x.org/3"
    assert m.ix.prefijo("zzzz") == range(m.ix.prefijo("zzzz").start, m.ix.prefijo("zzzz").start)


def test_r_precision_f_cobertura_novedad():
    assert M.precision_r([1, 0, 1, 1], 2) == pytest.approx(0.5)
    assert M.precision_r([1, 0], 0) == 0.0
    # P@10 = 0.2, recall@10 = 2/4 -> F = 2 * 0.2 * 0.5 / 0.7
    assert M.f_en([1, 1] + [0] * 8, 4) == pytest.approx(2 * 0.2 * 0.5 / 0.7)
    cob, nov = M.cobertura_novedad({1, 2, 3}, conocidos={1, 9}, conocidos_relevantes={1, 9})
    assert cob == pytest.approx(0.5) and nov == pytest.approx(2 / 3)
    assert M.cobertura_novedad(set(), set(), set()) == (None, None)


def test_juicio_con_conocido(app):
    c = app.test_client()
    cid = int(c.post("/evaluar", data={"evaluador": "Ana", "consulta": "zelda", "indice": "completo"})
              .headers["Location"].rsplit("/", 1)[1])
    c.post("/api/juicio", json={"corrida": cid, "page_id": 1, "relevancia": 2, "conocido": True})
    g = c.get("/api/metricas").json["global"]
    assert g["cobertura"] == pytest.approx(1.0) and g["novedad"] == pytest.approx(0.0)
    assert g["R-prec"] == pytest.approx(1.0)
    assert "ya la conocía" in c.get(f"/evaluar/{cid}").get_data(as_text=True)
    assert "R-precisión" in c.get("/metricas").get_data(as_text=True)
