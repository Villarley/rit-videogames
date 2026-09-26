# El Arañador: Recuperación de Información Textual

Proyecto del curso **Recuperación de Información Textual** (TEC). Tema: **videojuegos**.

**¿Vas a correr la descarga de ~30 GB?** Leé **[CORRER_CRAWL.md](CORRER_CRAWL.md)** (guía paso a paso para Windows, macOS y Linux).

## Integrantes

- Sebastián Calvo Hernández
- Santiago Villarreal Arley
- José Julián Brenes Garro

## Qué hay aquí

Dos arañadores distintos alimentan **un solo repositorio** (`repository/`): metadatos en SQLite y texto plano en disco.

| Implementación | Comando | Meta por defecto |
|----------------|---------|------------------|
| Propio (hilos) | `python -m crawler.main` | 20 GB |
| Scrapy | `python -m scrapy_crawler.run` | 10 GB |

Meta conjunta del curso: **30 GB** de texto útil sobre videojuegos.

## Estructura del proyecto

```
rit-videogames/
├── crawler/              # Arañador propio (frontier SQLite, hilos)
├── scrapy_crawler/       # Arañador Scrapy (JOBDIR, mismas políticas de contenido)
├── analysis/             # progress, stats, salida en analysis/output/
├── seeds/                # seeds.csv (semillas y partición por arañador)
├── repository/           # Generado al crawlear (no versionado en git)
├── tests/                # Pruebas (pytest)
├── CORRER_CRAWL.md       # Guía operativa para quien corre el crawl
├── requirements.txt
└── README.md
```

## Repositorio de datos

### SQLite: `repository/crawl.db`

| Tabla | Uso principal |
|-------|----------------|
| **pages** | Una fila por URL visitada: `url`, `final_url`, `seed_id`, `depth`, `title`, `text_path`, `word_count`, `content_hash`, `language`, `crawler_source` (`custom` / `scrapy`), `text_bytes`, métricas de enlaces y topicalidad |
| **frontier** | Cola del arañador propio: `url`, `domain`, `depth`, `status` (`pending`, `done`, …), reintentos |
| **links** | Aristas descubiertas (`from_url`, `to_url`) |
| **seeds** | Catálogo de semillas (`seed_id`, `url`, `subtema`, `scope_mode`, `idioma`) |

### Texto en disco

Ruta relativa a `repository/`:

`{crawler_source}/{dominio_sanitizado}/{2_hex_del_hash}/{16_hex_del_hash}.txt`

Contenido **texto plano** (sin HTML), mismo esquema para ambos arañadores.

### Bitácoras

- Propio: `repository/logs/custom/crawl.log` (rotación cada **100 MB**).
- Scrapy: `repository/logs/scrapy/crawl.log`.
- Estado de pausa Scrapy: `repository/scrapy_job/` (JOBDIR).

## Políticas de arañado (implementación propia)

| Política | Dónde | Resumen |
|----------|-------|---------|
| Alcance (`domain` / `prefix` / `topical` + focused crawling) | `crawler/policies.py` (`CrawlPolicies.link_in_scope`, `is_topical`) | Host rules desde semillas; modo topical filtra por densidad de palabras clave de videojuegos |
| Exclusión de URLs no textuales y namespaces MediaWiki | `CrawlPolicies.is_denied` | Extensiones binarias, query keys wiki, paths de namespace |
| `robots.txt` + Crawl-delay | `crawler/fetcher.py` (`RobotsCache`) | Respeta reglas por host |
| Cortesía: 1 solicitud en vuelo por dominio, intervalo mínimo 1 s, backoff 429/503 con Retry-After | `crawler/frontier.py` (`DomainScheduler`) | Cola por dominio estilo Mercator |
| Bloqueo de dominios hostiles (25 fallos consecutivos) | `DomainScheduler.task_done` | Deja de pedir ese host |
| Profundidad máxima y tope por dominio | `DomainScheduler.add_urls` + `crawler/threaded_crawler.py` | Defaults: profundidad 6, 100 000 páginas/dominio |
| Tipo de contenido y tamaño máximo (5 MB) | `fetcher.Fetcher.fetch` | Solo HTML/texto razonable |
| Deduplicación (URL normalizada + hash sha256 del texto) | `policies.normalize_url` + `storage.Repository.save_page` | Evita duplicar contenido |
| Contenido mínimo (50 palabras) | `threaded_crawler` + `CrawlPolicies.min_words` | Descarta páginas muy cortas |
| Frescura / revisita | `storage.Repository.revisit_stale` | Flag `--revisit` (default 7 días con `--revisit-days`) |
| Partición de hosts entre arañadores | `seeds/seeds.csv` columna `crawler` | Evita descargar el mismo host dos veces |

## Políticas en Scrapy (equivalente)

| Política | Setting / componente | Notas |
|----------|----------------------|-------|
| `robots.txt` | `ROBOTSTXT_OBEY = True` | |
| Cortesía y throttle | `DOWNLOAD_DELAY`, `CONCURRENT_REQUESTS_PER_DOMAIN = 1`, `AUTOTHROTTLE_*` | Complementa delay base 1 s |
| Profundidad BFS | `DEPTH_LIMIT`, `DEPTH_PRIORITY`, colas FIFO + `DownloaderAwarePriorityQueue` | Amplitud primero |
| Tamaño de descarga | `DOWNLOAD_MAXSIZE` (5 MB) | |
| Reintentos | `RETRY_HTTP_CODES` (429, 5xx, …) | |
| Pausa / reanudar | `JOBDIR` (`repository/scrapy_job`) | `--jobdir` en `run.py` |
| Dedup de URLs | Dupefilter persistido en JOBDIR | |
| Meta de GB y disco bajo 2 GB | `scrapy_crawler/extensions.py` (`TargetSizeExtension`) | `stop_reason=target_reached` o `low_disk` |
| Mismas reglas de contenido y almacenamiento | Reutiliza `crawler/policies.py`, `crawler/extractor.py`, `crawler/storage.py` | `crawler_source='scrapy'` |

## Semillas: `seeds/seeds.csv`

Columnas:

| Columna | Significado |
|---------|-------------|
| `seed_id` | Identificador estable |
| `url` | URL inicial |
| `subtema` | Etiqueta temática (tienda, wiki, etc.) |
| `scope_mode` | `domain`, `prefix` o `topical` |
| `idioma` | Código esperado (`en`, `es`, …) |
| `crawler` | `custom` o `scrapy` (partición de hosts) |

**65 semillas** en total: **37** para el arañador propio y **28** para Scrapy.

Algunas URLs del listado original quedan **bloqueadas (403)** o **no permitidas por robots.txt** (p. ej. listas en IMDb); la bitácora registra `ROBOTS_DISALLOW`, errores HTTP y dominios bloqueados.

## Uso rápido

Requisitos: Python 3.10+, `pip install -r requirements.txt`, venv recomendado. Detalle completo en **[CORRER_CRAWL.md](CORRER_CRAWL.md)**.

```bash
# Terminal 1
python -m crawler.main

# Terminal 2
python -m scrapy_crawler.run

# Progreso agregado (solo lectura)
python -m analysis.progress

# Al finalizar el crawl (~10 min)
python -m analysis.stats --export-metadata
```

Flags útiles (ver `--help`):

- Propio: `--target-gb`, `--threads`, `--max-depth`, `--revisit`, `--verbose`, `--fresh` (evitar salvo reset de cola).
- Scrapy: `--target-gb`, `--concurrency`, `--jobdir`, `--progress-every`, `--fresh`.

## Estadísticas

`python -m analysis.stats` escribe en `analysis/output/` (JSON, Markdown, CSV, gráficas Zipf y dominios). Opciones: `--db`, `--text-root`, `--out`, `--workers`, `--source custom|scrapy`, `--limit`, `--export-metadata`.

## Pruebas

```bash
python -m pytest
```

## Licencia y uso académico

Crawler identificado con User-Agent de curso; uso responsable de sitios semilla y cumplimiento de `robots.txt`.
