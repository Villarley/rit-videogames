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
    assert "Rocchio" in html


def test_formulas():
    assert M.precision_en([2, 0, 1, 0, 0], 5) == pytest.approx(0.4)
    assert M.precision_promedio([1, 0, 1], R=3) == pytest.approx((1 + 2 / 3) / 3)
    assert M.rango_reciproco([0, 0, 1]) == pytest.approx(1 / 3)
    ideal = (3 / math.log2(2)) + (1 / math.log2(3))
    assert M.ndcg([1, 2], [2, 1]) == pytest.approx((1 + 3 / math.log2(3)) / ideal)
    assert M.kappa([(1, 1), (0, 0), (1, 0), (0, 0)]) == pytest.approx(0.5)
