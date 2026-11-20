# Comprobación CAA después del match

El servicio consulta CAA únicamente para eventos `match_type=exact`, incluido el caso
wildcard cuyo nombre base coincide exactamente. `subdomain`, `variant` y `confusable`
no provocan consultas ni llevan el campo `caa`. Se consulta `observed_domain_ascii`;
en un exact coincide con el dominio vigilado.

La comprobación añade información: no filtra ni descarta detecciones. Se ejecuta después
del matching y antes de persistir el evento y confirmar el cursor. Los fallos DNS producen
`unknown`, conservando el evento. No se reintentan después como un trabajo independiente;
la outbox conserva la primera observación persistida y sus reintentos no repiten la consulta.
Los eventos ya almacenados no se enriquecen retroactivamente.

## Comparar la CA emisora

El nombre del emisor X.509 no es su identificador CAA. La correspondencia se obtiene
automáticamente del informe **List of CAA Identifiers V2 de CCADB**, usando dos datos:
el Authority Key Identifier del certificado debe coincidir con el Subject Key Identifier
de la intermedia, y su issuer completo debe coincidir con el Subject publicado.
No se infiere la CA por una palabra del CN, por el nombre comercial ni por el operador CT.

La imagen incluye una copia de 3.763 registros del 19 de noviembre de 2026. Se carga sin
red al arrancar, eligiendo la copia válida más reciente entre la incluida y la caché de
`/tmp/ctwatch-ccadb-caa.json`. El servicio comprueba cada hora si han pasado 24 horas desde
la descarga y actualiza el catálogo en una tarea separada de los lectores. No hay una
llamada a CCADB por match. Un fallo conserva la última copia válida y permite reintentar
al cabo de una hora. La actualización valida esquema/checksum y escribe la caché con
reemplazo atómico; los heartbeats registran antigüedad, hash y último error.

Si la copia tiene más de siete días, no se utiliza para autorizar ni excluir un emisor.
La política restrictiva devuelve `unknown`, salvo que exista una excepción manual.
Una copia inválida se ignora; sin ninguna válida, el servicio sigue detectando matches
y consulta DNS, pero la identificación automática queda indeterminada hasta recuperarla.
La caché en `/tmp` sobrevive a reinicios del proceso dentro de la misma tarea, pero no
garantiza persistencia entre tareas Fargate; la copia incluida es el fallback inicial.

Registros contradictorios para el mismo identificador/nombre, ausencia de AKI,
identificadores sin declarar o valores inválidos como `N/A` producen `unknown` con motivo
específico. Las diferencias de representación del Subject que no se puedan comparar
conservadoramente también quedan indeterminadas.

Las excepciones manuales siguen disponibles mediante `caa_issuers`, con prioridad sobre
CCADB. El mapa puede permanecer vacío para el funcionamiento normal. Ejemplo **ficticio**
de excepción por nombre RFC4514 exacto:

```json
{
  "schema_version": 1,
  "caa_issuers": {
    "CN=Example Issuing CA,O=Example CA,C=ES": ["ca.example"]
  },
  "domains": [{"domain": "example.com", "include_subdomains": true}]
}
```

Las excepciones forman parte de la versión de reglas y se recargan con la lista. Las
actualizaciones automáticas del catálogo no cambian esa versión: el evento conserva
`issuer_catalog_sha256` y `issuer_catalog_fetched_at_ms` como evidencia de la copia usada.
No cambian su `event_id` ni provocan una reevaluación de eventos ya persistidos.

Estados del campo `caa.status`:

| Estado | Significado |
|---|---|
| `authorized` | Una identidad del emisor obtenida de CCADB o de una excepción figura en una autorización aplicable sin parámetros adicionales |
| `unauthorized` | La política excluye las identidades identificadas, o prohíbe emitir a cualquier CA |
| `unrestricted` | No hay CAA relevante o no hay una propiedad que restrinja esta clase de emisión |
| `unknown` | Error DNS, emisor sin correspondencia, propiedad crítica no soportada, sintaxis no interpretable o parámetros pendientes de evaluar |

Se devuelve además `reason`, registros con flags/tag/value, nombre de la política
efectiva, TTL del RRset cuando existe, instante de observación e identidades del emisor.
No se convierte un timeout/SERVFAIL en ausencia de CAA. `unrestricted` no equivale a
certificado legítimo y `unauthorized` no demuestra por sí solo una emisión indebida.

## Resolución y límites

El resolver asíncrono sigue CNAME/DNAME mediante dnspython. Si no encuentra CAA, sube por
los padres del nombre original hasta antes de la raíz DNS. Para wildcard, `issuewild`
prevalece sobre `issue`; si no existe, se aplica `issue`. Las autorizaciones alternativas
se combinan como alternativas, no como restricciones acumulativas. Un parámetro como
`accounturi` o `validationmethods` no se valida con el certificado disponible y genera
`unknown` cuando la única autorización candidata depende de él. Una propiedad crítica
desconocida también queda indeterminada, pues no sabemos si la CA la entiende.

Se usan los servidores DNS del sistema y una caché LRU de 10.000 respuestas que respeta
la expiración DNS positiva/negativa de dnspython. Las consultas por dominio se comparten
entre los eventos del mismo lote. Hay como máximo 16 resoluciones simultáneas por tarea;
el límite de tres segundos por dominio incluye la espera de ese cupo y toda la búsqueda
por padres. No es una llamada DNS por cada entrada CT. Un dominio puede producir `unknown`
por agotar el tiempo esperando cupo durante una ráfaga de matches.

La infraestructura ECS usa el resolver de la VPC; un resolver personalizado debe ser
alcanzable con sus propias reglas de red. No se añaden resolvers públicos ni salida DNS
abierta a Internet. La implementación no valida DNSSEC y lo indica mediante
`dnssec_validated=false`. Tampoco valida la firma del certificado ni la cadena X.509:
el AKI y el nombre del emisor se leen del certificado, no prueban criptográficamente
quién lo firmó. Se indica `issuer_signature_verified=false` y `chain_validated=false`.
La fuente de la asociación es `ccadb_aki_ski_subject` para lookup automático o
`configured_issuer_dn` para una excepción. CCADB se obtiene por HTTPS; su checksum
permite identificar/verificar la copia guardada, pero no es una firma de CCADB.

**Se observa la política DNS actual, no la que existía al emitir el certificado.** El
resultado es una señal para investigar. No es una auditoría de cumplimiento histórico
de la CA ni una verificación completa de todas las SAN del certificado.

Semántica de referencia: [RFC 8659](https://www.rfc-editor.org/rfc/rfc8659.html).
Resolver: [dnspython asíncrono](https://dnspython.readthedocs.io/en/stable/async-resolver-class.html).

Catálogo y licencia: [CCADB](https://www.ccadb.org/resources),
[atribución y licencia incluidas](../data/CCADB-NOTICE.md).
Actualizar la copia distribuida y auditar una captura local, sin AWS:

```bash
PYTHONPATH=src .venv/bin/python scripts/fetch_caa_catalog.py
PYTHONPATH=src .venv/bin/python scripts/check_caa_catalog.py .deploy/ab-live2/direct.sqlite \
  --limit 2000 --output .deploy/ccadb-lookup.json
```
