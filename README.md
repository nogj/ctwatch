# CTWatch

Monitor de Certificate Transparency en Python, preparado para AWS ECS Fargate. Lee los logs
públicos de los catálogos de Chrome y Apple, mantiene cursores por log, reutiliza el análisis de certificados
repetidos y emite eventos por coincidencia de dominio.

**Estado:** implementación inicial con pruebas locales y plantillas de infraestructura.
No hay ningún despliegue AWS creado por este repositorio. La capacidad y latencia globales
requieren un benchmark en la cuenta destino; los smoke tests no lo sustituyen.

## Arquitectura

```text
Catálogos Chrome + Apple → lectores HTTPS RFC6962 / Static CT
                        ↕ DynamoDB: cursores y leases por log
                 hash SHA-256 → caché exacta compartida + in-flight dedup
                      → pool de procesos: X.509 → SAN → caché de dominios → matching
                      → solo exact: política CAA DNS actual + comparación de emisor
                      → DynamoDB: eventos pendientes e idempotencia
                      → Lambda: DynamoDB Streams + recuperación cada minuto
                      → SQS Standard: eventos con event_id estable

S3 versionado → configuración de vigilancia, recargada cada 60 segundos
```

Una tarea activa en dos subredes disponibles, con IPv4 pública y **sin puertos de entrada**.
Salida HTTPS a Internet, sin NAT Gateway, balanceador, Kafka o caché distribuida.
ECS puede reemplazar una tarea; no hay continuidad activa en varias zonas. Se tolera una pausa
de recuperación de tarea y lease. Los cursores permiten recuperar desde el origen mientras este
siga sirviendo las entradas. Los despliegues detienen la tarea anterior antes de arrancar la nueva.

Valores iniciales: ARM64, 8 vCPU, 16 GiB, 6 procesos de análisis y sondeo cada 15 segundos.
CloudFormation deja **DesiredCount=0 por defecto**. Activar el lector requiere solicitar 1.
Aunque esté parado, pueden existir costes de almacenamiento, recuperación de outbox y servicios
auxiliares. Con entrada global continua se factura cómputo incluso sin coincidencias.

## Preparación local

Desde la raíz del repositorio, con Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/fetch_unicode.py --verify
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python infra/render.py --check
.venv/bin/cfn-lint infra/bootstrap.json infra/service.json
```

Las dependencias de ejecución están fijadas en `requirements.lock`. La imagen usa Python 3.12;
las herramientas de desarrollo tienen rangos en `pyproject.toml`. Los datos oficiales de
confusables Unicode 16.0.0 están incluidos con checksum y licencia: no se descargan al arrancar.

Lecturas de prueba **sin cuenta AWS**, acotadas a un máximo de 256 entradas de un log:

```bash
.venv/bin/python scripts/smoke.py --kind static
.venv/bin/python scripts/smoke.py --kind rfc6962
# Seleccionar un log por parte del nombre:
.venv/bin/python scripts/smoke.py --kind static --name willow --entries 128
```

Estos comandos acceden a fuentes CT públicas. Verifican framing, firma del checkpoint/STH y
parseo/matching, pero no miden todo el caudal ni la latencia de producción. La tasa de parseo de
una muestra diminuta, con cachés, no representa capacidad sostenida.

Para una prueba simultánea de todos los logs `usable` y `qualified`, `scripts/live_probe.py`
mantiene cursores solo en memoria durante una ventana acotada y escribe un informe local:

```bash
.venv/bin/python scripts/live_probe.py --seconds 120 --workers 3 \
  --synthetic-domains 10000 --output .deploy/live-probe/global-120s.json
# Incluir CAA y controles positivos con cuatro certificados CT recientes:
.venv/bin/python scripts/live_probe.py --seconds 120 --workers 3 --caa \
  --positive-controls 4 --output .deploy/live-probe/global-caa-120s.json
# Dentro de un contenedor Linux con cgroup v2: consumo agregado de todos los procesos.
python /checks/live_probe.py --seconds 300 --workers 3 --cache-entries 500000 \
  --caa --positive-controls 4 --measure-resources --output /results/resources.json
```

Con `--caa` se enriquece cada match exacto mediante DNS y el catálogo CCADB incluido.
Los controles añaden hasta 32 nombres reales a la lista, se analizan antes de iniciar
el cronómetro y se contabilizan por separado; el motor y la caché DNS se reinician
antes de la ventana medida. Los nombres añadidos también se vigilan durante esa ventana.
El informe separa observaciones de match de eventos únicos por `event_id`, sin publicarlos.
`--measure-resources` requiere Linux/cgroup v2 y guarda una serie aproximadamente cada
segundo de CPU, memoria agregada, red y tamaño de caché. Separa el ensayo completo de la
ingesta más drenaje. El pico de memoria del kernel corresponde a toda la vida del
contenedor y captura ráfagas entre muestras. `--cache-entries` vale 200.000 por defecto
en la prueba; se puede usar 500.000 para igualar el límite del servicio.

Se combinan por `log_id` los catálogos de Chrome y Apple: un solo lector y cursor por log,
aunque figure en ambos. Si cualquiera lo considera `usable` o `qualified`, se sigue como
activo; a igual prioridad se conserva la descripción y endpoint de Chrome. Los catálogos
se descargan independientemente cada hora. Si falla uno, se conserva su última copia en
memoria y se reintenta el refresco al minuto; el otro sigue disponible. Al arrancar sin
ninguna copia válida se reintenta sin iniciar lectores. Estas copias no sobreviven al reinicio;
los cursores sí se conservan en DynamoDB. Los catálogos se obtienen por HTTPS y se comprueba
que cada `log_id` corresponda al hash de su clave; no se verifican firmas de los catálogos.

`LOG_LIST_URL` y `APPLE_LOG_LIST_URL` permiten cambiar las URLs en el servicio; dejar
`APPLE_LOG_LIST_URL` vacío desactiva Apple. Las pruebas aceptan `--registry` y
`--apple-registry`, también con cadena vacía para desactivar Apple. Auditoría de solapamiento
y comprobación opcional de cabeceras firmadas, sin AWS:

```bash
.venv/bin/python scripts/registry_check.py --heads --output .deploy/catalogs.json
```

El polling se configura con `POLL_SECONDS` en el servicio, con `--poll-seconds` en la prueba
y con `plan --poll-seconds` al desplegar. El valor por defecto es 15 segundos; se admite
cualquier entero positivo. La pausa se aplica al alcanzar el head; el backlog se recupera
sin añadir esa pausa entre rangos.

Ante HTTP 429 o 503, se respeta `Retry-After` en segundos o fecha HTTP. El cooldown se comparte
entre todas las peticiones al mismo origen (esquema, host y puerto), sin bloquear otros hosts.
Si la cabecera falta o es inválida se usa backoff exponencial con jitter. El error se propaga
al lector, que conserva el cursor; la siguiente petición espera el plazo restante. Los logs
servidos desde hosts diferentes no comparten todavía un límite agregado por operador.

La lista sintética añade dominios `.invalid` a los dos del ejemplo para medir una vigilancia
de miles de nombres; no son dominios reales vigilados. Con `--synthetic-domains 0 --watchlist
config/watchlist.local.json` se usa únicamente nuestra lista. No hay escrituras AWS ni eventos
publicados. El informe distingue head verificado, lectura de entradas, crecimiento nuevo,
aciertos de caché, errores HTTP y edad respecto al timestamp de aceptación CT. Esta última
incluye el retraso del operador: **no equivale a latencia interna del pipeline**. La ventana
puede prolongarse hasta unos 30 segundos para terminar peticiones en curso y cerrar workers.

Para validar una tarea pequeña, se puede repetir la prueba durante 20 minutos con límites
reales de CPU y memoria, sin swap. Conserva los 3 workers para comparar con la medición
anterior; no configura ni despliega recursos AWS:

```bash
mkdir -p .deploy/resource-trial
chmod 777 .deploy/resource-trial
docker run --name ctwatch-resource-trial --cpus 1 --memory 2g --memory-swap 2g \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  -v "$PWD/scripts:/checks:ro" -v "$PWD/.deploy/resource-trial:/results" \
  ctwatch:local python /checks/live_probe.py --seconds 1200 --workers 3 \
  --cache-entries 500000 --caa --positive-controls 4 --measure-resources \
  --output /results/resources.json
```

La instrumentación conserva además un diario `resources.resources.jsonl` cada segundo,
incluso si el proceso termina antes de generar el informe final. Incluye CPU limitada por
cuota, eventos OOM, presión CPU/memoria/IO, memoria, red, caché, llamadas de análisis activas,
certificados en vuelo, errores HTTP y pendientes con la antigüedad del último head por log.
El informe final añade percentiles de tiempo por lote para lectura, análisis (incluida cola)
y enriquecimiento. Los tiempos de lotes concurrentes se solapan y no deben sumarse.
Después de las primeras expulsiones LRU, los fallos de caché no equivalen a certificados
globalmente distintos. Una prueba de 20 minutos no cubre el TTL de una hora.

Añadiendo `--audit-dedup` se conserva una traza binaria de cada lookup: tiempo monotónico,
decisión real (analizar, caché o in-flight), indicador de caché llena y SHA-256 del DER.
La traza añade I/O y caché de archivos al consumo del contenedor. La referencia exacta
sin expulsiones y las simulaciones de capacidad se calculan después, fuera del contenedor:

```bash
.venv/bin/python scripts/analyze_dedup_audit.py \
  .deploy/resource-trial/resources.dedup.bin \
  --capacities 100000 200000 500000 1000000 \
  --output .deploy/resource-trial/dedup-analysis.json
```

El resultado distingue reanálisis de identidades ya vistas antes/después del llenado,
distribuciones de hits por certificado (incluido cero), hits en vuelo y repeticiones.
También calcula una curva LRU ideal sobre la misma secuencia: inserción inmediata y
TTL fijo sin renovarlo en hits. El motor real inserta al terminar cada trabajo, por lo
que esa curva orienta, pero no sustituye una medición con cada tamaño. No convierte
automáticamente análisis evitados en CPU o coste ahorrado.

Con Docker arrancado:

```bash
docker build --platform linux/arm64 -t ctwatch:local .
docker run --rm ctwatch:local python -c "from ctwatch.matcher import Matcher; import json; m=Matcher(json.load(open('config/watchlist.example.json')), 'data/confusables.txt'); print(m.match('pаypal.com'))"
```

Comprobación offline del runtime y los procesos de análisis, con raíz de solo lectura:

```bash
docker run --rm --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges \
  -v "$PWD/scripts/container_check.py:/checks/container_check.py:ro" \
  ctwatch:local python /checks/container_check.py
```

Si el disco virtual de Docker está lleno, esta comprobación puede ejecutarse añadiendo
`--tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777` antes del nombre de imagen. Eso no libera
espacio del disco ni cambia el volumen efímero configurado para Fargate.

## Lista de vigilancia y semántica

Copiar `config/watchlist.example.json` a un archivo propio, por ejemplo
`config/watchlist.local.json` (ignorado por Git). No contiene secretos y solo es un ejemplo.

```json
{
  "schema_version": 1,
  "domains": [
    {
      "domain": "example.com",
      "include_subdomains": true,
      "variants": ["examp1e.com", "examplle.com"]
    }
  ]
}
```

- `exact`: igualdad canónica tras UTS46/IDNA. Unicode y su Punycode son el mismo nombre.
- `subdomain`: sufijo por etiquetas, solo si se habilita. No coincide `example.com.attacker.net`.
- `confusable`: nombre distinto con el mismo skeleton NFD/tabla confusables/NFD del **dominio
  completo**. No genera el producto cartesiano de sustituciones Unicode.
- `variant`: igualdad con una variante explícita. No se genera automáticamente un diccionario
  de typos, variantes de TLD ni búsqueda de marcas en cualquier etiqueta.
- `*.example.com` se analiza sobre `example.com`, conservando `wildcard=true`.
- Se inspeccionan SAN DNS; no se usa CN como sustituto si faltan SAN.
- Una similitud no implica intención maliciosa. Las reglas pueden producir varios tipos de match.

La versión de reglas deriva de la configuración, los datos Unicode, la versión de normalización
del runtime y la versión IDNA. La huella local puede diferir si se usa otro Python. Al cambiar la lista se
crea otro motor y se invalidan cachés; el trabajo en curso acaba con su versión original. La
actualización es prospectiva, no una consulta histórica ni un corte global exacto por timestamp.

Los matches `exact` incorporan la política CAA actual y su comparación con la CA emisora.
La asociación del emisor se resuelve automáticamente con el catálogo CAA de CCADB,
incluido en la imagen y actualizado diariamente. `caa_issuers` permite excepciones manuales.
Un emisor sin correspondencia produce `unknown` cuando hay restricciones, y nunca se descarta el
match por CAA. No se consulta para `subdomain`, `confusable` ni `variant`. Véanse
[configuración, estados y límites](docs/caa.md), incluida la diferencia entre política
actual y política en el momento de emisión.

## Despliegue en AWS

Requisitos: AWS CLI/perfil autenticado, Docker con buildx, cuota Fargate suficiente, dos zonas
disponibles y permisos CloudFormation/ECR/ECS/EC2/IAM/DynamoDB/S3/Lambda/SQS/EventBridge/Logs/
CloudWatch. No se escriben credenciales en archivos del repo ni se incluyen en la imagen.

Los comandos muestran cuenta y región antes de actuar. **bootstrap, build, upload-watchlist y
apply escriben en AWS.** `plan` crea un change set, pero no ejecuta sus recursos. No hay workflow
de despliegue automático desde GitHub.

1. Crear únicamente el registro ECR y el bucket de configuración:

```bash
.venv/bin/python scripts/deploy.py --profile MI_PERFIL --region eu-west-1 bootstrap
```

2. Validar y subir nuestra lista al bucket versionado:

```bash
.venv/bin/python scripts/deploy.py --profile MI_PERFIL --region eu-west-1 upload-watchlist config/watchlist.local.json
```

3. Construir y subir la imagen. El comando imprime su URI **con digest inmutable**:

```bash
.venv/bin/python scripts/deploy.py --profile MI_PERFIL --region eu-west-1 build --architecture ARM64
```

4. Preparar el cambio de infraestructura. Sustituir la URI del ejemplo por la salida anterior.
   Con `--desired-count 0` se prepara todo sin arrancar el lector; con `1` empezará a consumir CT:

```bash
.venv/bin/python scripts/deploy.py --profile MI_PERFIL --region eu-west-1 plan \
  --image 'CUENTA.dkr.ecr.eu-west-1.amazonaws.com/REPO@sha256:DIGEST' \
  --desired-count 0 --poll-seconds 15
```

El change set y los parámetros quedan en `.deploy/`. Revisar IAM, red, retención, imagen,
arquitectura, tamaño y número de tareas. El script valida combinaciones CPU/memoria. No aplicar
una imagen ARM con arquitectura X86_64 ni viceversa.

5. Ejecutar **ese** change set:

```bash
.venv/bin/python scripts/deploy.py --profile MI_PERFIL --region eu-west-1 apply \
  --change-set 'ARN_DEL_CHANGE_SET'
.venv/bin/python scripts/deploy.py --profile MI_PERFIL --region eu-west-1 status
```

6. Para empezar la prueba, repetir `plan` con la misma imagen y `--desired-count 1`, revisar y
   aplicar. El primer arranque de cada log guarda el tamaño actual y vigila desde ahí. No descarga
   todo el histórico. En posteriores arranques continúa con el cursor persistido.

Para parar el lector, preparar y aplicar otro change set con `--desired-count 0`. No parar solo
la tarea: ECS la reemplazaría. El script permite `--name OTRO_NOMBRE` antes del subcomando para
mantener instalaciones separadas. No ejecutar dos servicios distintos contra la misma tabla.

## Salida y garantías

SQS Standard entrega al menos una vez. Los consumidores deben ser idempotentes por `event_id`:

```json
{
  "schema_version": 1,
  "event_id": "sha256-de-la-identidad-del-match",
  "watched_domain": "paypal.com",
  "observed_domain_ascii": "xn--pypal-4ve.com",
  "observed_domain_unicode": "pаypal.com",
  "wildcard": false,
  "match_type": "confusable",
  "certificate_sha256": "...",
  "artifact_type": "precertificate",
  "ruleset_version": "...",
  "log_id": "...",
  "log_url": "https://.../",
  "entry_index": 1234,
  "submission_timestamp_ms": 1790000000000,
  "detected_at_ms": 1790000010000
}
```

La identidad incluye certificado, dominio vigilado, nombre observado, wildcard, tipo y versión de
reglas. Se conserva evidencia de la primera observación persistida. Precertificado y certificado
final son artefactos distintos. Los eventos enviados se recuerdan 90 días; reprocesar después
puede volver a emitirlos. Los pendientes no tienen TTL. SQS retiene mensajes durante 14 días.

## Operación, costes y límites

Consultar los outputs para la cola, tabla, clúster y grupo de logs. Se registran `heartbeat`,
`log_progress` (cursor/backlog), `registry_refreshed`, `ruleset_reloaded` y errores por log. Los
heartbeats incluyen aciertos/fallos acumulados y tamaño de caché. Los logs retienen 14 días.
Hay alarmas CloudWatch de errores de lector y mensajes SQS antiguos; **sin destinatarios de
notificación configurados**. Configurarlos en la prueba de cuenta si se requiere aviso externo.

Referencia de cómputo Irlanda: 8 vCPU/16 GiB ARM ≈0,316 USD/h; IPv4 ≈0,005 USD/h. DynamoDB,
SQS, Lambda, S3, métricas/logs y tráfico se facturan aparte. Evitar logs de cada certificado y
NAT Gateway: el caudal global puede ser de terabytes diarios. No hay archivo bruto en S3.
Los cursores se actualizan por bloque/rango; no hay una escritura por cada entrada sin match.

Antes de producción medir 24–72 horas: caudal por log, descarga real comprimida, CPU/RAM,
deduplicación observada, backlog, fallos de parseo, coste DynamoDB de checkpoints y latencia
de aceptación CT a publicación SQS. La disponibilidad pública depende del log; no se infiere
directamente de su timestamp de aceptación.

Límites explícitos:

- La caché es una optimización exacta y acotada: su pérdida repite trabajo. No hay deduplicación
  global permanente de toda la entrada ni escalado horizontal transparente.
- Un error de framing, certificado o firma detiene el avance de **ese log** y reintenta; nunca
  se salta silenciosamente una entrada que no ha podido procesar. Otros logs siguen avanzando.
- Se verifica la firma de STH/checkpoint y la identidad de su clave. **No implementa auditoría
  Merkle completa de inclusión/consistencia ni gossip**. Confía en las respuestas HTTPS del
  operador para las entradas. No es un auditor CT criptográfico.
- Los catálogos de Chrome y Apple son la cobertura elegida, no todos los logs privados/experimentales.
  Logs nuevos retirados/rechazados no se recorren; los ya conocidos siguen en seguimiento.
  Los readonly se dan por terminados al alcanzar su tamaño observado.
- Las interrupciones pueden elevar la latencia. No hay SLA medido ni deduplicación normalizada
  entre precertificado y certificado final.

Para eliminar infraestructura: detener primero la ingesta y consumir los pendientes. Después,
eliminar el stack de servicio y el bootstrap mediante CloudFormation. DynamoDB, SQS, bucket y
ECR tienen `DeletionPolicy: Retain`: **no desaparecen ni dejan de facturar almacenamiento al
eliminar el stack**. No se reutilizan automáticamente al crear otro stack; conservar outputs y
planificar su recuperación/importación antes de borrar una instalación con estado valioso.

Más detalles en [docs/design.md](docs/design.md), [docs/validation.md](docs/validation.md)
y [experimentos de deduplicación y WebSocket](docs/benchmarks/README.md).
