# Guía: correr los indexadores (Proyecto 2) y el buscador (Proyecto 3)

Para quien tenga la colección completa (`repository/` con `crawl.db` y los `.txt`). Todo se corre desde la raíz del repo.

## 1. Preparar (una vez)

Requisitos: Python 3.10 o más nuevo, Java 17 o más nuevo y Maven.

```bash
git pull
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd lucene && mvn -q package && cd ..                   # deja lucene/target/indexador-lucene.jar
python -m pytest -q                                    # todo tiene que pasar
```

Si la colección no está en `repository/`, cambiar `raiz` y `base` en la sección `[coleccion]` de los tres archivos `config/*.conf`.

## 2. Prueba corta (5 minutos)

```bash
python -m indexer.benchmark config/basico.conf config/stop.conf config/completo.conf --limite 20000
```

Indexa los primeros 20 000 documentos con los dos indexadores y las tres políticas. Al final imprime la tabla de comparación y una línea por política que tiene que decir que el vocabulario y los df de los dos índices son iguales. Si eso sale bien, borrar `indices/` y seguir.

## 3. Corrida completa

```bash
python -m indexer.benchmark config/basico.conf config/stop.conf config/completo.conf
```

Cuánto tarda: la colección del Proyecto 1 tiene 1 792 784 documentos y 9.4 GiB de texto. En una máquina de 4 núcleos, con la muestra de prueba, el indexador propio fue a 8.5 MiB/s (`completo`) y 13 MiB/s (`basico`), y Lucene más o menos igual. Eso da entre 12 y 20 minutos por índice, unas 2 horas para los seis. Con 8 núcleos, más o menos la mitad. Se puede correr una política sola pasándole un solo `.conf`.

Espacio libre: unos 15 GB (los índices quedan entre el 13% y el 23% del tamaño del texto, y la mezcla del propio usa archivos temporales en `indices/tmp/`).

Memoria: cada proceso indexa 20 000 documentos a la vez (`docs_por_bloque`), unos 100 MiB de texto. Con 16 GB de RAM conviene `--procesos 4`. Si se queda sin memoria, bajar `docs_por_bloque` en el `.conf`.

Lo que queda:

```
indices/basico.idx  indices/stop.idx  indices/completo.idx       índice propio (+ .json con estadísticas)
indices/lucene-basico/  indices/lucene-stop/  indices/lucene-completo/   Lucene (+ .json)
indices/reporte/comparacion.csv      tiempo, tamaño, vocabulario y postings de los 6 índices
indices/reporte/verificacion.txt     si los dos indexadores dieron el mismo vocabulario
indices/reporte/estadisticas_*.json  términos con mayor df, hapax, largo de documentos...
indices/reporte/*.png                gráficos para el documento
```

Subir `indices/reporte/` y los `.json` al Drive del grupo. Los `.idx` no van a git.

Los indexadores se pueden volver a correr cuando sea, con la araña apagada o encendida: solo leen `crawl.db` en modo lectura.

## 4. Revisar el archivo binario

```bash
python -m indexer.inspeccionar indices/completo.idx                  # encabezado y secciones
python -m indexer.inspeccionar indices/completo.idx --termino zelda  # registro, texto y postings
python -m indexer.inspeccionar indices/completo.idx --top 30         # términos con mayor df
hexdump -C -n 128 indices/completo.idx                               # encabezado
```

Cada salida de `inspeccionar` trae el comando de `hexdump` que muestra esos mismos bytes. El formato está explicado en `FORMATO_INDICE.md`.

Con Lucene:

```bash
java -jar lucene/target/indexador-lucene.jar buscar config/completo.conf "elden ring boss"
java -jar lucene/target/indexador-lucene.jar analizar config/completo.conf <<< "The Legend of Zelda salió el 3 de marzo de 2017"
```

## 5. Buscador (Proyecto 3)

```bash
python -m buscador --indice indices/completo.idx --indice indices/stop.idx --indice indices/basico.idx
```

Abre en http://127.0.0.1:5000. Tiene tres modos: Buscar, Evaluar y Métricas. El primer `--indice` es el que se usa por defecto.

El buscador solo necesita el `.idx` y `crawl.db`. Los `.txt` sirven para mostrar el fragmento de cada resultado; si no están, los resultados salen sin fragmento. O sea que se puede llevar `indices/completo.idx` (unos 2 GB) y `repository/crawl.db` (3 GB) a otra compu, con `--base` y `--raiz`, sin copiar los 10 GB de texto.

Para que otras personas evalúen desde su compu en la misma red: `--host 0.0.0.0` y pasarles la IP. Desde fuera de la red, un túnel como `cloudflared tunnel --url http://localhost:5000`.

Los juicios y la bitácora de consultas quedan en `buscador/datos/evaluaciones.db`. Antes de la entrega, en Métricas está "Exportar juicios (JSON)" y "Corridas (CSV)", que son los datos para la sección de estadísticas de uso del documento.

Las necesidades de información del modo evaluación están en `buscador/necesidades.json` y se pueden cambiar sin tocar código.
