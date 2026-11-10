# Formato del archivo de índice `.idx`

Un solo archivo binario por política (`indices/basico.idx`, `indices/stop.idx`, `indices/completo.idx`). Lo escribe `indexer/construir.py` y lo lee `indexer/formato.py`. Todos los enteros son little-endian y sin signo. El archivo se puede revisar con `hexdump -C`, y `python -m indexer.inspeccionar` imprime el comando exacto de hexdump para cada parte.

## Vista general

```
offset 0     ENCABEZADO (128 bytes)
offset 128   CONF  sección: la política (.conf sin comentarios), texto UTF-8
             POST  sección: listas de postings comprimidas (variable byte)
             TERM  sección: el texto de cada término, separados por un byte 00
             VOCA  sección: un registro de 24 bytes por término (df, cf, idf, offsets)
             DOCS  sección: un registro de 16 bytes por documento (page_id, largo, norma)
```

Cada sección empieza con una cabecera de 16 bytes y se rellena con ceros hasta un múltiplo de 16. Por eso en hexdump cada sección arranca al inicio de una línea y la etiqueta se lee en la columna de texto (`|CONF....`, `|POST....`, etc.).

| Cabecera de sección | Tipo | Bytes |
|---|---|---|
| etiqueta (`CONF`, `POST`, `TERM`, `VOCA`, `DOCS`) | ASCII | 4 |
| cantidad de elementos | u32 | 4 |
| largo de los datos en bytes (sin relleno) | u64 | 8 |

## Encabezado (bytes 0 a 127)

| Offset | Campo | Tipo | Ejemplo (`completo.idx` de la muestra) |
|---|---|---|---|
| 0 | mágico `RITIDX01` | 8 bytes ASCII | `52 49 54 49 44 58 30 31` |
| 8 | versión | u16 | `01 00` = 1 |
| 10 | banderas de la política | u16 | `ff 00`: los 8 bits encendidos |
| 12 | N, documentos | u32 | `20 4e 00 00` = 20 000 |
| 16 | V, términos | u32 | `a7 44 03 00` = 214 183 |
| 20 | reservado | u32 | 0 |
| 24 | tokens en la colección | u64 | |
| 32 | offset y largo de CONF | u64 + u64 | |
| 48 | offset y largo de POST | u64 + u64 | |
| 64 | offset y largo de TERM | u64 + u64 | |
| 80 | offset y largo de VOCA | u64 + u64 | |
| 96 | offset y largo de DOCS | u64 + u64 | |
| 112 | fecha de construcción (segundos Unix) | u64 | |
| 120 | reservado | u64 | 0 |

Los offsets de la tabla apuntan al primer byte de datos de cada sección, o sea 16 bytes después de su etiqueta.

Bits de las banderas, del menos significativo al más significativo: plegado, minúsculas, fechas, números, stopwords, tesauro, nombres y Porter. `basico` da `0x03`, `stop` da `0x1f` y `completo` da `0xff`.

## VOCA y TERM: el diccionario

VOCA tiene V registros fijos de 24 bytes, en el orden de los bytes UTF-8 del término. Como son de tamaño fijo, el registro del término i está en `VOCA + 24 i` y se puede hacer búsqueda binaria sin cargar nada a memoria.

| Offset en el registro | Campo | Tipo |
|---|---|---|
| 0 | dónde empieza el texto del término dentro de TERM | u32 |
| 4 | df, documentos que lo contienen | u32 |
| 8 | cf, apariciones en toda la colección | u32 |
| 12 | idf = log10(N / df) | f32 |
| 16 | dónde empieza su lista dentro de POST | u64 |

El largo de una lista es el `post_off` del siguiente término menos el propio, y el largo de un término es igual con `term_off` (menos el 00 que lo separa). Guardar los términos juntos en TERM y no dentro de cada registro es el "diccionario como cadena" de Manning et al. (2008, sección 5.2.1): ahorra el espacio de un campo de largo fijo para la palabra más larga.

Ejemplo con el término `zeldabotw` (lo que queda de "Breath of the Wild" después de la regla de `config/nombres.txt`):

```
$ python -m indexer.inspeccionar indices/completo.idx --termino zeldabotw
término #183181: 'zeldabotw'
  df=2,512  cf=2,512  idf=0.9010

$ hexdump -C -s 20366104 -n 24 indices/completo.idx          (registro VOCA)
0136c318  5d 4c 17 00 d0 09 00 00  d0 09 00 00 9d a8 66 3f  |]L............f?|
0136c328  e8 62 d0 00 00 00 00 00                           |.b......|
```

`5d 4c 17 00` es term_off = 1 526 877, `d0 09 00 00` es df = 2 512, el siguiente `d0 09 00 00` es cf = 2 512, `9d a8 66 3f` es el float 0.9010 y `e8 62 d0 00 00 00 00 00` es post_off = 13 656 808.

```
$ hexdump -C -s 15534925 -n 10 indices/completo.idx          (TERM)
00ed0b4d  7a 65 6c 64 61 62 6f 74  77 00                    |zeldabotw.|
```

## POST: las listas de postings

La lista de un término tiene dos partes seguidas:

1. df códigos, uno por documento en orden creciente. El código es `(distancia << 1) | (tf == 1)`, donde la distancia es la diferencia con el documento anterior (el primero se guarda completo).
2. Los tf de los documentos cuyo código tenía el bit bajo en 0, en el mismo orden.

Todos los números van en variable byte (Manning et al. 2008, sección 5.3.1): 7 bits por byte, del menos al más significativo, y el bit alto en 1 indica que el número sigue en el próximo byte. Por ejemplo, 300 se guarda `ac 02`.

El bit para tf = 1 es el mismo truco del formato Lucene50 de Lucene: como la mayoría de los términos aparecen una sola vez en cada documento, casi ningún posting necesita un byte para su tf. En la muestra eso bajó el índice `completo` de 28.4 MiB a 20.4 MiB.

```
$ hexdump -C -s 13657512 -n 16 indices/completo.idx          (POST de zeldabotw)
00d065a8  05 05 05 0f 11 13 03 0f  11 05 13 11 05 15 33 67  |..............3g|
```

`05` = 0b101: distancia 2 y tf = 1, entonces el primer documento es el 2. El segundo `05` suma 2 (documento 4) y el tercero otros 2 (documento 6). `0f` = 0b1111 es distancia 7 con tf = 1, o sea el documento 13. Eso coincide con lo que imprime `inspeccionar` (2, 4, 6...).

## DOCS: la tabla de documentos

N registros de 16 bytes. El número interno de un documento (el que aparece en los postings) es su posición en esta tabla.

| Offset | Campo | Tipo |
|---|---|---|
| 0 | page_id, el id de la tabla `pages` de `crawl.db` | u32 |
| 4 | largo: términos después de normalizar | u32 |
| 8 | términos distintos | u32 |
| 12 | norma del vector TF-IDF | f32 |

```
01421d90  44 4f 43 53 20 4e 00 00  00 e2 04 00 00 00 00 00  |DOCS N..........|
01421da0  01 00 00 00 ac 01 00 00  85 01 00 00 9f e0 fb 41  |...............A|
```

La primera línea es la cabecera de la sección (DOCS, 20 000 registros, 320 000 bytes). El documento 0 tiene page_id 1, 428 términos, 389 distintos y norma 31.48.

La URL y el título no se repiten en el índice: el buscador los saca de `crawl.db` con el page_id. Con 1.8 millones de páginas eso son cientos de MB que el índice no tiene que cargar.

## El modelo vectorial que queda guardado

Con lo que trae el archivo se calcula el coseno sin volver a leer la colección:

```
w(t, d) = (1 + log10 tf) * idf(t)        idf(t) = log10(N / df)       (VOCA)
|d|     = raíz de la suma de w(t, d)²                                 (DOCS)
coseno(q, d) = suma de w(t, q) * w(t, d) / (|q| * |d|)
```

Es el esquema ltc.ltc de SMART (Salton y Buckley 1988). Lucene, con `ClassicSimilarity`, usa otra variante del mismo modelo: tf = raíz de la frecuencia, idf = 1 + ln(N / (df + 1)) y normaliza por 1 / raíz del largo del documento.

## Cómo se construye

SPIMI (Heinz y Zobel 2003; Manning et al. 2008, sección 4.3). Detalle en el docstring de `indexer/construir.py`:

1. Se leen los documentos de `crawl.db` en orden de id y se cortan en bloques de `docs_por_bloque`.
2. Cada proceso indexa un bloque en memoria y lo escribe ordenado a `indices/tmp/<política>/bloque_NNNNN.bin`.
3. Se mezclan los bloques con `heapq.merge`. Como cada bloque cubre documentos consecutivos, las listas de un término se pegan una tras otra y solo se recalcula la primera distancia de cada pedazo.
4. Con el df final se calcula el idf, y en una pasada con numpy sobre POST se calcula la norma de cada documento.

## Referencias

- Heinz, S. y Zobel, J. (2003). Efficient single-pass index construction for text databases. Journal of the American Society for Information Science and Technology, 54(8), 713-729.
- Manning, C. D., Raghavan, P. y Schütze, H. (2008). Introduction to Information Retrieval. Cambridge University Press. Capítulos 4, 5 y 6.
- Salton, G. y Buckley, C. (1988). Term-weighting approaches in automatic text retrieval. Information Processing & Management, 24(5), 513-523.
- Porter, M. F. (1980). An algorithm for suffix stripping. Program, 14(3), 130-137.
