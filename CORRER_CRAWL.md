# Cómo correr la descarga (~30 GB)

Guía paso a paso para ejecutar los dos arañadores en tu propia computadora (Windows, macOS o Linux).

## 1. Qué vas a hacer

Vas a abrir **dos terminales** y correr un arañador en cada una al mismo tiempo. El **propio** (`crawler`) apunta a unos **20 GB** de texto plano y **Scrapy** a unos **10 GB** (el mínimo del curso es 10 GB; nuestra meta es **30 GB** en total, y cada GB extra sobre 10 da un punto extra). Según tu conexión, puede tardar **aprox. 15 a 24 horas**. Puedes **pausar** (Ctrl+C) y **reanudar** más tarde con el mismo comando.

**Importante: la entrega es el 1 de octubre a las 10:00 p.m. y la descarga tarda cerca de un día. Arrancala lo antes posible.**

## 2. Antes de empezar (checklist)

- [ ] **Python 3.10+** instalado. Comprueba:

```powershell
python --version
```

```bash
python --version
```

(Si falla, prueba `py --version` en Windows o `python3 --version` en macOS/Linux.)

- [ ] **Git** instalado.
- [ ] **Al menos 45 GB libres** en disco (30 GB de texto + base de datos + margen). Al arrancar, cada arañador imprime un **WARNING** si el espacio libre es menor que su meta de GB más **5 GB** de margen. Si quedan **menos de 2 GB**, el arañador **se detiene solo** para no llenar el disco.
- [ ] **Conexión estable** (evita VPN o redes de escuela que bloqueen sitios).
- [ ] Computadora **enchufada** y **sin suspensión** mientras corre la descarga:
  - **macOS:** en una tercera terminal deja corriendo:

```bash
caffeinate -dimsu
```

  O arranca el propio arañador ya envuelto en `caffeinate`:

```bash
caffeinate -i python -m crawler.main
```

  - **Windows:** Configuración > Sistema > Energía > **Suspender = Nunca** (mientras dure el crawl).
  - **Linux:** puedes usar el prefijo `systemd-inhibit` (ejemplo para el arañador propio):

```bash
systemd-inhibit --what=idle:sleep:handle-lid-switch --why="RIT crawl" python -m crawler.main
```

## 3. Instalación (una sola vez)

Clona el repo, entra al directorio, crea el entorno virtual e instala dependencias. **Siempre** trabaja desde la **raíz del repo** (`rit-videogames/`).

```bash
git clone https://github.com/Villarley/rit-videogames.git
cd rit-videogames
python -m venv .venv
```

**Activar el entorno virtual**

Windows (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
```

Si PowerShell bloquea la activación:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

macOS / Linux:

```bash
source .venv/bin/activate
```

**Instalar paquetes** (con el venv activado):

```bash
pip install -r requirements.txt
```

Si ya tenías el repo clonado, actualiza antes de correr:

```bash
git pull
```

## 4. Arrancar la descarga

Abre **dos terminales**. En **cada una**: `cd` al repo y activa el venv. Luego:

**Terminal 1 (arañador propio, meta 20 GB, 32 hilos por defecto):**

```bash
python -m crawler.main
```

**Terminal 2 (Scrapy, meta 10 GB):**

```bash
python -m scrapy_crawler.run
```

Al inicio verás una línea tipo `CRAWLER seeds=...` o `SCRAPY seeds=...` con `mode=fresh` o `mode=resuming`.

### Qué significa la línea `PROGRESS`

Cada ~20 segundos aparece una línea en stdout. Ejemplo del **arañador propio**:

```
PROGRESS 15:04:35 saved=2332/2332 text=0.03/20.0GB (0.2%) eta=24h34m rate=19.40p/s 0.23MB/s domains=29 inflight=7 errors=71 blocked=2
```

| Campo | Significado |
|-------|-------------|
| `15:04:35` | Hora local del reporte |
| `saved=2332/2332` | Páginas guardadas **en esta ejecución** / total en la base para este arañador |
| `text=0.03/20.0GB (0.2%)` | GB acumulados de este arañador hacia su meta |
| `eta=24h34m` | Tiempo estimado para llegar a la meta (puede ser `?` al principio) |
| `rate=19.40p/s 0.23MB/s` | Velocidad reciente (páginas y megabytes por segundo) |
| `domains=29` | Dominios con trabajo activo |
| `inflight=7` | Descargas en curso; como máximo una por dominio (política de cortesía), así que suele ser menor que el número de hilos |
| `errors=71` | Fallos de red, HTTP, etc. (normal en crawls largos) |
| `blocked=2` | Dominios bloqueados por muchos fallos seguidos (el crawler los respeta) |

Velocidad esperada: entre los dos arañadores, ~1.3 a 1.5 GB por hora; al inicio la ETA fluctúa, es normal.

En **Scrapy**, la línea es similar pero en lugar de `domains` / `inflight` / `blocked` verás `pending_requests=` (cola + peticiones en curso) y `errors=`.

Los **403** y entradas **blocked** son **normales**: algunos sitios no quieren bots; nosotros obedecemos `robots.txt` y dejamos de insistir.

## 5. Ver el progreso total

En una **tercera terminal** (venv activado, raíz del repo):

```bash
python -m analysis.progress
```

Muestra GB y páginas por `crawler_source`, totales hacia la meta de 30 GB, dominios top e idiomas. Solo lectura sobre `repository/crawl.db`.

Opcional: otra ruta a la base:

```bash
python -m analysis.progress --db repository/crawl.db
```

## 6. Pausar y reanudar

1. Pulsa **Ctrl+C una sola vez** en la terminal del arañador.
2. Espera a que aparezca la línea **`SUMMARY`** (parada ordenada).
3. **No pulses Ctrl+C dos veces** salvo que esté colgado (la segunda en Scrapy fuerza salida).

Para **reanudar**, ejecuta **exactamente el mismo comando** de antes:

```bash
python -m crawler.main
```

```bash
python -m scrapy_crawler.run
```

- **Propio:** la cola vive en SQLite (`frontier` en `repository/crawl.db`).
- **Scrapy:** estado en `repository/scrapy_job/` (JOBDIR).

Si reiniciaste la computadora, mismo procedimiento: mismos comandos, mismo venv.

**No borres** la carpeta `repository/`. **No uses** `--fresh` (solo borra cola/ JOBDIR y pide confirmación; no hace falta para reanudar).

## 7. Cuándo termina

Cada arañador **se detiene solo** al alcanzar su meta de GB. En `SUMMARY` verás `stop_reason=target_reached`.

Si uno termina antes y el **total** sigue por debajo de 30 GB, puedes subir su meta y volver a lanzarlo (sigue desde donde quedó). Ejemplo:

```bash
python -m crawler.main --target-gb 25
```

```bash
python -m scrapy_crawler.run --target-gb 12
```

(`--target-gb 0` desactiva tope de tamaño; no lo uses salvo que lo acordéis en el equipo.)

## 8. Al terminar

Genera estadísticas y gráficas (tarda **~10 minutos** con decenas de GB):

```bash
python -m analysis.stats --export-metadata
```

Salida por defecto en `analysis/output/` (`stats.md`, `stats.json`, `zipf.png`, `top_words.png`, `domains.png`, `word_freq_top1000.csv`, y con `--export-metadata` también `metadata.csv.gz`).

### Cómo entregar los datos

La carpeta **`repository/`** (texto, `crawl.db`, logs) **no va en git** (es enorme). Opciones:

**Comprimir** (macOS / Linux / Windows 10+ con `tar` en PowerShell), desde la raíz del repo:

```bash
tar -czf rit-repositorio.tar.gz repository analysis/output
```

Comparte el **`rit-repositorio.tar.gz`** con Santiago (USB, Drive, etc.).

Los resultados en **`analysis/output/`** sí son pequeños y **se pueden subir al repo**:

```bash
git add analysis/output
git commit -m "Estadisticas del repositorio de crawl"
git push
```

**Mandale a Santiago el tar.gz; los resultados de `analysis/output` sí se pueden subir al repo.**

## 9. Problemas comunes

| Problema | Qué hacer |
|----------|-----------|
| `command not found: python` | Prueba `python3` o en Windows `py -m venv .venv` y `py -m crawler.main` |
| `pip install` falla con **lxml** (Windows) | `python -m pip install --upgrade pip` y repite `pip install -r requirements.txt` |
| **database is locked** | Poco frecuente con WAL; cierra otras apps que abran `crawl.db` y vuelve a ejecutar el mismo comando |
| **PROGRESS** con `0.00p/s` mucho rato | Revisa internet; Ctrl+C una vez, espera SUMMARY, relanza |
| Disco casi lleno | El crawl para solo (`low_disk` o aviso); libera espacio y relanza |
| Muchos **403** en logs | Normal en sitios que bloquean bots |
| Scrapy no reanuda | No borres `repository/scrapy_job/`; no uses `--fresh` |

## 10. Qué NO hacer

- No edites **`seeds/seeds.csv`** mientras corre un crawl.
- No abras **`repository/crawl.db`** con un editor de base de datos **durante** la ejecución (herramientas de solo lectura como `analysis.progress` están bien).
- No ejecutes **dos copias del mismo arañador** a la vez (comparten `crawl.db` y/o JOBDIR).
- No corras los dos arañadores **sin** el venv activado ni desde otra carpeta que no sea la raíz del repo.

---

Dudas de flags avanzados: `python -m crawler.main --help` y `python -m scrapy_crawler.run --help`. Visión general del proyecto: [README.md](README.md).
