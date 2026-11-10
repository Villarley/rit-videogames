"""Algoritmo de Porter (1980) para raíces en inglés.

Es el mismo algoritmo de "porter.pl" de la Tarea05, pero escrito como la versión
en C que publicó Martin Porter (tartarus.org/martin/PorterStemmer), que es de
donde sale el PorterStemmer de Lucene. La razón es que el Proyecto 2 pide que el
indexador propio y el de Lucene normalicen igual: si uso el algoritmo del paper
tal cual, hay palabras donde los dos no coinciden. La versión en C tiene tres
cambios chicos respecto al paper de 1980, y los dejo marcados abajo:

1. En el paso 2, "bli" -> "ble" en lugar de "abli" -> "able".
2. En el paso 2 se agrega "logi" -> "log".
3. Las reglas se escogen por la penúltima letra (un switch), lo que en la
   práctica da el mismo sufijo más largo que pide el paper.

La prueba "tests/test_porter.py" compara esto contra un listado de palabras que
sacó el propio Lucene.

La palabra se maneja como en C: una lista de letras ``b``, ``k`` es el índice de
la última letra y ``j`` marca dónde empieza el sufijo que se está probando.
"""
from __future__ import annotations

_VOCALES = frozenset("aeiou")


class _Porter:
    __slots__ = ("b", "k", "j")

    def __init__(self, palabra: str) -> None:
        self.b = palabra
        self.k = len(palabra) - 1
        self.j = 0

    # --- utilidades del paper (ver porter.pl: es_consonante, medida, etc.)
    def cons(self, i: int) -> bool:
        c = self.b[i]
        if c in _VOCALES:
            return False
        if c == "y":
            return True if i == 0 else not self.cons(i - 1)
        return True

    def m(self) -> int:
        """Medida m de b[0..j]: cuántas veces aparece VC."""
        n = 0
        i = 0
        j = self.j
        while True:
            if i > j:
                return n
            if not self.cons(i):
                break
            i += 1
        i += 1
        while True:
            while True:
                if i > j:
                    return n
                if self.cons(i):
                    break
                i += 1
            i += 1
            n += 1
            while True:
                if i > j:
                    return n
                if not self.cons(i):
                    break
                i += 1
            i += 1

    def vocal_en_raiz(self) -> bool:
        return any(not self.cons(i) for i in range(self.j + 1))

    def doble(self, j: int) -> bool:
        return j >= 1 and self.b[j] == self.b[j - 1] and self.cons(j)

    def cvc(self, i: int) -> bool:
        if i < 2 or not self.cons(i) or self.cons(i - 1) or not self.cons(i - 2):
            return False
        return self.b[i] not in "wxy"

    def termina(self, s: str) -> bool:
        n = len(s)
        if n > self.k + 1:
            return False
        if self.b[self.k - n + 1 : self.k + 1] != s:
            return False
        self.j = self.k - n
        return True

    def poner(self, s: str) -> None:
        self.b = self.b[: self.j + 1] + s + self.b[self.k + 1 :]
        self.k = self.j + len(s)

    def r(self, s: str) -> None:
        if self.m() > 0:
            self.poner(s)

    # --- pasos
    def paso1ab(self) -> None:
        # 1a: plurales
        if self.b[self.k] == "s":
            if self.termina("sses"):
                self.k -= 2
            elif self.termina("ies"):
                self.poner("i")
            elif self.b[self.k - 1] != "s":
                self.k -= 1
        # 1b: -eed, -ed, -ing
        if self.termina("eed"):
            if self.m() > 0:
                self.k -= 1
        elif (self.termina("ed") or self.termina("ing")) and self.vocal_en_raiz():
            self.k = self.j
            if self.termina("at"):
                self.poner("ate")
            elif self.termina("bl"):
                self.poner("ble")
            elif self.termina("iz"):
                self.poner("ize")
            elif self.doble(self.k):
                self.k -= 1
                if self.b[self.k] in "lsz":
                    self.k += 1
            else:
                self.j = self.k
                if self.m() == 1 and self.cvc(self.k):
                    self.poner_final("e")

    def poner_final(self, s: str) -> None:
        self.j = self.k
        self.poner(s)

    def paso1c(self) -> None:
        if self.termina("y") and self.vocal_en_raiz():
            self.b = self.b[: self.k] + "i" + self.b[self.k + 1 :]

    def paso2(self) -> None:
        if self.k == 0:
            return
        c = self.b[self.k - 1]
        reglas = _PASO2.get(c, ())
        for sufijo, nuevo in reglas:
            if self.termina(sufijo):
                self.r(nuevo)
                return

    def paso3(self) -> None:
        for sufijo, nuevo in _PASO3.get(self.b[self.k], ()):
            if self.termina(sufijo):
                self.r(nuevo)
                return

    def paso4(self) -> None:
        if self.k == 0:
            return
        c = self.b[self.k - 1]
        if c == "o":
            if self.termina("ion") and self.j >= 0 and self.b[self.j] in "st":
                pass
            elif self.termina("ou"):
                pass
            else:
                return
        else:
            for sufijo in _PASO4.get(c, ()):
                if self.termina(sufijo):
                    break
            else:
                return
        if self.m() > 1:
            self.k = self.j

    def paso5(self) -> None:
        self.j = self.k
        if self.b[self.k] == "e":
            a = self.m()
            if a > 1 or (a == 1 and not self.cvc(self.k - 1)):
                self.k -= 1
        if self.b[self.k] == "l" and self.doble(self.k) and self.m() > 1:
            self.k -= 1

    def raiz(self) -> str:
        if self.k > 1:
            self.paso1ab()
            self.paso1c()
            self.paso2()
            self.paso3()
            self.paso4()
            self.paso5()
        return self.b[: self.k + 1]


_PASO2 = {
    "a": (("ational", "ate"), ("tional", "tion")),
    "c": (("enci", "ence"), ("anci", "ance")),
    "e": (("izer", "ize"),),
    "l": (("bli", "ble"), ("alli", "al"), ("entli", "ent"), ("eli", "e"), ("ousli", "ous")),
    "o": (("ization", "ize"), ("ation", "ate"), ("ator", "ate")),
    "s": (("alism", "al"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous")),
    "t": (("aliti", "al"), ("iviti", "ive"), ("biliti", "ble")),
    "g": (("logi", "log"),),
}

_PASO3 = {
    "e": (("icate", "ic"), ("ative", ""), ("alize", "al")),
    "i": (("iciti", "ic"),),
    "l": (("ical", "ic"), ("ful", "")),
    "s": (("ness", ""),),
}

_PASO4 = {
    "a": ("al",),
    "c": ("ance", "ence"),
    "e": ("er",),
    "i": ("ic",),
    "l": ("able", "ible"),
    "n": ("ant", "ement", "ment", "ent"),
    "s": ("ism",),
    "t": ("ate", "iti"),
    "u": ("ous",),
    "v": ("ive",),
    "z": ("ize",),
}


def porter(palabra: str) -> str:
    """Raíz de Porter de una palabra ya en minúsculas."""
    if len(palabra) <= 2:
        return palabra
    return _Porter(palabra).raiz()
