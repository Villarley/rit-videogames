"""Lectura de los archivos .conf (formato INI, el de muchos archivos de /etc).

Se usa configparser de la biblioteca estándar. Las rutas relativas se resuelven
desde la raíz del repo, para que el mismo .conf sirva para el indexador propio,
el de Lucene y el buscador sin importar desde dónde se corran.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

from indexer.normalizacion import Politica

RAIZ_REPO = Path(__file__).resolve().parent.parent

_SI = {"si", "sí", "yes", "true", "1", "on"}
_NO = {"no", "false", "0", "off", ""}


def _ruta(valor: str, base: Path) -> Path | None:
    valor = (valor or "").strip()
    if valor.lower() in _NO:
        return None
    p = Path(os.path.expanduser(valor))
    return p if p.is_absolute() else (base / p)


def _bool(valor: str) -> bool:
    v = (valor or "").strip().lower()
    if v in _SI:
        return True
    if v in _NO:
        return False
    raise ValueError(f"valor booleano inválido: {valor!r} (usar si/no)")


@dataclass
class Configuracion:
    archivo: Path
    texto: str
    politica: Politica
    raiz_coleccion: Path
    base: Path
    salida: Path
    procesos: int
    docs_por_bloque: int
    temporal: Path


def _politica(cp: configparser.ConfigParser, nombre: str, base: Path) -> Politica:
    n = cp["normalizacion"]
    stemming = n.get("stemming", "no").strip().lower()
    if stemming not in {"no", "porter"}:
        raise ValueError(f"stemming no soportado: {stemming}")
    return Politica(
        nombre=nombre,
        plegado=_ruta(n.get("plegado", "no"), base),
        minusculas=_bool(n.get("minusculas", "si")),
        fechas=_bool(n.get("fechas", "no")),
        numeros=_bool(n.get("numeros", "no")),
        stopwords=_ruta(n.get("stopwords", "no"), base),
        tesauro=_ruta(n.get("tesauro", "no"), base),
        nombres=_ruta(n.get("nombres", "no"), base),
        stemming=stemming,
        largo_min=n.getint("largo_min", 1),
        largo_max=n.getint("largo_max", 40),
    )


def _parser(texto: str, origen: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read_string(texto, source=origen)
    return cp


def leer_configuracion(archivo: str | Path, base: Path = RAIZ_REPO) -> Configuracion:
    archivo = Path(archivo)
    texto = archivo.read_text(encoding="utf-8")
    cp = _parser(texto, str(archivo))
    nombre = cp.get("indice", "nombre", fallback=archivo.stem)
    c = cp["coleccion"]
    i = cp["indice"]
    return Configuracion(
        archivo=archivo,
        texto=texto,
        politica=_politica(cp, nombre, base),
        raiz_coleccion=_ruta(c.get("raiz", "repository"), base),
        base=_ruta(c.get("base", "repository/crawl.db"), base),
        salida=_ruta(i.get("salida", f"indices/{nombre}.idx"), base),
        procesos=i.getint("procesos", 0) or (os.cpu_count() or 1),
        docs_por_bloque=i.getint("docs_por_bloque", 20000),
        temporal=_ruta(i.get("temporal", "indices/tmp"), base) / nombre,
    )


def politica_desde_texto(texto: str, base: Path = RAIZ_REPO) -> Politica:
    """El .idx guarda el .conf adentro; el buscador lo usa para analizar consultas."""
    cp = _parser(texto, "<indice>")
    return _politica(cp, cp.get("indice", "nombre", fallback="indice"), base)
