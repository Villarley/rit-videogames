"""Lo que guarda el buscador: bitácora de consultas, clics y juicios de relevancia.

Todo en un SQLite aparte (buscador/datos/evaluaciones.db), nunca en crawl.db.

- consultas: cada búsqueda del modo normal (texto, índice, cantidad, tiempo).
- clics: cada vez que alguien abre una página original desde los resultados.
- corridas: cada consulta que se hace en el modo evaluación, con quién la evaluó
  y para qué necesidad de información.
- mostrados: el ranking exacto que vio la persona evaluadora en esa corrida.
  Las métricas se calculan sobre este ranking, no sobre uno recalculado.
- juicios: la relevancia que marcó (0 no relevante, 1 algo, 2 muy relevante) y
  si ya conocía esa página antes de buscar (para cobertura y novedad).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS consultas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  sesion TEXT,
  consulta TEXT NOT NULL,
  indice TEXT NOT NULL,
  pagina INTEGER,
  total INTEGER,
  ms REAL,
  rocchio INTEGER DEFAULT 0,
  sugerencia TEXT
);
CREATE TABLE IF NOT EXISTS clics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  consulta_id INTEGER REFERENCES consultas(id),
  page_id INTEGER,
  rango INTEGER
);
CREATE TABLE IF NOT EXISTS corridas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  evaluador TEXT NOT NULL,
  necesidad TEXT,
  consulta TEXT NOT NULL,
  indice TEXT NOT NULL,
  total INTEGER,
  ms REAL
);
CREATE TABLE IF NOT EXISTS mostrados (
  corrida_id INTEGER REFERENCES corridas(id),
  rango INTEGER,
  page_id INTEGER,
  url TEXT,
  titulo TEXT,
  puntaje REAL,
  PRIMARY KEY (corrida_id, rango)
);
CREATE TABLE IF NOT EXISTS juicios (
  corrida_id INTEGER REFERENCES corridas(id),
  page_id INTEGER,
  relevancia INTEGER NOT NULL CHECK (relevancia IN (0, 1, 2)),
  conocido INTEGER NOT NULL DEFAULT 0,
  ts TEXT NOT NULL,
  PRIMARY KEY (corrida_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_corridas_necesidad ON corridas(necesidad);
"""


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Datos:
    def __init__(self, ruta: str | Path) -> None:
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.cn = sqlite3.connect(self.ruta, check_same_thread=False)
        self.cn.row_factory = sqlite3.Row
        self.cn.execute("PRAGMA journal_mode=WAL")
        self.cn.executescript(ESQUEMA)
        columnas = {r[1] for r in self.cn.execute("PRAGMA table_info(juicios)")}
        if "conocido" not in columnas:  # bases creadas antes de agregar cobertura y novedad
            self.cn.execute("ALTER TABLE juicios ADD COLUMN conocido INTEGER NOT NULL DEFAULT 0")
            self.cn.commit()

    def _ejecutar(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self.cn.execute(sql, args)
            self.cn.commit()
            return cur.lastrowid

    def registrar_consulta(self, sesion, consulta, indice, pagina, total, ms, rocchio, sugerencia) -> int:
        return self._ejecutar(
            "INSERT INTO consultas (ts, sesion, consulta, indice, pagina, total, ms, rocchio, sugerencia) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ahora(), sesion, consulta, indice, pagina, total, ms, int(rocchio), sugerencia))

    def registrar_clic(self, consulta_id, page_id, rango) -> None:
        self._ejecutar("INSERT INTO clics (ts, consulta_id, page_id, rango) VALUES (?,?,?,?)",
                       (ahora(), consulta_id, page_id, rango))

    def nueva_corrida(self, evaluador, necesidad, consulta, indice, total, ms, resultados) -> int:
        with self._lock:
            cur = self.cn.execute(
                "INSERT INTO corridas (ts, evaluador, necesidad, consulta, indice, total, ms) VALUES (?,?,?,?,?,?,?)",
                (ahora(), evaluador, necesidad, consulta, indice, total, ms))
            cid = cur.lastrowid
            self.cn.executemany(
                "INSERT INTO mostrados (corrida_id, rango, page_id, url, titulo, puntaje) VALUES (?,?,?,?,?,?)",
                [(cid, r.rango, r.page_id, r.url, r.titulo, r.puntaje) for r in resultados])
            self.cn.commit()
            return cid

    def corrida(self, cid: int):
        return self.cn.execute("SELECT * FROM corridas WHERE id = ?", (cid,)).fetchone()

    def mostrados(self, cid: int):
        return self.cn.execute("SELECT * FROM mostrados WHERE corrida_id = ? ORDER BY rango", (cid,)).fetchall()

    def juzgar(self, cid: int, page_id: int, relevancia: int, conocido: bool = False) -> None:
        self._ejecutar(
            "INSERT INTO juicios (corrida_id, page_id, relevancia, conocido, ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(corrida_id, page_id) DO UPDATE SET relevancia = excluded.relevancia, "
            "conocido = excluded.conocido, ts = excluded.ts",
            (cid, page_id, relevancia, int(bool(conocido)), ahora()))

    def conocidos_corrida(self, cid: int) -> set[int]:
        return {int(r[0]) for r in
                self.cn.execute("SELECT page_id FROM juicios WHERE corrida_id = ? AND conocido = 1", (cid,))}

    def juicios_corrida(self, cid: int) -> dict[int, int]:
        return {int(r[0]): int(r[1]) for r in
                self.cn.execute("SELECT page_id, relevancia FROM juicios WHERE corrida_id = ?", (cid,))}

    def juicios_para(self, consulta: str, indice: str) -> tuple[list[int], list[int]]:
        """Relevantes y no relevantes ya marcados para esta consulta (para Rocchio)."""
        filas = self.cn.execute(
            "SELECT j.page_id, MAX(j.relevancia) FROM juicios j JOIN corridas c ON c.id = j.corrida_id "
            "WHERE lower(trim(c.consulta)) = lower(trim(?)) GROUP BY j.page_id", (consulta,)).fetchall()
        rel = [int(p) for p, r in filas if r >= 1]
        norel = [int(p) for p, r in filas if r == 0]
        return rel, norel

    def exportar(self) -> dict:
        tablas = {}
        for t in ("consultas", "clics", "corridas", "mostrados", "juicios"):
            tablas[t] = [dict(r) for r in self.cn.execute(f"SELECT * FROM {t}")]
        return tablas

    def exportar_json(self) -> str:
        return json.dumps(self.exportar(), ensure_ascii=False, indent=1)
