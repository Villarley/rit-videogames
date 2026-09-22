# El Arañador — Recuperación de Información Textual

Proyecto del curso **Recuperación de Información Textual** (TEC) sobre el tema **videojuegos**.

Este repositorio contiene **dos implementaciones de crawler** que alimentan un mismo repositorio compartido de metadatos (SQLite) y texto plano (`repository/`):

1. **Crawler personalizado con hilos** — en `crawler/`: implementación propia en Python con descarga concurrente, políticas de rastreo y extracción de texto.
2. **Crawler basado en Scrapy** — en `scrapy_crawler/` (se añadirá en un paso posterior): segunda implementación que escribe en el mismo esquema, identificada por `crawler_source`.

Ambos crawlers descargan páginas relacionadas con videojuegos, extraen texto limpio y lo persisten en archivos bajo `repository/` junto con metadatos en la base de datos SQLite.

## Integrantes

- Sebastián Calvo Hernández
- Santiago Villarreal Arley
- José Julián Brenes Garro

## Estructura del proyecto

```
rit-videogames/
├── crawler/           # Crawler personalizado (hilos)
├── scrapy_crawler/    # Crawler Scrapy (pendiente)
├── repository/        # Texto plano generado por los crawlers
├── seeds/             # URLs semilla (seeds.csv)
└── requirements.txt
```

## Cómo ejecutar

Una vez exista `crawler/main.py`, el crawler personalizado se ejecutará así:

```bash
python -m crawler.main --seeds seeds/seeds.csv --max-depth 3 --max-pages-per-domain 200 --threads 8
```

## Requisitos

```bash
pip install -r requirements.txt
```
