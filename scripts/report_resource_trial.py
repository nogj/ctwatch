"""Summarize a resource trial and produce a PDF with measured temporal charts."""
# ruff: noqa: E501

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from resource_probe import summarize


def number(value, digits=1):
    return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--total-pages", type=int, default=5)
    args = parser.parse_args()
    raw = args.source.read_bytes()
    data = json.loads(gzip.decompress(raw) if args.source.suffix == ".gz" else raw)
    resources = data["resources"]
    total = resources["ingestion_and_drain"]
    points = resources["series"][-total["samples"] :]
    zero = points[0]["monotonic_seconds"]
    active = [p for p in points if p["monotonic_seconds"] - zero <= data["configured_seconds"]]
    summary = summarize(active)
    elapsed = summary["elapsed_seconds"]
    last = active[-1]
    cache_full = next(
        (
            p["monotonic_seconds"] - zero
            for p in active
            if p["certificate_cache_entries"] >= data["certificate_cache_capacity"]
        ),
        None,
    )
    windows = []
    for minute in range(0, math.ceil(elapsed / 60)):
        left = min(active, key=lambda p: abs(p["monotonic_seconds"] - zero - minute * 60))
        right = min(active, key=lambda p: abs(p["monotonic_seconds"] - zero - (minute + 1) * 60))
        subset = [
            p
            for p in active
            if left["monotonic_seconds"] <= p["monotonic_seconds"] <= right["monotonic_seconds"]
        ]
        row = summarize(subset)
        seconds = row["elapsed_seconds"]
        row.update(
            {
                "minute": minute + 1,
                "entries_per_minute": (right["entries_processed"] - left["entries_processed"])
                * 60
                / seconds,
                "misses_per_minute": (
                    right["certificate_cache_misses"] - left["certificate_cache_misses"]
                )
                * 60
                / seconds,
                "cache_entries_end": right["certificate_cache_entries"],
                "backlog_end": right["backlog_last_observed_heads"],
            }
        )
        windows.append(row)
    analysis = {
        "active_summary": summary,
        "active_entries_processed": last["entries_processed"],
        "active_entries_per_minute": last["entries_processed"] * 60 / elapsed,
        "cache_first_full_seconds": cache_full,
        "memory_events_end": points[-1]["memory_events"],
        "minute_windows": windows,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "last_five_minutes": summarize(
            [p for p in active if p["monotonic_seconds"] >= last["monotonic_seconds"] - 300]
        ),
        "last_five_minutes_memory_min_bytes": min(
            p["memory_current_bytes"]
            for p in active
            if p["monotonic_seconds"] >= last["monotonic_seconds"] - 300
        ),
        "cpu_throttled_period_fraction": summary["cpu_throttled_periods"] / summary["cpu_periods"]
        if summary["cpu_periods"]
        else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
    fontdir = Path("/System/Library/Fonts/Supplemental")
    for name, file in (("Body", "Arial.ttf"), ("Bold", "Arial Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(fontdir / file)))
    pdf = canvas.Canvas(str(args.output), pagesize=A4)
    pdf.setTitle("CTWatch: ensayo limitado a 1 vCPU y 2 GiB")
    width, height = A4
    navy, blue, teal = "#132D46", "#2465AD", "#087E8B"
    page = 0

    def text(x, y, value, size=10, bold=False, color=navy):
        pdf.setFillColor(colors.HexColor(color))
        pdf.setFont("Bold" if bold else "Body", size)
        pdf.drawString(x, y, value)

    def start(title):
        nonlocal page
        if page:
            pdf.showPage()
        page += 1
        pdf.setFillColor(colors.HexColor(navy))
        pdf.rect(0, height - 70, width, 70, fill=1, stroke=0)
        text(45, height - 43, title, 19, True, "#FFFFFF")
        text(45, 26, "CTWatch | Medición local Docker ARM64 | 19 noviembre 2026", 8)
        text(width - 65, 26, f"{page}/{args.total_pages}", 8)

    def lines(y, values, size=10):
        for value in values:
            text(45, y, value, size)
            y -= 17
        return y

    def rows(y, values):
        for i, (key, value) in enumerate(values):
            if i % 2 == 0:
                pdf.setFillColor(colors.HexColor("#EEF3F7"))
                pdf.rect(42, y - 8, width - 84, 26, fill=1, stroke=0)
            text(50, y, key, 9)
            text(320, y, value, 9, True)
            y -= 26
        return y

    def chart(top, title, series, unit, ceiling=None):
        text(45, top, title, 12, True)
        x0, y0, w, h = 80, top - 202, width - 130, 166
        ymax = ceiling or max(1, max(y for _, values, _ in series for _, y in values) * 1.08)
        xmax = max(p["monotonic_seconds"] - zero for p in points) / 60
        for tick in range(5):
            y = y0 + h * tick / 4
            pdf.setStrokeColor(colors.HexColor("#DBE3EB"))
            pdf.setLineWidth(0.4)
            pdf.line(x0, y, x0 + w, y)
            text(45, y - 3, number(ymax * tick / 4, 2 if ymax <= 2 else 0), 8)
        for tick in range(5):
            x = x0 + w * tick / 4
            text(x - 5, y0 - 16, number(xmax * tick / 4, 1), 8)
        text(x0, y0 + h + 10, unit, 8)
        text(x0 + w - 42, y0 - 32, "minutos", 8)
        for _name, values, color in series:
            pdf.setStrokeColor(colors.HexColor(color))
            pdf.setLineWidth(1.1)
            path = pdf.beginPath()
            for index, (x, y) in enumerate(values):
                xx, yy = x0 + x / xmax * w, y0 + y / ymax * h
                if index == 0:
                    path.moveTo(xx, yy)
                else:
                    path.lineTo(xx, yy)
            pdf.drawPath(path)
        text(80, y0 - 46, " | ".join(name for name, _, _ in series), 8)

    def values(key, scale=1):
        return [((p["monotonic_seconds"] - zero) / 60, p[key] / scale) for p in points]

    def rates(key, scale=1):
        result = []
        for i in range(1, len(points)):
            j = max(0, i - 10)
            dt = points[i]["monotonic_seconds"] - points[j]["monotonic_seconds"]
            if dt > 0:
                result.append(
                    (
                        (points[i]["monotonic_seconds"] - zero) / 60,
                        (points[i][key] - points[j][key]) / dt * scale,
                    )
                )
        return result

    start("1. Condiciones y resultados")
    y = lines(
        744,
        [
            f"Inicio UTC: {data['started_utc']}",
            f"Ventana configurada: {data['configured_seconds']} s; con drenaje: {data['elapsed_seconds']} s.",
            f"Cuota: {resources['cpu_limit_cpu_max']}; memoria: {int(resources['memory_limit_bytes']) / 2**30:.0f} GiB; swap desactivado.",
            f"{data['workers']} workers; {data['watch_domains']} dominios; HTTP paralelo {data['http_concurrency']}; polling {data['poll_seconds']} s.",
            "Caché exacta: 500.000 entradas, TTL 1 h. CAA activo y controles positivos reales.",
            "Muestras ~1 s. CPU/memoria incluyen padre, workers, instrumentación y kernel.",
        ],
    )
    y = rows(
        y - 12,
        [
            ("Entradas procesadas (con drenaje)", number(data["entries_processed"], 0)),
            (
                "Caudal observado durante ventana activa",
                number(analysis["active_entries_per_minute"], 0) + "/min",
            ),
            (
                "CPU media / p95 (ventana activa)",
                f"{number(summary['average_cpu_cores'], 3)} / {number(summary['sampled_cpu_cores_p95'], 3)} vCPU",
            ),
            (
                "Periodos CPU limitados / periodos totales",
                f"{summary['cpu_throttled_periods']} / {summary['cpu_periods']}",
            ),
            (
                "Memoria media / pico del kernel",
                f"{number(summary['memory_time_weighted_average_bytes'] / 2**20)} / {number(resources['memory_kernel_peak_whole_container_lifetime_bytes'] / 2**20)} MiB",
            ),
            (
                "Caché final / aciertos",
                f"{data['certificate_cache_entries_at_end']} / {number(data['cache_hit_fraction'] * 100)} %",
            ),
            (
                "Primer llenado de caché",
                "No alcanzado" if cache_full is None else number(cache_full) + " s",
            ),
            (
                "Heads válidos / logs con entradas",
                f"{data['logs_head_ok']}/{data['logs_selected']} / {data['logs_with_entries']}/{data['logs_selected']}",
            ),
            (
                "Backlog relativo a últimos heads",
                number(sum(p.get("backlog", 0) for p in data["logs"]), 0),
            ),
            (
                "OOM / procesos terminados por OOM",
                f"{points[-1]['memory_events'].get('oom', 0)} / {points[-1]['memory_events'].get('oom_kill', 0)}",
            ),
        ],
    )
    lines(
        y - 10,
        [
            "El caudal refleja actividad y restricciones externas; no mide capacidad máxima.",
            "Los fallos de caché dejan de equivaler a certificados únicos tras expulsiones LRU.",
            "Edad CT incluye publicación del log, polling y colas: no es latencia interna.",
            "Sin DynamoDB/SQS, persistencia, recuperación de cursores ni publicación AWS.",
            "La carga CAA depende de los pocos matches de control; no es un ensayo masivo.",
        ],
        9,
    )
    start("2. CPU, memoria y caché")
    chart(
        744,
        "CPU consumida con cuota de 1 vCPU",
        [
            ("CPU (media móvil ~10 s)", rates("cpu_seconds"), blue),
        ],
        "vCPU equivalentes",
        1.1,
    )
    chart(
        457,
        "Memoria del contenedor y caché de certificados",
        [
            ("Memoria MiB", values("memory_current_bytes", 2**20), teal),
            ("Caché (miles de entradas)", values("certificate_cache_entries", 1000), blue),
        ],
        "MiB / miles de entradas",
    )
    lines(
        160,
        [
            "La CPU puede superar ligeramente 1 en intervalos cortos por ventanas de cuota.",
            "El pico kernel registra ráfagas entre muestras; no se suman RSS máximos separados.",
            "El descenso de memoria final corresponde al cierre de los workers.",
            "Caché de nombres adicional: hasta 100.000 entradas por worker, TTL 1 h.",
        ],
        9,
    )
    start("3. Caudal, pendientes y red")
    chart(
        744,
        "Entrada procesada y nuevos análisis",
        [
            ("Observaciones/min", rates("entries_processed", 60), blue),
            ("Fallos de caché/min", rates("certificate_cache_misses", 60), teal),
        ],
        "entradas/min; media móvil ~10 s",
    )
    chart(
        457,
        "Pendientes respecto a los últimos heads observados",
        [
            ("Backlog (miles)", values("backlog_last_observed_heads", 1000), blue),
        ],
        "miles de entradas",
    )
    lines(
        160,
        [
            f"Red activa: RX {number(summary['network_rx_bytes'] / 1e9, 3)} GB; TX {number(summary['network_tx_bytes'] / 1e6)} MB.",
            f"Media RX {number(summary['network_rx_bytes'] / elapsed / 1e6, 2)} MB/s; no equivale a tráfico AWS facturado.",
            "Backlog con heads no sincronizados: consultar antigüedad y estado de cada log.",
            "Los 429 y errores de protocolo pueden acumular pendientes con CPU disponible.",
        ],
        9,
    )
    start("4. Errores, latencias y trazabilidad")
    y = lines(
        744,
        [
            "Respuestas HTTP: " + "; ".join(f"{k}: {v}" for k, v in data["http_statuses"].items()),
            f"CAA, eventos únicos en vivo: {data['unique_match_events']['live']}; estados: "
            + str(data["caa_unique_event_statuses"]["live"]),
        ],
        9,
    )
    text(
        45, y - 10, "Tiempos por lote: pared, con esperas; lotes concurrentes se solapan", 10, True
    )
    rows(
        y - 42,
        [
            (name, f"p50 {v['p50']} s; p95 {v['p95']} s; p99 {v['p99']} s")
            for name, v in data["batch_stage_wall_seconds"].items()
            if v
        ],
    )
    text(45, 559, "Diez logs con más pendientes al finalizar", 11, True)
    y = 532
    for log in sorted(data["logs"], key=lambda p: p.get("backlog", 0), reverse=True)[:10]:
        age = points[-1]["monotonic_seconds"] - log.get("head_observed_monotonic", zero)
        text(45, y, log["name"][:42], 8)
        text(
            295,
            y,
            f"{log.get('backlog', 0):,} pend.; {log['error_count']} errores; head {age:.0f} s",
            8,
        )
        y -= 20
    lines(
        285,
        [
            "Condiciones y límites de interpretación:",
            "- No es una comparación simultánea A/B: cambian las emisiones y los rate limits.",
            "- La lista es mayoritariamente sintética; no representa una tasa alta de matches.",
            "- 20 minutos no cubren la expiración TTL de 1 h ni prueban disponibilidad prolongada.",
            "- No se mide la ocupación de la caché de nombres interna de cada worker.",
            "- No se omitieron entradas deliberadamente por rate limits o falta de capacidad.",
            "Fuente: " + args.source.name,
            "SHA-256:",
            analysis["source_sha256"],
            "Series completas, presión CPU/memoria/IO, eventos OOM y contadores por log en JSON.",
        ],
        8,
    )
    start("5. Red y periodos limitados por CPU")
    chart(
        744,
        "Recepción por interfaces del contenedor",
        [("RX, media móvil ~10 s", rates("network_rx_bytes", 8 / 1e6), blue)],
        "Mbit/s",
    )
    throttle = []
    for i in range(1, len(points)):
        j = max(0, i - 10)
        periods = points[i]["cpu_periods"] - points[j]["cpu_periods"]
        limited = points[i]["cpu_throttled_periods"] - points[j]["cpu_throttled_periods"]
        if periods:
            throttle.append(((points[i]["monotonic_seconds"] - zero) / 60, limited / periods * 100))
    chart(
        457,
        "Periodos de cuota CPU con throttling",
        [("Fracción de periodos, ventana móvil ~10 s", throttle, teal)],
        "% de periodos",
    )
    lines(
        160,
        [
            "Un periodo limitado indica una ráfaga que agotó cuota; no saturación continua.",
            "Las métricas de presión CPU/memoria/IO se conservan completas en el JSON fuente.",
            "Los contadores de interfaz incluyen tráfico y metadatos del contenedor.",
        ],
        9,
    )
    pdf.save()
    print(json.dumps(analysis, indent=2))


if __name__ == "__main__":
    main()
