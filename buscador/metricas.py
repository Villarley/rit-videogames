"""Métricas de evaluación a partir de los juicios guardados.

Definiciones (Manning, Raghavan y Schütze 2008, cap. 8; Järvelin y Kekäläinen 2002):

- P@k: de los primeros k resultados mostrados, cuántos se marcaron relevantes
  (relevancia >= 1), entre k. Un resultado sin juicio cuenta como no relevante.
- AP (precisión promedio): promedio de P@i en cada posición i donde hay un
  relevante, dividido entre R. R es la cantidad de documentos relevantes
  distintos que se conocen para esa necesidad (juntando todas las corridas y
  evaluadores, como el "pooling" de TREC). MAP es el promedio de AP.
- RR: 1 / posición del primer relevante. MRR es el promedio.
- nDCG@10: ganancia acumulada con descuento log2, usando la relevancia graduada
  (0, 1, 2), dividida entre la del orden ideal con los juicios conocidos.
- kappa de Cohen: acuerdo entre dos evaluadores sobre los mismos pares
  (necesidad, documento), corregido por el acuerdo esperado por azar.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from itertools import combinations

from buscador.datos import Datos


def precision_en(rels: list[int], k: int) -> float:
    return sum(1 for r in rels[:k] if r >= 1) / k


def precision_promedio(rels: list[int], R: int) -> float:
    if R == 0:
        return 0.0
    aciertos = 0
    suma = 0.0
    for i, r in enumerate(rels, 1):
        if r >= 1:
            aciertos += 1
            suma += aciertos / i
    return suma / R


def rango_reciproco(rels: list[int]) -> float:
    for i, r in enumerate(rels, 1):
        if r >= 1:
            return 1.0 / i
    return 0.0


def dcg(ganancias: list[int], k: int) -> float:
    return sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(ganancias[:k], 1))


def ndcg(rels: list[int], conocidos: list[int], k: int = 10) -> float:
    ideal = dcg(sorted(conocidos, reverse=True), k)
    return dcg(rels, k) / ideal if ideal > 0 else 0.0


def kappa(pares: list[tuple[int, int]]) -> float | None:
    """Kappa de Cohen binaria (relevante sí/no)."""
    n = len(pares)
    if n == 0:
        return None
    acuerdo = sum(1 for a, b in pares if a == b) / n
    pa = sum(a for a, _ in pares) / n
    pb = sum(b for _, b in pares) / n
    azar = pa * pb + (1 - pa) * (1 - pb)
    if azar >= 1:
        return 1.0 if acuerdo == 1 else 0.0
    return (acuerdo - azar) / (1 - azar)


def percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    v = sorted(valores)
    i = (len(v) - 1) * p
    lo, hi = math.floor(i), math.ceil(i)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def calcular(datos: Datos) -> dict:
    cn = datos.cn
    corridas = [dict(r) for r in cn.execute("SELECT * FROM corridas ORDER BY id")]
    mostrados = defaultdict(list)
    for r in cn.execute("SELECT corrida_id, rango, page_id FROM mostrados ORDER BY corrida_id, rango"):
        mostrados[r[0]].append(int(r[2]))
    juicios = defaultdict(dict)
    for r in cn.execute("SELECT corrida_id, page_id, relevancia FROM juicios"):
        juicios[r[0]][int(r[1])] = int(r[2])

    def clave(c):
        return c["necesidad"] or c["consulta"].strip().lower()

    # relevancia conocida por necesidad (máximo entre evaluadores, para el pool)
    pool: dict[str, dict[int, int]] = defaultdict(dict)
    for c in corridas:
        for pid, rel in juicios[c["id"]].items():
            k = clave(c)
            pool[k][pid] = max(pool[k].get(pid, 0), rel)

    filas = []
    for c in corridas:
        j = juicios[c["id"]]
        if not j:
            continue
        rels = [j.get(pid, 0) for pid in mostrados[c["id"]]]
        conocidos = list(pool[clave(c)].values())
        R = sum(1 for r in conocidos if r >= 1)
        filas.append({
            "corrida": c["id"], "evaluador": c["evaluador"], "necesidad": clave(c), "consulta": c["consulta"],
            "indice": c["indice"], "ts": c["ts"], "juzgados": len(j), "relevantes": sum(1 for r in rels if r >= 1),
            "p5": precision_en(rels, 5), "p10": precision_en(rels, 10), "ap": precision_promedio(rels, R),
            "rr": rango_reciproco(rels), "ndcg10": ndcg(rels, conocidos, 10), "ms": c["ms"] or 0,
            "rels": rels,
        })

    def promedio(fs, m):
        return statistics.fmean(f[m] for f in fs) if fs else 0.0

    def resumen(fs):
        return {"corridas": len(fs), "P@5": promedio(fs, "p5"), "P@10": promedio(fs, "p10"),
                "MAP": promedio(fs, "ap"), "MRR": promedio(fs, "rr"), "nDCG@10": promedio(fs, "ndcg10")}

    por_indice = defaultdict(list)
    por_necesidad = defaultdict(list)
    por_evaluador = defaultdict(list)
    for f in filas:
        por_indice[f["indice"]].append(f)
        por_necesidad[f["necesidad"]].append(f)
        por_evaluador[f["evaluador"]].append(f)

    # precisión por posición (curva P@k, k = 1..10) por índice
    curva = {}
    for ind, fs in por_indice.items():
        curva[ind] = [statistics.fmean(precision_en(f["rels"], k) for f in fs) for k in range(1, 11)]

    # precisión interpolada en 11 puntos de recall (TREC), por índice
    def interpolada(fs):
        puntos = [0.0] * 11
        validas = 0
        for f in fs:
            R = sum(1 for r in pool[f["necesidad"]].values() if r >= 1)
            if R == 0:
                continue
            validas += 1
            pr = []
            aciertos = 0
            for i, r in enumerate(f["rels"], 1):
                if r >= 1:
                    aciertos += 1
                pr.append((aciertos / R, aciertos / i))
            for n in range(11):
                nivel = n / 10
                puntos[n] += max((p for rc, p in pr if rc >= nivel), default=0.0)
        return [p / validas for p in puntos] if validas else puntos

    recall_precision = {ind: interpolada(fs) for ind, fs in por_indice.items()}

    # acuerdo entre evaluadores
    por_par = defaultdict(dict)  # (necesidad, page) -> {evaluador: rel}
    for c in corridas:
        for pid, rel in juicios[c["id"]].items():
            por_par[(clave(c), pid)][c["evaluador"]] = 1 if rel >= 1 else 0
    evaluadores = sorted({c["evaluador"] for c in corridas})
    acuerdos = []
    for a, b in combinations(evaluadores, 2):
        pares = [(v[a], v[b]) for v in por_par.values() if a in v and b in v]
        if pares:
            acuerdos.append({"a": a, "b": b, "pares": len(pares), "kappa": kappa(pares)})

    # uso (modo búsqueda)
    consultas = [dict(r) for r in cn.execute("SELECT * FROM consultas ORDER BY id")]
    latencias = [c["ms"] for c in consultas if c["ms"] is not None]
    por_dia = Counter(c["ts"][:10] for c in consultas)
    clics = [dict(r) for r in cn.execute("SELECT * FROM clics")]
    clics_por_rango = Counter(int(c["rango"]) for c in clics if c["rango"])
    sin_resultados = sum(1 for c in consultas if not c["total"])
    top = Counter(c["consulta"].strip().lower() for c in consultas).most_common(15)
    consultas_con_clic = len({c["consulta_id"] for c in clics})

    return {
        "global": resumen(filas),
        "por_indice": {k: resumen(v) for k, v in sorted(por_indice.items())},
        "por_necesidad": {k: resumen(v) for k, v in sorted(por_necesidad.items())},
        "por_evaluador": {k: {**resumen(v), "juicios": sum(f["juzgados"] for f in v)}
                          for k, v in sorted(por_evaluador.items())},
        "curva_p_en_k": curva,
        "recall_precision": recall_precision,
        "acuerdo": acuerdos,
        "corridas": [{k: v for k, v in f.items() if k != "rels"} for f in filas],
        "juicios_total": sum(len(j) for j in juicios.values()),
        "distribucion_relevancia": dict(Counter(r for j in juicios.values() for r in j.values())),
        "uso": {
            "consultas": len(consultas),
            "sesiones": len({c["sesion"] for c in consultas}),
            "sin_resultados": sin_resultados,
            "proporcion_sin_resultados": sin_resultados / len(consultas) if consultas else 0,
            "latencia_p50": percentil(latencias, 0.5),
            "latencia_p95": percentil(latencias, 0.95),
            "latencia_promedio": statistics.fmean(latencias) if latencias else 0,
            "por_dia": dict(sorted(por_dia.items())),
            "clics": len(clics),
            "ctr": consultas_con_clic / len(consultas) if consultas else 0,
            "clics_por_rango": {str(k): clics_por_rango[k] for k in sorted(clics_por_rango)},
            "mas_buscadas": top,
            "con_rocchio": sum(1 for c in consultas if c["rocchio"]),
            "con_sugerencia": sum(1 for c in consultas if c["sugerencia"]),
        },
    }
