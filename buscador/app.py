"""Aplicación web del buscador (Proyecto 3). Programa aparte de la araña y del indexador.

    python -m buscador --indice indices/completo.idx --indice indices/stop.idx --indice indices/basico.idx

Tres modos separados:
    /          búsqueda normal (resultados con enlace a la página original)
    /evaluar   modo evaluación: la persona marca qué resultados son relevantes
    /metricas  métricas numéricas y gráficos a partir de los juicios y del uso
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import secrets
from pathlib import Path
from threading import Lock

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, url_for

from buscador import metricas
from buscador.datos import Datos
from buscador.motor import Motor

AQUI = Path(__file__).resolve().parent
POR_PAGINA = 10
K_EVALUACION = 10


def crear_app(indices: list[str], ruta_datos: str | Path, base: str | None = None,
              raiz: str | None = None, necesidades: str | Path | None = None,
              indices_adicionales: list[str] | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(AQUI / "templates"), static_folder=str(AQUI / "static"))
    motores: dict[str, Motor] = {}
    for r in indices:
        m = Motor(r, base=base, raiz_textos=raiz)
        motores[m.nombre] = m
    indices_cargados = tuple(motores)
    por_defecto = next(iter(motores))
    adicionales = {Path(r).stem: Path(r) for r in indices_adicionales or []}
    carga = Lock()
    datos = Datos(ruta_datos)
    lista_necesidades = json.loads(Path(necesidades or AQUI / "necesidades.json").read_text(encoding="utf-8"))
    necesidad_por_id = {n["id"]: n for n in lista_necesidades}

    def disponible(nombre: str) -> bool:
        return nombre in motores or (nombre in adicionales and adicionales[nombre].is_file())

    def motor(nombre: str | None) -> Motor:
        nombre = nombre or por_defecto
        if nombre not in motores and disponible(nombre):
            with carga:
                if nombre not in motores:
                    motores[nombre] = Motor(adicionales[nombre], base=base, raiz_textos=raiz)
        return motores.get(nombre, motores[por_defecto])

    def sesion() -> str:
        return request.cookies.get("sesion") or secrets.token_hex(8)

    def con_sesion(resp, s):
        resp = make_response(resp)
        resp.set_cookie("sesion", s, max_age=3600 * 24 * 365, samesite="Lax")
        return resp

    @app.context_processor
    def comunes():
        opciones = dict.fromkeys((*indices_cargados, *adicionales))
        return {"opciones_indice": [(nombre, disponible(nombre)) for nombre in opciones],
                "por_defecto": por_defecto}

    @app.get("/")
    def buscar():
        q = request.args.get("q", "").strip()
        nombre = request.args.get("indice", por_defecto)
        if not disponible(nombre):
            nombre = por_defecto
        pagina = max(1, request.args.get("p", 1, type=int))
        usar_rocchio = request.args.get("rocchio") == "1"
        s = sesion()
        if not q:
            return con_sesion(render_template("buscar.html", q="", indice=nombre, rocchio=usar_rocchio), s)
        m = motor(nombre)
        desde = (pagina - 1) * POR_PAGINA
        aviso_rocchio = None
        if usar_rocchio:
            rel, norel = datos.juicios_para(q, m.nombre)
            if rel:
                r = m.rocchio(q, rel, norel, k=POR_PAGINA, desde=desde)
            else:
                aviso_rocchio = "Todavía no hay juicios de relevancia para esta consulta; se muestra la búsqueda normal."
                r = m.buscar(q, k=POR_PAGINA, desde=desde)
        else:
            r = m.buscar(q, k=POR_PAGINA, desde=desde)
        cid = datos.registrar_consulta(s, q, m.nombre, pagina, r.total, r.ms, usar_rocchio and bool(r.expansion),
                                       r.sugerencia)
        paginas = (r.total + POR_PAGINA - 1) // POR_PAGINA
        return con_sesion(render_template("buscar.html", q=q, indice=m.nombre, r=r, pagina=pagina, paginas=paginas,
                                          consulta_id=cid, rocchio=usar_rocchio, aviso_rocchio=aviso_rocchio), s)

    @app.get("/ir/<int:consulta_id>/<int:rango>/<int:page_id>")
    def ir(consulta_id: int, rango: int, page_id: int):
        """Registra el clic y manda a la página original (no al .txt)."""
        doc = motor(None).documento(page_id)
        if not doc:
            abort(404)
        datos.registrar_clic(consulta_id, page_id, rango)
        return redirect(doc["final_url"] or doc["url"], code=302)

    # --- modo evaluación
    def formulario_evaluacion(evaluador, necesidad="", consulta="", indice=por_defecto, error=None):
        return render_template("evaluar.html", necesidades=lista_necesidades, evaluador=evaluador,
                               seleccion=necesidad, consulta=consulta, indice=indice, error=error)

    @app.get("/evaluar")
    def evaluar():
        return formulario_evaluacion(request.cookies.get("evaluador", ""))

    @app.post("/evaluar")
    def nueva_corrida():
        evaluador = request.form.get("evaluador", "").strip()
        necesidad = request.form.get("necesidad", "").strip() or None
        consulta = request.form.get("consulta", "").strip()
        m = motor(request.form.get("indice"))
        if necesidad and necesidad not in necesidad_por_id:
            return formulario_evaluacion(evaluador, "", consulta, m.nombre,
                                         "Elegí una necesidad de la lista."), 400
        if not evaluador or len(evaluador) > 80 or len(consulta) > 300 or not (necesidad or consulta):
            return formulario_evaluacion(evaluador, necesidad, consulta, m.nombre,
                                         "Ingresá un alias y elegí una necesidad o escribí tu consulta."), 400
        if necesidad and not consulta:
            consulta = necesidad_por_id[necesidad]["consulta"]
        r = m.buscar(consulta, k=K_EVALUACION, fragmentos=False)
        cid = datos.nueva_corrida(evaluador, necesidad, consulta, m.nombre, r.total, r.ms, r.resultados)
        resp = make_response(redirect(url_for("corrida", cid=cid)))
        resp.set_cookie("evaluador", evaluador, max_age=3600 * 24 * 365, samesite="Lax")
        return resp

    @app.get("/evaluar/<int:cid>")
    def corrida(cid: int):
        c = datos.corrida(cid)
        if not c:
            abort(404)
        filas = datos.mostrados(cid)
        m = motor(c["indice"])
        terminos = set(m.interpretar(c["consulta"]).libres)
        resultados = []
        for f in filas:
            ruta = m._db.execute("SELECT text_path FROM pages WHERE id = ?", (f["page_id"],)).fetchone()
            frag = m.fragmento(m.raiz / ruta[0], terminos) if ruta and ruta[0] else ""
            resultados.append({**dict(f), "fragmento": frag})
        return render_template("corrida.html", c=c, resultados=resultados, juicios=datos.juicios_corrida(cid),
                               conocidos=datos.conocidos_corrida(cid), necesidad=necesidad_por_id.get(c["necesidad"] or ""))

    @app.post("/api/juicio")
    def juicio():
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            return jsonify(error="No se pudo leer la valoración."), 400
        cid, page_id, rel = (d.get(k) for k in ("corrida", "page_id", "relevancia"))
        conocido = d.get("conocido", False)
        if (any(type(v) is not int for v in (cid, page_id, rel)) or type(conocido) is not bool
                or rel not in (0, 1, 2) or not 0 < cid < 2**63 or not 0 < page_id < 2**63
                or not datos.fue_mostrado(cid, page_id)):
            return jsonify(error="La valoración no corresponde a un resultado de esta evaluación."), 400
        datos.juzgar(cid, page_id, rel, conocido)
        return jsonify(ok=True, juzgados=len(datos.juicios_corrida(cid)))

    # --- métricas
    @app.get("/metricas")
    def ver_metricas():
        return render_template("metricas.html", m=metricas.calcular(datos))

    @app.get("/api/metricas")
    def api_metricas():
        return jsonify(metricas.calcular(datos))

    @app.get("/api/exportar")
    def exportar():
        return Response(datos.exportar_json(), mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=evaluaciones.json"})

    @app.get("/api/corridas.csv")
    def corridas_csv():
        m = metricas.calcular(datos)
        buf = io.StringIO()
        campos = ["corrida", "evaluador", "necesidad", "consulta", "indice", "ts", "juzgados", "relevantes",
                  "p5", "p10", "ap", "rr", "ndcg10", "ms"]
        w = csv.DictWriter(buf, fieldnames=campos)
        w.writeheader()
        for f in m["corridas"]:
            w.writerow({k: f[k] for k in campos})
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=corridas.csv"})

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Buscador de videojuegos (RIT, Proyecto 3)")
    ap.add_argument("--indice", action="append", required=True, help=".idx del Proyecto 2 (se puede repetir)")
    ap.add_argument("--indice-adicional", action="append", help=".idx opcional; se habilita al aparecer y se abre al usarlo")
    ap.add_argument("--datos", default=str(AQUI / "datos" / "evaluaciones.db"))
    ap.add_argument("--base", help="crawl.db (por defecto, la del .conf del índice)")
    ap.add_argument("--raiz", help="carpeta repository/ con los .txt (por defecto, la del .conf)")
    ap.add_argument("--necesidades", help="JSON con las necesidades de información")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--puerto", type=int, default=5000)
    a = ap.parse_args(argv)
    app = crear_app(a.indice, a.datos, a.base, a.raiz, a.necesidades, a.indice_adicional)
    app.run(host=a.host, port=a.puerto, threaded=True)
    return 0
