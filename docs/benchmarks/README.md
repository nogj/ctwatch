# Experimentos de deduplicación y WebSocket

Mediciones locales del 19 de noviembre de 2026, Linux ARM64 en Docker Desktop.
No se desplegaron recursos AWS ni se publicaron eventos. Scripts y reproducción:
[scripts/benchmarks](../../scripts/benchmarks/README.md).

## Deduplicación: comparación controlada

Dos secuencias reales de 100.000 observaciones CT, en su orden original, se reprodujeron
con y sin caché de certificado. Tres ejecuciones por variante, alternando el orden, con
procesos nuevos y cachés inicialmente vacías. Mismo motor de producción, dos workers,
misma caché de nombres habilitada en ambas variantes y 10.034 dominios vigilados:
10.002 de la lista sintética y 32 controles positivos extraídos de cada captura.
Se midió CPU del cgroup, incluyendo padre y workers, después de arrancar los procesos.

| Muestra | Análisis evitados | CPU sin dedup, mediana | CPU con dedup, mediana | Ahorro CPU |
|---|---:|---:|---:|---:|
| Menos repetición | 43,995 % | 4,880 s | 3,986 s | 18,3 % |
| Más repetición | 70,801 % | 3,772 s | 2,323 s | 38,4 % |

Los resultados completos fueron idénticos por hash en las seis ejecuciones de cada
muestra, incluidos 131 y 76 matches respectivamente. La caché reutiliza análisis;
no elimina observaciones ni sustituye la idempotencia de los eventos de salida.

El coste del hash, consultas de caché y gestión de resultados está incluido en la variante
con dedup. Por tanto, **compensa en ambas muestras incluso contabilizando su coste**.
El porcentaje de duplicados no equivale al porcentaje de CPU ahorrada: siguen existiendo
trabajo por observación y cachés de nombres en ambas variantes. No se midieron red,
DynamoDB ni SQS; no se demuestra una reducción equivalente de la factura Fargate.
Los lotes se procesaron de uno en uno, sin buscar throughput máximo. El pico de memoria
del cgroup es acumulativo entre ensayos y no permite comparar memoria por variante.

La primera secuencia procede del prefijo válido de una captura cuyo informe en vivo no
llegó a completarse; solo se usa para replay offline. La segunda procede de la captura
completa descrita abajo. No se introdujeron duplicados artificiales.

Datos: [menos repetición](dedup-ab-low-repeat-2026-11-19.json) y
[más repetición](dedup-ab-high-repeat-2026-11-19.json).

## WebSocket: comparación de soluciones durante dos minutos

Se ejecutaron simultáneamente el lector HTTPS Python y un cliente Python WebSocket Lite
con la misma lista de 10.002 dominios. El WebSocket provenía de un agregador Rust local,
fijado por digest, cuyo coste también se midió. Se negoció `permessage-deflate`.
Los lectores directos empezaron en el head actual, sin semilla histórica, con polling de
15 s. El agregador usó también 15 s y se calentó antes de empezar la ventana.
Ambos clientes terminaron con exit code 0 y sin OOM. Ningún match con la lista sintética.

| Medida | HTTPS directo | Cliente WebSocket | Agregador WebSocket |
|---|---:|---:|---:|
| Observaciones procesadas | 291.418 | 84.240, ya deduplicadas | No comparable |
| Certificados únicos observados | 85.597 | 84.240 | — |
| CPU consumida | 34,91 s | 10,08 s | 14,62 s |
| Recepción por interfaz de red | 500,61 MB | 25,18 MB | 495,71 MB |
| Memoria máxima | 275,3 MiB | 87,2 MiB | 65,0 MiB, máximo muestreado |
| Tiempo incluido en recursos | 140,16 s con drenaje | 120,07 s | 120,00 s |

El cliente WebSocket recibió unos 20 veces menos bytes, pero el agregador siguió
descargando unos 496 MB desde CT. La suma de CPU de agregador y cliente fue 24,70 s.
La suma de recepción fue 520,89 MB, que incluye el salto interno Docker; **no es una
medida de tráfico externo facturable**. Un proveedor externo desplazaría ese trabajo
fuera de nuestra cuenta, con su propio precio y garantías.

Hubo 69.352 certificados compartidos. Entre ellos, WebSocket llegó antes en el 78,3 %;
la mediana de llegada WebSocket menos directo fue −5,52 s, y el p95 +17,04 s.
Es una diferencia de llegada condicionada a haber visto el certificado por ambas vías,
no una latencia end-to-end ni un SLA.

Se observaron 16.245 certificados solo por directo y 14.888 solo por WebSocket durante
la captura. Esto impide declarar equivalencia de cobertura; tampoco demuestra pérdida
permanente de esos certificados. El desglose posterior del full outer join se recoge abajo.

### Auditoría posterior del full outer join

Clave: SHA-256 del DER, una fila por identidad; no por observación ni dominio.
Precertificado y certificado final siguen siendo distintos. El directo recibió 291.418
observaciones, de las cuales 205.821 repiten identidad. El WebSocket recibió 84.240,
todas distintas porque su agregador ya deduplicaba.

| Cohorte | Ambos | Solo directo | Solo WebSocket | Full outer | Total directo | Total WebSocket |
|---|---:|---:|---:|---:|---:|---:|
| Toda la captura, incluido drenaje | 69.352 | 16.245 | 14.888 | 100.485 | 85.597 | 84.240 |
| Misma hora límite de recepción | 68.634 | 15.228 | 15.606 | 99.468 | 83.862 | 84.240 |
| Aceptación en minuto central, cohorte común | 30.377 | 1.958 | 2.068 | 34.403 | 32.335 | 32.445 |

Con toda la captura WebSocket tiene 1.357 identidades menos (1,59 %), pero con el mismo
límite de recepción tiene 378 más (0,45 %). El directo vio por primera vez 1.735 identidades
después del límite, 1.017 de ellas exclusivas. El total neto no mide las discrepancias:
en toda la captura hay 31.133 identidades exclusivas de una vía u otra, el 30,98 % de la unión.

En el minuto central, el 88,30 % de la unión es compartido; 5,69 % es solo directo y
6,01 % solo WebSocket. Esta cohorte usa un único timestamp de referencia por identidad:
el mínimo observado entre ambos canales. **Corrige el filtro exploratorio anterior**,
que filtraba cada lado por separado y daba 2.187/2.787 exclusivos. Con aquel método,
distintos timestamps de aceptación en distintos logs podían convertir artificialmente
un certificado compartido en exclusivo al filtrar.

De los exclusivos del directo, 11.719 tienen aceptación anterior al inicio; de los
exclusivos WebSocket, 7.861. En los últimos 30 segundos de aceptación hay 364 exclusivos
directos frente a 4.012 WebSocket. Estos patrones son compatibles con efectos de arranque,
backlog y retraso de lectura, sin permitir atribuir causalmente todas las diferencias.
Aceptación anterior al inicio no significa recepción anterior: CT puede publicar tarde.

De los exclusivos WebSocket, 4.164 fueron reportados primero desde Tuscolo2027h1 y 2.037
desde HETU2027. Juntos son el 41,65 % de ese grupo. El lector directo sufrió 429 y quedó
atrasado en ambos logs. Esto aporta evidencia de una limitación del lector directo en
esta ejecución; no permite atribuir todos los exclusivos al throttling. La fuente reportada
por WebSocket es la primera tras dedup, no una lista de todos los logs que contienen el DER.

Datos reproducibles: [auditoría JSON](websocket-outer-join-2026-11-19.json), generada con
`scripts/benchmarks/outer_join.py` sobre las capturas existentes, sin nuevo tráfico CT.

Limitaciones para interpretar los números:

- Se comparan soluciones completas: parser Rust y mensajes preprocesados frente a
  descarga y parseo Python. No se aísla causalmente el protocolo WebSocket.
- Catálogos distintos: directo selecciona Chrome; el agregador combina Chrome y Apple.
  Se verificaron 80 heads directos; hubo datos de 42 logs directos y 43 fuentes WebSocket.
  Un head saludable no prueba lectura completa de sus entradas.
- Comparten IP pública y pueden competir por límites de los operadores. Arranque,
  pacing y drenaje son distintos. Una ventana de dos minutos no prueba cobertura sostenida.
- WebSocket local sin TLS frente a HTTPS público. La CPU incluye inicialización y registro
  SQLite; directo además archiva DER de las primeras 100.000 observaciones. No son dos
  mediciones del mismo trabajo con instrumentación idéntica.
- La unión observada no es una referencia completa del ecosistema. No se midieron
  desconexión, replay durable ni integración de un proveedor externo.

**Decisión:** conservar la deduplicación exacta antes del análisis. WebSocket es prometedor
para aligerar el cliente y reducir retrasos, pero estos datos no justifican sustituir aún
los lectores con cursores. Antes de ello, igualar catálogo, medir 24–72 horas y probar
recuperación tras desconexión con un mecanismo de replay verificable.

Datos: [comparación](websocket-comparison-2026-11-19.json),
[directo](websocket-direct-capture-2026-11-19.json),
[cliente Lite](websocket-lite-capture-2026-11-19.json) y
[agregador](websocket-aggregator-2026-11-19.json).
Las capturas SQLite están en `.deploy/`, ignoradas por Git. Los contenedores y la red
temporal se eliminaron al terminar; las imágenes se conservan para reproducir.

Referencias de la alternativa: [documentación Certstream](https://certstream.dev/docs)
y [API alojada](https://certstream.dev/hosted-api). Se probó el servidor local, no un
servicio contratado ni su SLA.
