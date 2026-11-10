"""Corre los dos indexadores con las políticas que se le pasen y compara.

    python -m indexer.benchmark config/basico.conf config/stop.conf config/completo.conf
    python -m indexer.benchmark config/*.conf --limite 20000      # prueba corta

Para cada .conf corre el indexador propio y el de Lucene (java -jar), uno
después del otro y con la misma cantidad de procesos, y deja en
indices/reporte/:

- comparacion.csv: tiempo, MiB/s, tamaño, vocabulario y postings de cada índice
- estadisticas_<politica>.json: términos con mayor df, distribución de df, etc.
- gráficos .png para el documento
- verificacion.txt: si el vocabulario y los df de los dos índices son iguales
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from indexer.config import RAIZ_REPO, leer_configuracion  # noqa: E402
from indexer.construir import construir  # noqa: E402
from indexer.formato import IndiceBinario  # noqa: E402

JAR = RAIZ_REPO / "lucene" / "target" / "indexador-lucene.jar"


def ruta_lucene(conf_path: Path) -> Path:
    import configparser

    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(conf_path, encoding="utf-8")
    p = Path(cp.get("lucene", "salida", fallback="indices/lucene"))
    return p if p.is_absolute() else RAIZ_REPO / p


def correr_lucene(conf: Path, limite: int | None, procesos: int) -> dict:
    if not JAR.exists():
        raise SystemExit(f"Falta {JAR}. Compilar con: cd lucene && mvn -q package")
    cmd = ["java", "-Xmx4g", f"-Drit.raiz={RAIZ_REPO}", "-jar", str(JAR), "indexar", str(conf),
           "--hilos", str(procesos)]
    if limite:
        cmd += ["--limite", str(limite)]
    subprocess.run(cmd, check=True)
    return json.loads(Path(str(ruta_lucene(conf)) + ".json").read_text())


def volcar_lucene(conf: Path) -> dict[str, tuple[int, int]]:
    out = subprocess.run(["java", f"-Drit.raiz={RAIZ_REPO}", "-jar", str(JAR), "volcar", str(conf)],
                         check=True, capture_output=True).stdout.decode("utf-8")
    res = {}
    for linea in out.splitlines():
        t, df, cf = linea.split("\t")
        res[t] = (int(df), int(cf))
    return res


def estadisticas_indice(ruta: Path, top: int = 40) -> dict:
    ix = IndiceBinario(ruta)
    try:
        df = ix.voc["df"].astype(np.int64)
        cf = ix.voc["cf"].astype(np.int64)
        orden = np.argsort(-df)[:top]
        largos = ix.docs["largo"].astype(np.int64)
        hapax = int((cf == 1).sum())
        return {
            "documentos": ix.N,
            "vocabulario": ix.V,
            "tokens": ix.tokens,
            "postings": int(df.sum()),
            "hapax_legomena": hapax,
            "proporcion_hapax": hapax / ix.V if ix.V else 0,
            "df_mediana": float(np.median(df)) if ix.V else 0,
            "terminos_df_1": int((df == 1).sum()),
            "largo_doc_promedio": float(largos.mean()) if ix.N else 0,
            "largo_doc_mediana": float(np.median(largos)) if ix.N else 0,
            "largo_doc_max": int(largos.max()) if ix.N else 0,
            "docs_vacios": int((largos == 0).sum()),
            "bytes_por_seccion": {k: v.largo for k, v in ix.secciones.items()},
            "top_df": [
                {"termino": ix.termino(int(i)), "df": int(df[i]), "cf": int(cf[i]),
                 "idf": float(ix.voc[i]["idf"])}
                for i in orden
            ],
            "_cf": np.sort(cf)[::-1],
            "_largos": largos,
        }
    finally:
        ix.cerrar()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("conf", nargs="+")
    ap.add_argument("--limite", type=int)
    ap.add_argument("--procesos", type=int, default=os.cpu_count())
    ap.add_argument("--salida", default=str(RAIZ_REPO / "indices" / "reporte"))
    ap.add_argument("--sin-lucene", action="store_true")
    a = ap.parse_args(argv)
    salida = Path(a.salida)
    salida.mkdir(parents=True, exist_ok=True)

    filas = []
    verificacion = []
    stats_por_politica = {}
    for c in a.conf:
        conf_path = Path(c).resolve()
        conf = leer_configuracion(conf_path)
        nombre = conf.politica.nombre
        propio = construir(conf, limite=a.limite, procesos=a.procesos)
        filas.append(propio)
        st = estadisticas_indice(conf.salida)
        stats_por_politica[nombre] = st
        (salida / f"estadisticas_{nombre}.json").write_text(json.dumps(
            {k: v for k, v in st.items() if not k.startswith("_")}, indent=2, ensure_ascii=False))
        if not a.sin_lucene:
            luc = correr_lucene(conf_path, a.limite, a.procesos)
            filas.append(luc)
            vl = volcar_lucene(conf_path)
            ix = IndiceBinario(conf.salida)
            iguales = 0
            distintos = []
            for i, t in enumerate(ix.terminos()):
                par = (int(ix.voc[i]["df"]), int(ix.voc[i]["cf"]))
                if vl.get(t) == par:
                    iguales += 1
                elif len(distintos) < 20:
                    distintos.append((t, par, vl.get(t)))
            solo_lucene = len(set(vl) - set(ix.terminos())) if len(vl) != iguales else 0
            ix.cerrar()
            verificacion.append(
                f"{nombre}: propio {propio['vocabulario']:,} términos, lucene {len(vl):,}; "
                f"iguales en df y cf: {iguales:,}; solo en lucene: {solo_lucene}; ejemplos distintos: {distintos}")

    campos = ["indexador", "politica", "normalizacion", "documentos", "vocabulario", "tokens", "postings",
              "bytes_texto", "bytes_indice", "proporcion_indice_texto", "mib_por_segundo", "segundos_total"]
    with open(salida / "comparacion.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for f in filas:
            w.writerow({**f, "segundos_total": f["segundos"]["total"]})
    (salida / "verificacion.txt").write_text("\n".join(verificacion) + "\n", encoding="utf-8")

    # Gráficos
    politicas = list(dict.fromkeys(f["politica"] for f in filas))
    for medida, titulo, archivo, escala in [
        ("bytes_indice", "Tamaño del índice (MiB)", "tamano.png", 2**20),
        ("segundos_total", "Tiempo de indexación (s)", "tiempo.png", 1),
        ("vocabulario", "Términos en el vocabulario", "vocabulario.png", 1),
    ]:
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.arange(len(politicas))
        for k, idx in enumerate(["propio", "lucene"]):
            vals = []
            for p in politicas:
                f = next((f for f in filas if f["politica"] == p and f["indexador"] == idx), None)
                v = (f["segundos"]["total"] if medida == "segundos_total" else f[medida]) if f else 0
                vals.append(v / escala)
            ax.bar(x + (k - 0.5) * 0.38, vals, 0.38, label=idx)
        ax.set_xticks(x, politicas)
        ax.set_title(titulo)
        ax.legend()
        fig.tight_layout()
        fig.savefig(salida / archivo, dpi=150)
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for p, st in stats_por_politica.items():
        cf = st["_cf"]
        if cf.size:
            ax.loglog(np.arange(1, cf.size + 1), cf, label=p)
    ax.set_xlabel("rango del término")
    ax.set_ylabel("frecuencia en la colección")
    ax.set_title("Ley de Zipf por política")
    ax.legend()
    fig.tight_layout()
    fig.savefig(salida / "zipf.png", dpi=150)
    plt.close(fig)

    print(f"\nReporte en {salida}")
    print((salida / "verificacion.txt").read_text())
    with open(salida / "comparacion.csv", encoding="utf-8") as fh:
        print(fh.read())
    return 0


if __name__ == "__main__":
    sys.exit(main())
