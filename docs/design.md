# Diseño y decisiones

## Recuperación

Cada log tiene un registro `LOG#<sha256-clave-publica>`. Un lease de 120 segundos impide que dos
owners avancen simultáneamente. Su renovación y el commit usan escrituras condicionales. El
commit exige owner, lease vigente y cursor esperado. El proceso conserva rangos contiguos;
no confirma un rango posterior saltando un fallo previo. No se usan timestamps como cursores.

Para cada rango: descargar → analizar → guardar todos los matches con identidad estable →
confirmar cursor. Guardar varios matches no es una transacción atómica de todo el rango:
si falla a mitad, se repite. Cada inserción condicional tolera los ya guardados. Si falla el
commit, los eventos persisten y el rango se vuelve a procesar de forma idempotente.

La caché devuelve resultados reutilizables; no dice simplemente «descartar certificado». Así
un resultado analizado pero aún no persistido puede volver a guardarse tras un fallo de AWS.
Dos lectores que encuentran el mismo certificado mientras se analiza comparten un Future.
Un error no queda convertido en resultado negativo ni avanza el cursor.

## Outbox

`EVENT#<event_id>` es simultáneamente el registro de deduplicación y el evento pendiente. Un GSI
disperso indexa solo pendientes. La Lambda envía a SQS antes de marcar SENT. Un fallo entre esos
pasos puede duplicar entrega, pero no declara enviado algo que nunca se publicó. DynamoDB Streams
activa la vía rápida; una regla cada minuto consulta pendientes que pudieron sobrevivir a la
retención del stream. El consumidor SQS es responsable de su propia idempotencia y reintentos.

Los eventos pendientes no caducan. SENT caduca a los 90 días; no es un historial eterno. Los
IDs incluyen la versión de vigilancia: un replay deliberado con otras reglas puede generar
otro evento lógico. Se conserva la primera evidencia persistida, no todas las observaciones.

## Cobertura y confianza

Se verifica SHA256(clave DER)=log_id, URLs HTTPS, firma ECDSA/RSA SHA256 de STH y firmas de nota
Static CT con origen de submission separado de monitoring. Se rechazan retrocesos de tamaño y
cambios de raíz a tamaño idéntico respecto al estado guardado. Se valida framing, anchura de tile,
vectores y contigüidad. Los bloques parciales pueden desaparecer al publicarse el completo;
en ese caso se descarga el completo pero solo se procesa hasta el tamaño del checkpoint leído.

No se verifican pruebas Merkle de cada entrada, consistencia entre árboles ni gossip. Ese es
un alcance adicional si se necesita detectar operadores de log que mienten criptográficamente.
HTTPS más STH firmado no equivale a inclusión verificada. No confundir con validación de cadenas
de confianza TLS: este monitor inspecciona SAN de artefactos registrados, no decide su confianza.

## Por qué un servicio continuo

Todas las fuentes se reúnen en una tarea, con caché anterior al parseo y múltiples procesos
para evitar que el GIL limite el análisis a un núcleo. `ProcessPoolExecutor` tiene cola limitada
por un semáforo; la red tiene límite global de conexiones y cada log un único lector ordenado.
La descarga se frena si faltan slots de CPU. Las cachés están limitadas por número de entradas
y TTL; vigilar RSS es necesario porque cada resultado tiene tamaño variable.

MSK aportaría retención y reparto horizontal, pero no elimina la lógica de deduplicación de
negocio y cobra por transportar toda la entrada. Añadirlo se justifica con varios consumidores,
replay propio o escalado horizontal. SQS en esta versión contiene únicamente matches.

## Configuración de runtime

| Variable | Default | Función |
|---|---|---|
| `STATE_TABLE` | obligatoria | Tabla de cursores y outbox |
| `WATCHLIST_URI` | archivo de ejemplo | Archivo local o `s3://bucket/key` |
| `CONFUSABLES_PATH` | `data/confusables.txt` | Tabla vendorizada |
| `WORKERS` | 6 | Procesos de análisis |
| `CERT_CACHE_ENTRIES` | 500000 | Límite de resultados por certificado |
| `HTTP_CONCURRENCY` | 16 | Peticiones simultáneas globales |
| `POLL_SECONDS` | 15 | Pausa al alcanzar el head; entero positivo |
| `START_MODE` | `head` | Inicialización de logs nuevos: head o beginning |
| `LOG_LIST_URL` | registro Chrome | Registro de logs HTTPS |
| `APPLE_LOG_LIST_URL` | catálogo oficial Apple | Catálogo adicional HTTPS; vacío lo desactiva |

`beginning` puede descargar miles de millones de entradas: no lo expone el comando normal de
despliegue. Cambiarlo no rebobina cursores existentes. Un replay debe usar estado aislado y un
plan de coste/cobertura; no borrar cursores de producción sin preservar su punto de recuperación.

## Retry-After y concurrencia HTTP

HTTP 429/503 fija un cooldown por origen, compartido por checkpoints, tiles y rangos del mismo
servidor. `Retry-After` acepta delay-seconds o HTTP-date según RFC 9110 §10.2.3; las fechas
pasadas equivalen a cero. Se transforma el plazo a reloj monotónico al recibir la respuesta.
Sin cabecera válida, el backoff exponencial por origen empieza en 2 segundos y llega a
300 segundos, más hasta 1 segundo de jitter; un éxito reinicia ese contador. Un Retry-After
válido no se recorta a 300 segundos.

Las peticiones de un mismo origen se serializan. La espera no ocupa slots HTTP globales y
es cancelable al detener el servicio; otros hosts siguen operando. No hay reintentos internos
que oculten errores: el lector reintenta el rango sin avanzar cursor. El backoff del lector y
el cooldown se solapan; la siguiente petición solo espera el tiempo que falte. No se reanudan
peticiones anteriores al deadline aunque estuviesen en cola. Los cooldowns son transitorios
en memoria y no se comparten entre hosts de un mismo operador.

Referencia: https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after
