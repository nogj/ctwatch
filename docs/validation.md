# Validación

## Comprobaciones locales

Resultado actual: **112 tests pasados en Python 3.12**, incluyendo un pool real de
procesos con spawn y el flujo DynamoDB/SQS emulado. Lint Python, formato, checksum Unicode,
sincronía de plantillas, `cfn-lint` y consistencia de dependencias (`pip check`) correctos.

- Tests de matching exacto, límites de subdominio, wildcard, IDNA/Punycode, homógrafos y variantes.
- Framing X.509/precertificados en RFC6962 y Static CT, truncamiento y rutas de tiles.
- Firmas de checkpoint y detección de manipulación de raíz/origen.
- Caché exacta, resultados negativos, caducidad e in-flight dedup.
- Fallos de persistencia/publicación, IDs entre logs y versiones, fencing del cursor.
- Flujo contra Moto (DynamoDB + SQS): caída después de guardar, replay, lease competidor,
  recuperación de outbox sin stream y confirmación del cursor.
- Plantillas regenerables, límites de tamaño de la API, cfn-lint, lint y formato Python.
- Polling de 15 s por defecto y configurable; rechazo de valores no positivos.
- Retry-After en segundos/fecha HTTP para 429 y 503, cooldown entre peticiones concurrentes
  del mismo origen, independencia de otros hosts, cancelación, cabeceras inválidas y backoff
  exponencial reiniciado tras éxito. Los tiempos se prueban con relojes simulados.

Smoke real acotado realizado durante la preparación, 19 de noviembre de 2026:

| Protocolo | Log | Entradas parseadas | DER total |
|---|---|---:|---:|
| Static CT | Google ParcelYard2026h2 | 32 | 32867 bytes |
| RFC6962 | Cloudflare Nimbus2026 | 32 | 41729 bytes |

Estas cifras solo prueban compatibilidad con muestras; no son promedios globales ni benchmarks
de red, deduplicación, capacidad sostenida o latencia. Las mediciones de parseo incluyeron una
lista de ejemplo pequeña. No se crearon recursos AWS ni se publicó ningún evento real.

La imagen ARM `ctwatch:local` se ha construido y comprobado localmente. Runtime verificado:
Python 3.12.16, Linux aarch64 y usuario 10001. El check offline valida checksum Unicode,
matching exacto/homógrafo, procesos reales con spawn y deduplicación. Se ejecutó sin red,
con raíz de solo lectura, capabilities eliminadas y `no-new-privileges`.

La primera ejecución detectó el disco virtual de Docker lleno: 16 GB, 0 disponibles. El
volumen `/tmp` tenía permisos 1777, pero no podía almacenar datos. No se borraron imágenes ni
volúmenes. La comprobación pasó utilizando un tmpfs local de 128 MB para `/tmp`. Este tmpfs es
solo un recurso de la prueba Docker; la tarea Fargate conserva el volumen efímero definido en
la infraestructura. Posteriormente se limpió la caché de builds y la imagen antigua sin
etiqueta autorizadas: quedaron aproximadamente 1,9 GiB disponibles (88 % de uso). La misma
comprobación pasó de nuevo con el volumen normal de disco, sin tmpfs. No se eliminaron
contenedores ni volúmenes de datos. CI incorpora `scripts/container_check.py` sobre la imagen
construida.

## Ingesta simultánea del registro activo

Prueba global del 19 de noviembre de 2026, con imagen ARM local limitada a 3 CPU y 3 GiB,
3 workers, 16 peticiones concurrentes y sondeo cada 5 segundos. Se usaron los dos dominios
del ejemplo más 10.000 dominios sintéticos `.invalid`. No se accedió a AWS.

Ventana de 120 segundos; 135,13 segundos incluyendo el drenaje de peticiones en curso.
Se sembró el cursor temporal con las últimas 256 entradas de cada log, y después se siguió
su crecimiento. Los cursores no se guardaron en DynamoDB ni se publicaron matches.

| Medida | Resultado |
|---|---:|
| Logs seleccionados | 80: 63 Static CT y 17 RFC6962 |
| Estados | 50 usable y 30 qualified |
| Heads firmados verificados | 80/80 |
| Logs con entradas descargadas y analizadas | 76/80 |
| Entradas procesadas, incluida semilla | 277.716 |
| Entradas nuevas procesadas | 258.260 |
| Caudal procesado sobre tiempo total | 2.055 entradas/s |
| Aciertos de caché de certificado | 176.626 (63,6 %) |
| Análisis nuevos por certificado | 101.090 |
| Descarga de cuerpos HTTP, comprimidos cuando aplica | 503,4 MB |
| Peticiones HTTP | 3.281 |
| Matches con la lista de prueba | 0 |

En una muestra de 8.261 observaciones nuevas, la edad desde el timestamp de aceptación CT al
fin del análisis tuvo p50 de 7,68 s, p95 de 160,68 s y p99 de 208,21 s. **No es la latencia
interna ni end-to-end de la solución AWS**: incluye publicación y retrasos de cada log, polling,
backlog, red y análisis; no incluye persistencia/publicación SQS. La muestra cada 32 entradas
de cada lote no es una muestra aleatoria uniforme del ecosistema.

Hubo errores en 16 logs, muchos transitorios. Se observaron 49 respuestas 429, 16 respuestas
404, 3 respuestas 400 y una 522; algunos 404 de bloques parciales se resuelven con fallback al
completo. Sectigo Tiger también devolvió respuestas con longitud de entradas no válida. Los
cuatro logs sin lectura satisfactoria fueron Geomys Tuscolo2026h2, Tuscolo2027h1, Tuscolo2027h2
y Tuscolo2028h1: tenían entradas pero recibimos 429. No se clasifican como «sin actividad».

Al finalizar había 58.354 entradas pendientes respecto al último head observado, incluyendo
esos cuatro logs. Los timestamps de muestreo difieren entre logs: no es un backlog global
sincronizado. Los errores no provocaron saltos de cursor. Los datos descargados incluyen
metadatos y posibles redescargas de rangos que fallaron antes de completarse.

**Conclusión de esta prueba:** el lector y el matching funcionan simultáneamente con miles de
dominios y ambas APIs, y la deduplicación ahorra análisis. No se ha demostrado todavía cobertura
completa sostenida ni capacidad máxima: hay que endurecer límites por operador/host, respetar
Retry-After y evaluar respuestas inconsistentes antes de una prueba larga o despliegue productivo.

Después de esta medición se ha cambiado el polling por defecto a 15 segundos y se ha añadido
cooldown por origen con Retry-After y serialización de peticiones al mismo servidor. La medición
anterior se conserva como baseline de la versión con polling de 5 segundos, no como benchmark
de esos cambios. El límite agregado entre distintos hosts de un operador y las respuestas
inconsistentes de ciertos logs siguen siendo puntos a evaluar en una prueba larga.

Informe por log y métricas en [benchmarks/global-2026-11-19.json](benchmarks/global-2026-11-19.json).
El informe conserva el caudal real, no lo extrapola a una tarea de 8 vCPU en Fargate. Los
contadores RSS de hijos no se usan para dimensionar memoria; para eso hace falta muestreo del
cgroup durante una ejecución más larga.

## Segunda prueba: polling 15 s y Retry-After

Repetición del 19 de noviembre de 2026 con los mismos límites locales (3 CPU, 3 GiB, 3 workers,
10.002 dominios de prueba), ahora con polling de 15 s y la imagen con cooldown/Retry-After.
Ventana de 120 s y duración total de 142,41 s al terminar peticiones en curso.

| Medida | Baseline: 5 s | Segunda prueba: 15 s + Retry-After |
|---|---:|---:|
| Heads firmados verificados | 80/80 | 80/80 |
| Logs con entradas procesadas | 76/80 | 77/80 |
| Entradas procesadas, incluida semilla | 277.716 | 345.270 |
| Entradas nuevas procesadas | 258.260 | 325.558 |
| Caudal sobre tiempo total | 2.055/s | 2.425/s |
| Análisis evitados por caché | 63,60 % | 62,49 % |
| Peticiones HTTP | 3.281 | 2.576 |
| Respuestas 429 | 49 | 37 |
| Cuerpos HTTP descargados | 503,4 MB | 624,5 MB |
| Edad desde aceptación CT: p50 | 7,68 s | 17,76 s |
| Edad desde aceptación CT: p95 | 160,68 s | 138,98 s |
| Backlog respecto al último head observado | 58.354 | 80.148 |

Los tres logs sin lectura satisfactoria en esta segunda ventana fueron TrustAsia log2026a,
Geomys Tuscolo2026h2 y Geomys Tuscolo2027h1, por respuestas 429. Hubo errores en 15 logs en
total, incluyendo errores transitorios: además de 429 se observaron 16 respuestas 404,
2 respuestas 400, 2 respuestas 403 y timeouts. Hubo cero matches con la lista de prueba.

Las peticiones bajaron un 21,5 % y los 429 un 24,5 %, pero su proporción sobre peticiones
sigue próxima al 1,5 %: no se considera resuelto el throttling de los operadores. El crecimiento
observado fue distinto (315.590 frente a 404.938 entradas) y los periodos de drenaje también;
no se atribuye causalmente la variación de caudal o percentiles a los cambios. El p50 más alto
es compatible con un intervalo de polling mayor, sin demostrar una relación exacta.

No hay cobertura completa sostenida: persisten logs bloqueados y backlog. La edad CT incluye
el origen y no es la latencia del pipeline AWS. No se desplegó infraestructura ni se publicaron
eventos. Informe completo: [benchmarks/global-2026-11-19-poll15-retry.json](benchmarks/global-2026-11-19-poll15-retry.json).

## Catálogo Apple añadido al canal directo

El servicio, smoke, live probe y capturador del benchmark usan ahora la unión Chrome +
Apple por `log_id`. Los informes anteriores conservan su selección original de Chrome;
no se reinterpretan como pruebas de esta ampliación. Los 64 tests incluyen cinco nuevos
casos de unión sin lectores duplicados, prioridad de estados entre programas, selección
de endpoint, fallo parcial y conservación de snapshots frente a catálogos inválidos/vacíos.

Auditoría real del 19 de noviembre: Chrome 80 logs `usable`/`qualified`, Apple 44,
37 compartidos y **87 activos únicos**. Apple añade cuatro RFC6962 de Google
(Argon2026h2, Argon2027h1, Xenon2026h2 y Xenon2027h1) y tres Static CT de Cloudflare
(Raio2026h2a, Raio2027h1a y Raio2027h2a). Los siete heads firmados se verificaron.
Identidades, URLs, tamaños y raíces en
[benchmarks/apple-catalogs-2026-11-19.json](benchmarks/apple-catalogs-2026-11-19.json).

Smoke de ocho entradas recientes por protocolo exclusivo de Apple:

| Log | Protocolo | Entradas analizadas | DER total |
|---|---|---:|---:|
| Google Argon2027h1 | RFC6962 | 8 | 8525 bytes |
| Cloudflare Raio2027h1a | Static CT | 8 | 7757 bytes |

Sin matches con la lista de ejemplo. Esto valida descubrimiento, firma, descarga y parseo
en muestras, no cobertura sostenida ni caudal de los 87 logs. No se usó AWS.

## Paso CAA para matches exactos

Se añadieron 23 casos de prueba para selección exclusiva de `exact`, herencia CAA,
alias con y sin respuesta, NXDOMAIN/SERVFAIL, límites de tiempo, wildcard, alternativas
de emisión, emisor desconocido, propiedades críticas y parámetros no soportados.
La prueba de integración verifica que un fallo DNS no pierde el match ni evita guardar
el evento antes de confirmar el cursor. La identidad del evento no depende del resultado
DNS. Configuración e interpretación: [caa.md](caa.md).

Consulta DNS real a `letsencrypt.org`: respuesta CAA obtenida y decodificada, incluidos
`issue` e `issuewild`. Es una prueba de compatibilidad del resolver, no una comprobación
histórica de la emisión de un certificado. La comparación de emisor se valida con
certificados de prueba y mapeos explícitos. Dependencia `dnspython==2.8.0` fijada
e imagen ARM reconstruida.

## Lookup automático CCADB

Se añadió el CSV oficial List of CAA Identifiers V2: 3.763 registros, con atribución,
licencia y SHA-256. La actualización diaria conserva la última copia válida ante fallos;
una copia de más de siete días devuelve indeterminado cuando se necesita identificar
al emisor. Las excepciones manuales siguen teniendo prioridad. Se añadieron 17 casos de
prueba para coincidencia AKI/SKI + Subject, conflictos, campos inválidos, falta de AKI,
caducidad, checksum, actualización diaria, caché atómica, fallos y enriquecimiento.

Auditoría offline con 2.000 certificados reales distintos de la captura anterior,
seleccionados por orden de SHA-256: **2.000 correspondencias automáticas**, con emisores
de Let's Encrypt, Google, Amazon, DigiCert, Sectigo, GoDaddy y otros. No se infiere de
ello cobertura universal; no se consultó DNS ni se verificaron firmas/cadenas en esta
auditoría. Datos y distribución de emisores en
[benchmarks/ccadb-lookup-2026-11-19.json](benchmarks/ccadb-lookup-2026-11-19.json).

## Dos minutos con Chrome + Apple y CAA/CCADB

Prueba local del 19 de noviembre de 2026 a las 16:16:42 UTC (17:16:42 Madrid):
Docker ARM, 3 CPU/3 GiB, 3 workers, 16 peticiones concurrentes, polling de 15 s,
10.000 dominios sintéticos, dos de ejemplo y tres nombres reales añadidos como controles.
Ventana de 120 s, 132,785 s incluyendo drenaje. Ninguna llamada a AWS ni publicación.

| Medida | Resultado |
|---|---:|
| Logs activos únicos seleccionados | 87: 66 Static CT y 21 RFC6962 |
| Heads firmados verificados | 87/87 |
| Logs con entradas procesadas | 86/87 |
| Entradas totales, incluida semilla | 266.730 |
| Entradas posteriores al head inicial | 244.714 |
| Caudal sobre tiempo total | 2.008,73 entradas/s |
| Análisis evitados por caché | 158.840 (59,55 %) |
| Cuerpos HTTP descargados, incluidos preparación y catálogos | 499,39 MB |
| Respuestas HTTP 429 | 40 |
| Backlog respecto al último head observado por log | 46.764 entradas |

Se prepararon cuatro observaciones de certificados recientes de un log antes de iniciar
la ventana. Contenían tres certificados distintos y tres nombres DNS; generaron cuatro
eventos exactos (uno de los certificados produjo match wildcard y no wildcard). Los
controles se analizaron separadamente y se reiniciaron motor y caché DNS local antes
del cronómetro. La lista ampliada también se vigiló durante la ventana.

En la captura hubo **28 observaciones de match exacto, equivalentes a cuatro eventos
únicos por `event_id`**, sobre esos mismos certificados. Todos se aceptaron en CT antes
del inicio de la ventana y se encontraron a través de la semilla/repeticiones entre logs;
no se presentan como cuatro nuevas emisiones posteriores al inicio. CCADB identificó
automáticamente los emisores de los cuatro eventos y CAA devolvió `unrestricted` en
todos: no se encontró un RRset CAA efectivo. No hubo errores DNS ni emisores desconocidos
en esos matches. Esta ejecución no ejercitó una política restrictiva real; los casos
permitido/denegado siguen cubiertos por pruebas automatizadas.

Hubo errores en 16 logs, incluidos transitorios. Tuscolo2027h1 no pudo aportar entradas
por 429. También se observaron 404 de tiles, 400, una respuesta de longitud inválida de
Sectigo y timeout. No se ha demostrado cobertura completa sostenida. La edad CT muestreada
tuvo p50 25,118 s y p95 140,753 s: incluye publicación, polling y backlog; no mide la
latencia interna ni la entrega AWS. El ensayo no aísla el coste de CAA ni permite comparar
caudal causalmente con ventanas anteriores de distinto catálogo/actividad.

Informe: [benchmarks/global-2026-11-19-apple-caa.json](benchmarks/global-2026-11-19-apple-caa.json).

## Consumo real: cinco minutos con cgroup v2

El 19 de noviembre a las 16:28:16 UTC (17:28:16 Madrid) se repitió la ingesta con medición
agregada de CPU/memoria del contenedor y sus workers. Límites: 3 vCPU, 3 GiB, 3 workers,
16 peticiones concurrentes, polling 15 s, caché de 500.000 certificados, 10.005 dominios,
CAA y controles positivos. Cinco minutos de ingesta y 30,34 s de drenaje; 335,87 s con
preparación. La serie incluye 336 muestras; en la fase de ingesta el mayor intervalo fue
1,16 s. El pico de memoria del kernel captura también ráfagas entre muestras.

| Recurso real | Ventana activa ≈300,32 s | Ingesta + drenaje: 330,34 s |
|---|---:|---:|
| CPU total consumida | 100,68 s de CPU | 104,33 s de CPU |
| CPU media, núcleos equivalentes | 0,335 | 0,316 |
| CPU p95 de intervalos muestreados | 0,530 | 0,526 |
| CPU máxima de intervalos muestreados | 2,138 | 2,138 |
| Memoria agregada media ponderada | 429,75 MiB | 440,67 MiB |
| Memoria agregada máxima muestreada | 580,73 MiB | 580,73 MiB |
| Descarga por interfaces | 4,27 MB/s | 3,95 MB/s |

**Pico de memoria registrado por el kernel: 580,83 MiB** en toda la vida del contenedor.
CPU sin throttling de cuota: cero periodos y cero segundos limitados. Los promedios CPU
no se dividen entre los tres núcleos: 0,335 representa 33,5 % de un núcleo o 11,2 % de
la capacidad asignada. Los máximos/p95 son tasas entre muestras, no picos instantáneos.
La memoria incluye procesos, caché de páginas y memoria de kernel imputada al cgroup;
no se han sumado máximos RSS independientes.

Evolución agregada observada:

| Tiempo aproximado | Memoria actual del cgroup | Entradas de caché de certificado |
|---|---:|---:|
| 60 s | 332,1 MiB | 44.430 |
| 120 s | 436,9 MiB | 105.314 |
| 180 s | 470,3 MiB | 151.387 |
| 240 s | 547,3 MiB | 199.735 |
| 300 s | 580,6 MiB | 239.948 |

La caché acabó en 243.616/500.000 entradas después del drenaje. No se ha medido el máximo
con todas las cachés llenas. La huella observada tampoco corresponde a los seis workers
configurados inicialmente para producción. El porcentaje de CPU libre no demuestra
capacidad global completa: hubo errores/rate limits y backlog.

Se procesaron 709.412 entradas, 687.396 posteriores al head inicial; 65,66 % de análisis
evitados y 86/87 logs con datos. El backlog respecto al último head observado sumó 184.164
entradas. Tuscolo2027h1 no aportó entradas por 429. Hubo 97 respuestas 429, 34 respuestas
404, 10 respuestas 403, una cabecera inconsistente y respuestas de longitud inválida.
La descarga de interfaces fue 1,304 GB y el envío 7,46 MB durante ingesta + drenaje.
Los cuatro eventos únicos de los controles recibieron CAA `unrestricted`; solo once
observaciones de match en vivo, por lo que no se demuestra capacidad de CAA a gran escala.
No se ejecutaron escrituras/publicación AWS; su coste de proceso no está incluido.

Informe y serie: [benchmarks/resources-2026-11-19-5min.json](benchmarks/resources-2026-11-19-5min.json).
El contenedor temporal terminó correctamente y se eliminó. Se añadieron dos tests para
verificar deltas de contadores y promedios ponderados con intervalos irregulares.

## Ensayo limitado a 1 vCPU y 2 GiB: 20 minutos

Inicio: 19 de noviembre de 2026, 16:54:20 UTC. Docker ARM64, imagen
`sha256:ce78b51ab2aa9319b83ac55b23e844176ff3df5cae47d6ff1ed9a18d3861554b`,
cuota efectiva `cpu.max=100000 100000`, 2.147.483.648 bytes de RAM, sin swap.
Tres workers, HTTP paralelo 16, polling 15 s, caché de 500.000 certificados,
10.004 dominios, CAA y controles positivos. Ventana de 1.200 s; 1.232,22 s con
drenaje. La instrumentación registra cgroup v2 aproximadamente cada segundo,
con CPU/memoria agregadas de todos los procesos, red, presión CPU/memoria/IO,
eventos OOM, caché y pendientes/antigüedad de head por log. No hubo escrituras AWS.

| Medida | Resultado |
|---|---:|
| Entradas procesadas, incluido drenaje | 3.816.105 |
| Caudal activo aproximado | 189.500 observaciones/min |
| CPU media / p95 de intervalos activos | 0,347 / 0,530 vCPU |
| Periodos CPU limitados | ≈3,34 % |
| Memoria media activa / pico kernel de toda la vida | ≈700 / 874,82 MiB |
| Caché: primer llenado | ≈695 s desde inicio de ingesta |
| Hits de motor, incluidos in-flight | 2.995.977 (78,51 %) |
| Fallos de caché / análisis iniciados | 820.128 |
| Heads verificados / logs con entradas | 87/87 / 86/87 |
| Pendientes respecto a últimos heads observados | 679.417 |
| Respuestas 429 | 280 |
| OOM / procesos terminados por OOM | 0 / 0 |

El tiempo de pared por lote tuvo p95 de 19,907 s en lectura, 0,194 s en análisis
incluyendo cola y 0,005 s en enriquecimiento. Estos tiempos se solapan entre logs;
lectura incluye esperas de origen, cooldown y red, y no mide solamente transferencia.
La edad CT tuvo p50 31,968 s, p95 294,279 s y p99 818,072 s; incluye publicación del
log y pendientes, no entrega AWS. Los controles produjeron dos eventos únicos
`unrestricted`; no se ha sometido CAA a una alta tasa de matches.

La memoria siguió creciendo después del llenado: durante los últimos cinco minutos
pasó de un mínimo de ≈817 MiB a ≈865 MiB al final de la ventana. La caché de nombres
por worker, los asignadores y la propia instrumentación también consumen memoria.
Por tanto, 2 GiB proporciona margen en esta prueba; 20 minutos no prueban una meseta
de memoria ni cubren el TTL de una hora. La baja CPU no prueba cobertura sostenida:
hubo errores por origen y pendientes importantes, incluidos heads antiguos.

Los 820.128 fallos **no son certificados globalmente únicos**: tras expulsiones LRU,
una misma huella puede requerir otro análisis. Esta ejecución no conservó todas las
huellas, y no permite calcular esa fracción retrospectivamente. Se mantiene como
referencia de recursos sin traza por certificado; la auditoría adicional separa ese
efecto de la sobrecarga de registrar todas las decisiones.

Datos completos comprimidos:
[resources-2026-11-19-1cpu-2g-20min.json.gz](benchmarks/resources-2026-11-19-1cpu-2g-20min.json.gz).
Informe reproducible: `scripts/report_resource_trial.py`, con gráficas de CPU,
memoria, ocupación, caudal, pendientes, red y throttling.

## Auditoría exacta de deduplicación y tamaños de caché

Segunda captura: 17:15:52 UTC, 20 minutos más drenaje (1.231,86 s), mismos límites
1 vCPU/2 GiB, tres workers, 10.004 dominios, polling 15 s y caché 500k. Se registró
cada lookup en una traza binaria de 42 bytes: tiempo, resultado real (análisis,
hit de caché o in-flight), indicador de caché llena y SHA-256 del DER. La referencia
exacta mantiene todas las huellas fuera del contenedor medido, sin LRU ni expiración.
Los contadores de la traza cuadran exactamente con los del motor: 3.697.918
observaciones, 897.484 análisis y 2.800.434 hits, de los que 73.665 fueron in-flight.

Hubo **839.437 huellas DER distintas** y **58.047 reanálisis**. Antes de observar la
caché llena (≈619,34 s desde el primer lookup), hubo 500.384 análisis nuevos y cero
reanálisis; los trabajos en vuelo explican que el número supere ligeramente 500k.
Después hubo 397.100 análisis, incluidos 58.047 reanálisis: 14,62 % de los análisis
de esa fase, o 3,66 % de sus 1.587.522 observaciones duplicadas. Son denominadores
distintos. Los 58.047 certificados reanalizados lo fueron una vez adicional cada uno.
La evolución por minuto se conserva; no se presupone un régimen estacionario.
En el último bloque completo de la ventana activa, los reanálisis fueron el 28,72 %
de los análisis del bloque. La media posterior al llenado oculta ese crecimiento.
Los bloques exclusivos de drenaje se excluyen del gráfico temporal de porcentajes,
pero permanecen en los totales y en los datos originales.

Distribución de hits de caché por huella en toda la captura (excluye in-flight):

| Hits | Certificados | Porcentaje |
|---|---:|---:|
| 0 | 235.166 | 28,01 % |
| 1 | 184.915 | 22,03 % |
| 2 | 105.543 | 12,57 % |
| 3–4 | 102.801 | 12,25 % |
| 5–9 | 112.600 | 13,41 % |
| 10–19 | 98.412 | 11,72 % |

Mediana 1 hit; p90 10, p95 12, p99 13 y máximo 19. El 1 % de huellas con más hits
concentra el 4,46 % de los hits. Se incluyen entradas con cero hits; el histograma
exacto, la distribución de in-flight y la de reanálisis también se conservan.
El comienzo/final de la ventana censura repeticiones anteriores y posteriores.

Simulación sobre la misma secuencia, LRU e inserción inmediata, TTL fijo de 1 h:

| Capacidad | Hit rate simulado | Análisis | Reanálisis |
|---|---:|---:|---:|
| 100k | 61,80 % | 1.412.513 | 573.076 |
| 200k | 71,25 % | 1.063.103 | 223.666 |
| 500k | 75,73 % | 897.625 | 58.188 |
| 1M | 77,30 % | 839.437 | 0 |

El motor real inserta al completar trabajos, por lo que el orden puede diferir del
simulado; con 500k el modelo predijo 141 análisis más que los observados (≈0,016 %).
Doblar 500k a 1M reduce ≈6,5 % los análisis simulados en esta ventana; no significa
ese porcentaje de CPU o coste. 1M no se llena con 839.437 huellas: cero reanálisis
no es una garantía para una hora o un día. 500k sigue siendo un punto de partida
razonable; la curva no demuestra todavía un tamaño óptimo de producción.

La caché del motor memoriza resultados y no suprime eventos. En la muestra de matches
hubo diez observaciones de evento antes del llenado, siete con `event_id` ya visto;
no hubo matches después. La escritura condicional de DynamoDB evita crear otro
registro para el mismo evento; SQS sigue requiriendo consumidor idempotente ante
reentregas. Esta prueba no ejercitó DynamoDB ni SQS reales.

La traza añade consumo: pico de 1.061,30 MiB, con hasta 147,05 MiB de caché de archivos
muestreada y CPU activa media ≈0,349 vCPU. No se atribuye la diferencia con la primera
ventana causalmente a una sola variable; cambian también actividad y restricciones
externas. Se usa la primera ejecución sin traza como referencia de recursos.

Datos: [auditoría y simulación](benchmarks/dedup-audit-2026-11-19-1cpu-2g-20min.json)
y [serie completa de recursos auditados](benchmarks/resources-2026-11-19-1cpu-2g-20min-audited.json.gz).
La traza local queda en `.deploy/dedup-audit-1cpu-2g-2026-11-19/resources.dedup.bin`;
su SHA-256 está en el informe agregado. Se han conservado también logs y evidencias
Docker. Scripts: `dedup_audit.py`, `analyze_dedup_audit.py`, `report_dedup_audit.py`.

## Prueba en la cuenta destino

Se realizaron además experimentos locales controlados de deduplicación y una comparación
en vivo con WebSocket Lite. Métodos, resultados y límites en
[benchmarks/README.md](benchmarks/README.md). La comparación distingue certificados
compartidos de exclusivos y no asigna latencia a los no observados.

1. Verificar cuenta, región, cuota Fargate y política IAM de despliegue.
2. Preparar infraestructura con DesiredCount=0 y revisar el change set.
3. Subir una lista propia y construir la imagen de la arquitectura elegida.
4. Arrancar con DesiredCount=1, verificar inicialización de cursores y errores de cada fuente.
5. Emitir un certificado de prueba sobre un dominio controlado incluido en la vigilancia.
6. Comprobar match, evidencia, event_id y tiempos en SQS. El timestamp de aceptación no es el de
   publicación pública; no atribuir todo ese retraso al servicio.
7. Reiniciar tarea y comprobar recuperación desde cursores; forzar fallo del publicador y
   comprobar pendientes/reintentos antes de permitir caducidad de mensajes.
8. Cambiar lista y comprobar nueva versión; medir durante 24–72 horas antes de dimensionar.
9. Revisar CPU/RAM, cache hit rate, backlog por log, costes auxiliares y límites del operador.

No se ha desplegado ni validado todavía la integración real de IAM, red, ECS, Streams y Lambda
en esta cuenta. Moto y cfn-lint reducen errores; no equivalen a esa prueba.
