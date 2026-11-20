# Reproducir los experimentos

Ejecutar desde la raíz del repositorio. Requiere Docker con cgroup v2, salida a Internet,
espacio para imágenes y capturas, y Python con Docker SDK (`pip install docker==7.2.0`).
Las dependencias extra se limitan a herramientas de prueba; producción no usa WebSocket.
Los scripts leen CT público y escriben archivos locales, sin AWS.

El capturador directo usa ahora Chrome + Apple. Los informes guardados del experimento
original usaban solo Chrome en el directo: reconstruir la imagen y repetir producirá
una selección distinta, registrada en el campo `catalogs` del nuevo informe.

```bash
docker build --platform linux/arm64 -t ctwatch:local .
docker build -f scripts/benchmarks/Dockerfile -t ctwatch:bench .
docker network create ctwatch-bench-net
docker run -d --name ctwatch-bench-server --network ctwatch-bench-net \
  --cpus 1 --memory 1g --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  -e CERTSTREAM_CONFIG=/app/config.yaml \
  -v "$PWD/scripts/benchmarks/certstream.yaml:/app/config.yaml:ro" \
  ghcr.io/reloading01/certstream-server-rust@sha256:7cabc7aa3b652ead3a1ca29d9e2ff2f720eb943373c56f2fcda94dcf9fbb2da8
```

No se publican puertos del servidor en el host. Confirmar en sus logs que ha cargado la
configuración antes de medir. El orquestador arranca el servidor si está parado y espera
20 s en ese caso; si ya está arrancado, dejarlo calentar al menos 20 s.

```bash
.venv/bin/python scripts/benchmarks/run_live.py --output .deploy/ab-new
.venv/bin/python scripts/benchmarks/analyze_live.py .deploy/ab-new
.venv/bin/python scripts/benchmarks/outer_join.py .deploy/ab-new \
  --output .deploy/ab-new/outer-join.json
```

La ventana compartida dura 120 s más inicialización y drenaje. Se crean dos clientes
temporales: directo con 2 CPU/3 GiB y WebSocket con 0,5 CPU/1 GiB. El servidor se detiene
al terminar; los clientes se eliminan. Los informes conservan estado de salida y logs.
Elegir un directorio nuevo: el script rechaza capturas SQLite existentes. Los resultados
son públicos y el directorio se hace escribible para el UID del contenedor.

Replay offline de las primeras 100.000 observaciones guardadas, seis ensayos alternados:

```bash
docker run --rm --network none --cpus 2 --memory 3g --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  -v "$PWD/scripts/benchmarks:/bench:ro" \
  -v "$PWD/.deploy/ab-new:/results:rw" \
  ctwatch:bench python /bench/dedup_ab.py
```

El replay conserva la secuencia, añade 32 dominios reales como controles positivos y
comprueba hashes de salida idénticos. Mide CPU del cgroup y tiempo, sin llamadas externas.
`--limit` y `--workers` permiten cambiar muestra y procesos. Una captura menor dará una
muestra menor; revisar `entries` en los informes antes de comparar.

Limpieza de los recursos temporales propios, una vez terminadas las pruebas:

```bash
docker rm ctwatch-bench-server
docker network rm ctwatch-bench-net
```

Los JSON se pueden versionar; conservar SQLite solo localmente. Resultados e interpretación
de la ejecución realizada: [informe](../../docs/benchmarks/README.md).
