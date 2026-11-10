"""Normalización del texto, igual para el índice propio, el de Lucene y el buscador.

El orden de las etapas es el mismo que arma "lucene/.../AnalizadorRit.java":

    texto -> fechas -> números -> plegado -> tokens -> minúsculas -> largo
          -> nombres y tesauro -> palabras vacías -> Porter

Las tres primeras trabajan sobre el texto completo (en Lucene son CharFilter) y
las demás sobre la lista de términos (en Lucene son TokenFilter). Qué etapas
corren lo decide la sección [normalizacion] del archivo .conf.

Las expresiones regulares están escritas para que signifiquen lo mismo en Python
y en Java: dígitos como [0-9] en vez de \\d, y en Java se compilan con
UNICODE_CHARACTER_CLASS para que \\b y \\s se comporten como en Python.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from indexer.porter import porter

# --- Fechas -----------------------------------------------------------------
# Lo que aprendí en fechas.sh (Tarea05) revisando la colección: las fechas
# vienen como 2026-10-05, 10/05/2026, "October 5, 2026", "5 de octubre de
# 2026" y "5 Oct 2026". Todas quedan como un solo término AAAAMMDD, así una
# búsqueda de "5 de octubre de 2026" encuentra la página que dice "Oct 5, 2026".

MESES: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "enero": 1, "ene": 1, "febrero": 2, "marzo": 3, "abril": 4, "abr": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "set": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12, "dic": 12,
}
_MES = "|".join(sorted(MESES, key=lambda m: (-len(m), m)))

# Mismo texto de patrón que en Java (AnalizadorRit.PATRONES_FECHA).
PATRON_ISO = r"\b([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})\b"
# El mes se busca como "una palabra de 3 a 10 letras" y se revisa contra MESES
# en la función de reemplazo. Poner los 45 nombres como alternativa en el regex
# daba 5 MB/s; así da 27 MB/s y el resultado es el mismo.
PATRON_MES_DIA = (
    r"\b([A-Za-z]{3,10})\.?\s+([0-9]{1,2})(?:st|nd|rd|th)?,?\s+([0-9]{4})\b"
)
PATRON_DIA_MES = (
    r"\b([0-9]{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?(" + _MES
    + r")\.?,?\s+(?:del?\s+)?([0-9]{4})\b"
)
PATRON_NUMERICA = r"\b([0-9]{1,2})[/.-]([0-9]{1,2})[/.-]([0-9]{4})\b"

_RE_ISO = re.compile(PATRON_ISO)
_RE_MES_DIA = re.compile(PATRON_MES_DIA)
_RE_DIA_MES = re.compile(PATRON_DIA_MES, re.IGNORECASE)
_RE_NUMERICA = re.compile(PATRON_NUMERICA)


def _fecha(anio: int, mes: int, dia: int) -> str | None:
    if 1 <= mes <= 12 and 1 <= dia <= 31 and 1000 <= anio <= 2999:
        return f"{anio:04d}{mes:02d}{dia:02d}"
    return None


def _iso(m: re.Match) -> str:
    return _fecha(int(m[1]), int(m[2]), int(m[3])) or m[0]


def _mes_dia(m: re.Match) -> str:
    mes = MESES.get(m[1].lower())
    if mes is None:
        return m[0]
    return _fecha(int(m[3]), mes, int(m[2])) or m[0]


def _dia_mes(m: re.Match) -> str:
    return _fecha(int(m[3]), MESES[m[2].lower()], int(m[1])) or m[0]


def _numerica(m: re.Match) -> str:
    a, b, anio = int(m[1]), int(m[2]), int(m[3])
    # 13/10/2026 solo puede ser día/mes. Si los dos son <= 12 es ambiguo y
    # asumo mes/día, el formato de Estados Unidos, porque 164 de las 184
    # semillas son en inglés.
    if a > 12:
        return _fecha(anio, b, a) or m[0]
    return _fecha(anio, a, b) or m[0]


def normalizar_fechas(texto: str) -> str:
    texto = _RE_ISO.sub(_iso, texto)
    texto = _RE_MES_DIA.sub(_mes_dia, texto)
    texto = _RE_DIA_MES.sub(_dia_mes, texto)
    return _RE_NUMERICA.sub(_numerica, texto)


# --- Números ----------------------------------------------------------------
# "1,000,000" se vuelve "1000000" (si no, el tokenizador lo parte en tres) y
# "4th" o "21st" se vuelven "4" y "21". La coma solo se quita si le siguen
# exactamente tres dígitos, para no tocar listas como "3,5".
PATRON_MILES = r"(?<=[0-9]),(?=[0-9]{3}(?![0-9]))"
PATRON_ORDINAL = r"\b([0-9]+)(?:st|nd|rd|th)\b"
_RE_MILES = re.compile(PATRON_MILES)
_RE_ORDINAL = re.compile(PATRON_ORDINAL, re.IGNORECASE)


def normalizar_numeros(texto: str) -> str:
    texto = _RE_MILES.sub("", texto)
    return _RE_ORDINAL.sub(r"\1", texto)


# --- Tokens -----------------------------------------------------------------
# Un término es una corrida de letras o dígitos de cualquier idioma. En Java es
# [\p{L}\p{N}]+, que abarca las mismas categorías de Unicode que [^\W_] aquí.
PATRON_TOKEN = r"[^\W_]+"
_RE_TOKEN = re.compile(PATRON_TOKEN)


# --- Archivos de la política -----------------------------------------------

def cargar_plegado(ruta: Path) -> dict[int, str]:
    """Lee el archivo en formato MappingCharFilter: "á" => "a"."""
    tabla: dict[int, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        origen, destino = (p.strip() for p in linea.split("=>"))
        tabla[ord(origen.strip('"'))] = destino.strip('"')
    return tabla


def cargar_lista(ruta: Path) -> frozenset[str]:
    palabras = set()
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#"):
            palabras.add(linea)
    return frozenset(palabras)


def cargar_reglas(ruta: Path) -> list[tuple[str, str]]:
    """Reglas "frase => termino" (formato de sinónimos de Solr)."""
    reglas = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        izquierda, derecha = linea.split("=>")
        reglas.append((izquierda.strip(), derecha.strip()))
    return reglas


@dataclass
class Politica:
    nombre: str = "basico"
    plegado: Path | None = None
    minusculas: bool = True
    fechas: bool = False
    numeros: bool = False
    stopwords: Path | None = None
    tesauro: Path | None = None
    nombres: Path | None = None
    stemming: str = "no"
    largo_min: int = 1
    largo_max: int = 40
    extra: dict = field(default_factory=dict)

    def resumen(self) -> str:
        partes = []
        if self.plegado:
            partes.append("plegado")
        if self.minusculas:
            partes.append("minusculas")
        if self.fechas:
            partes.append("fechas")
        if self.numeros:
            partes.append("numeros")
        if self.nombres:
            partes.append("nombres")
        if self.tesauro:
            partes.append("tesauro")
        if self.stopwords:
            partes.append("stopwords")
        if self.stemming != "no":
            partes.append(self.stemming)
        return "+".join(partes)

    def banderas(self) -> int:
        """Bits que se guardan en el encabezado del .idx (ver FORMATO_INDICE.md)."""
        b = 0
        for i, activo in enumerate([
            self.plegado, self.minusculas, self.fechas, self.numeros,
            self.stopwords, self.tesauro, self.nombres, self.stemming == "porter",
        ]):
            if activo:
                b |= 1 << i
        return b


class Analizador:
    """Convierte texto en la lista de términos del índice según la política."""

    def __init__(self, politica: Politica) -> None:
        self.p = politica
        self.plegado = cargar_plegado(politica.plegado) if politica.plegado else None
        self.stop = cargar_lista(politica.stopwords) if politica.stopwords else frozenset()
        self._raices: dict[str, str] = {}
        self._plegados: dict[str, str] = {}
        self.reglas: dict[str, list[tuple[tuple[str, ...], list[str]]]] = {}
        for ruta in (politica.nombres, politica.tesauro):
            if ruta:
                for izq, der in cargar_reglas(ruta):
                    self._agregar_regla(izq, der)
        for lista in self.reglas.values():
            lista.sort(key=lambda r: -len(r[0]))

    def _tokens_simples(self, texto: str) -> list[str]:
        if self.p.minusculas:
            texto = texto.lower()
        tokens = _RE_TOKEN.findall(texto)
        if self.plegado and not texto.isascii():
            # En Lucene el plegado es un CharFilter que corre antes del
            # tokenizador. Aquí corre después, término por término y con
            # caché, porque str.translate sobre todo el texto iba a 14 MB/s.
            # El resultado es el mismo: el archivo de plegado solo cambia
            # letras por letras, así que no mueve los cortes entre términos, y
            # trae las mayúsculas y las minúsculas, así que el orden con
            # lower() no importa.
            plegar = self._plegar
            tokens = [t if t.isascii() else plegar(t) for t in tokens]
        return tokens

    def _plegar(self, t: str) -> str:
        r = self._plegados.get(t)
        if r is None:
            if len(self._plegados) > 500_000:
                self._plegados.clear()
            r = self._plegados[t] = t.translate(self.plegado)
            if self.p.minusculas:
                r = self._plegados[t] = r.lower()
        return r

    def _agregar_regla(self, izq: str, der: str) -> None:
        lhs = tuple(self._tokens_simples(izq))
        rhs = self._tokens_simples(der)
        if not lhs or not rhs:
            return
        lista = self.reglas.setdefault(lhs[0], [])
        # Como SynonymMap de Lucene: si la misma frase aparece dos veces, se
        # queda la primera (los nombres se cargan antes que el tesauro).
        if any(r[0] == lhs for r in lista):
            return
        lista.append((lhs, rhs))

    def _sinonimos(self, tokens: list[str]) -> list[str]:
        salida: list[str] = []
        i = 0
        n = len(tokens)
        reglas = self.reglas
        while i < n:
            t = tokens[i]
            candidatas = reglas.get(t)
            if candidatas:
                for lhs, rhs in candidatas:
                    largo = len(lhs)
                    if tuple(tokens[i : i + largo]) == lhs:
                        salida.extend(rhs)
                        i += largo
                        break
                else:
                    salida.append(t)
                    i += 1
            else:
                salida.append(t)
                i += 1
        return salida

    def raiz(self, t: str) -> str:
        r = self._raices.get(t)
        if r is None:
            if len(self._raices) > 2_000_000:
                self._raices.clear()
            r = self._raices[t] = porter(t)
        return r

    def analizar(self, texto: str) -> list[str]:
        p = self.p
        if p.fechas:
            texto = normalizar_fechas(texto)
        if p.numeros:
            texto = normalizar_numeros(texto)
        tokens = self._tokens_simples(texto)
        lo, hi = p.largo_min, p.largo_max
        if lo > 1 or hi < 10_000:
            tokens = [t for t in tokens if lo <= len(t) <= hi]
        if self.reglas:
            tokens = self._sinonimos(tokens)
        if self.stop:
            stop = self.stop
            tokens = [t for t in tokens if t not in stop]
        if p.stemming == "porter":
            cache = self._raices
            raiz = self.raiz
            tokens = [cache.get(t) or raiz(t) for t in tokens]
        return tokens

    def frecuencias(self, texto: str) -> Counter:
        return Counter(self.analizar(texto))
