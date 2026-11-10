"""Genera una colección de prueba con el mismo formato que deja la araña.

La colección real (~30 GB) no está en esta máquina. Para probar los
indexadores y medir velocidad se arma una muestra: repository/crawl.db con la
tabla pages y un .txt por página, igual que crawler/storage.py.

El texto se sortea con las frecuencias reales de la colección completa
(frecuencias.txt de la Tarea05, "palabra => frecuencia"), así la distribución
de Zipf y el vocabulario se parecen a los reales. Además se meten frases con
fechas, números y nombres de juegos para que la normalización tenga qué hacer.

    python tools/generar_muestra.py --frecuencias frecuencias.txt --docs 20000 --salida muestra
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.storage import Repository  # noqa: E402

FRASES = [
    "The Legend of Zelda: Breath of the Wild was released on March 3, 2017 for the Nintendo Switch.",
    "Elden Ring sold more than 25,000,000 copies by 2025-06-30 according to Bandai Namco.",
    "Grand Theft Auto V launched on 17 September 2013 and is one of the best-selling video games.",
    "Call of Duty: Modern Warfare III arrives on 11/10/2023 for PlayStation 5 and Xbox Series X.",
    "El videojuego Hollow Knight salió el 24 de febrero de 2017 y tiene más de 3,000,000 de jugadores.",
    "Stardew Valley is a farming simulation role-playing game released on February 26, 2016.",
    "Minecraft is the best-selling videogame of all time, with over 300,000,000 copies sold.",
    "Super Mario Bros. Wonder was released on October 20th, 2023 for the Nintendo Switch.",
    "League of Legends World Championship 2024 final was played on 2 Nov 2024 in London.",
    "Los juegos de rol multijugador como World of Warcraft tienen misiones, jefes y logros.",
    "Baldur's Gate 3 won Game of the Year at The Game Awards on December 7, 2023.",
    "The patch notes for Terraria 1.4.4 list 1,200 changes to bosses, weapons and NPCs.",
]
DOMINIOS = ["minecraft.wiki", "terraria.wiki.gg", "www.mariowiki.com", "zeldawiki.wiki",
            "en.wikipedia.org", "es.wikipedia.org", "www.pcgamer.com", "www.3djuegos.com",
            "eldenring.wiki.gg", "stardewvalleywiki.com", "www.ign.com", "kotaku.com"]


def cargar_vocabulario(ruta: Path, maximo: int) -> tuple[list[str], np.ndarray]:
    palabras, frec = [], []
    with open(ruta, encoding="utf-8", errors="replace") as fh:
        for linea in fh:
            if " => " not in linea:
                continue
            w, f = linea.rstrip("\r\n").rsplit(" => ", 1)
            palabras.append(w)
            frec.append(int(f))
            if len(palabras) >= maximo:
                break
    p = np.array(frec, dtype=np.float64)
    return palabras, p / p.sum()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frecuencias", required=True)
    ap.add_argument("--docs", type=int, default=20000)
    ap.add_argument("--salida", default="muestra")
    ap.add_argument("--vocabulario", type=int, default=400_000)
    ap.add_argument("--semilla", type=int, default=7)
    a = ap.parse_args()

    rng = np.random.default_rng(a.semilla)
    palabras, p = cargar_vocabulario(Path(a.frecuencias), a.vocabulario)
    salida = Path(a.salida)
    repo = Repository(str(salida / "crawl.db"), str(salida))
    largos = np.clip(rng.lognormal(6.5, 0.9, a.docs).astype(int), 60, 20000)
    for i in range(a.docs):
        idx = rng.choice(len(palabras), size=largos[i], p=p)
        texto = " ".join(palabras[j] for j in idx)
        frases = rng.choice(len(FRASES), size=rng.integers(0, 4), replace=False)
        texto = " ".join(FRASES[f] for f in frases) + "\n" + texto
        dominio = DOMINIOS[i % len(DOMINIOS)]
        h = hashlib.sha256(texto.encode()).hexdigest()
        titulo = " ".join(palabras[j] for j in idx[:6]).title()
        url = f"https://{dominio}/wiki/Pagina_{i}"
        repo.save_page(
            url=url, final_url=url, seed_id="muestra", depth=1, parent_url=None,
            http_status=200, content_type="text/html", title=titulo, text=texto,
            content_hash=h, language="es" if dominio.startswith("es.") or "3djuegos" in dominio else "en",
            crawler_source="custom" if i % 2 else "scrapy",
        )
        if (i + 1) % 2000 == 0:
            print(f"{i + 1} documentos")
    repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
