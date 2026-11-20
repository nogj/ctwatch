"""Render exact audit counts, hit popularity and the idealized capacity curve."""
# ruff: noqa: E501

import argparse
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


def fmt(value, digits=0):
    if value is None:
        return "n/d"
    return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--page-offset", type=int, default=0)
    parser.add_argument("--total-pages", type=int, default=3)
    args = parser.parse_args()
    d = json.loads(args.source.read_text())
    for name, filename in (("Body", "Arial.ttf"), ("Bold", "Arial Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, "/System/Library/Fonts/Supplemental/" + filename))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(args.output), pagesize=A4)
    width, height = A4
    navy, blue, teal = "#132D46", "#2465AD", "#087E8B"

    def text(x, y, value, size=10, bold=False, color=navy):
        pdf.setFont("Bold" if bold else "Body", size)
        pdf.setFillColor(colors.HexColor(color))
        pdf.drawString(x, y, value)

    def page(title, num):
        if num > 1:
            pdf.showPage()
        pdf.setFillColor(colors.HexColor(navy))
        pdf.rect(0, height - 70, width, 70, fill=1, stroke=0)
        text(45, height - 43, title, 18, True, "#FFFFFF")
        text(45, 26, "CTWatch | Auditoría adicional; no mezclar con la prueba base de recursos", 8)
        text(width - 65, 26, f"{num + args.page_offset}/{args.total_pages}", 8)

    def table(top, rows, widths):
        y = top
        for i, row in enumerate(rows):
            pdf.setFillColor(colors.HexColor(navy if i == 0 else "#EEF3F7" if i % 2 else "#FFFFFF"))
            pdf.rect(43, y - 9, sum(widths), 25, fill=1, stroke=0)
            x = 49
            for value, w in zip(row, widths, strict=True):
                text(x, y, str(value), 8.5, i == 0, "#FFFFFF" if i == 0 else navy)
                x += w
            y -= 25
        return y

    def chart(top, title, series, unit):
        text(45, top, title, 12, True)
        x0, y0, w, h = 90, top - 210, width - 140, 170
        xmax = max((x for _, values, _ in series for x, _ in values), default=1) or 1
        ymax = max((y for _, values, _ in series for _, y in values), default=1) * 1.1 or 1
        for tick in range(5):
            yy = y0 + h * tick / 4
            pdf.setStrokeColor(colors.HexColor("#DCE4EB"))
            pdf.setLineWidth(0.4)
            pdf.line(x0, yy, x0 + w, yy)
            text(45, yy - 3, fmt(ymax * tick / 4, 1), 8)
            text(x0 + w * tick / 4 - 5, y0 - 16, fmt(xmax * tick / 4, 0), 8)
        text(x0, y0 + h + 10, unit, 8)
        for _name, values, color in series:
            path = pdf.beginPath()
            for i, (x, y) in enumerate(values):
                xx, yy = x0 + x / xmax * w, y0 + y / ymax * h
                if i:
                    path.lineTo(xx, yy)
                else:
                    path.moveTo(xx, yy)
            pdf.setStrokeColor(colors.HexColor(color))
            pdf.setLineWidth(1.5)
            pdf.drawPath(path)
        text(90, y0 - 37, " | ".join(name for name, _, _ in series), 8)

    page("6. Duplicados antes y después del llenado", 1)
    text(
        45,
        744,
        f"Certificados DER distintos en toda la captura: {fmt(d['unique_der_fingerprints_entire_window'])}",
        11,
        True,
    )
    text(
        45,
        721,
        "Referencia exacta fuera del contenedor: conserva todas las huellas sin LRU ni TTL.",
        9,
    )
    full = d["first_cache_full_seconds_from_first_lookup"]
    text(
        45,
        700,
        "Primer lookup con caché llena: "
        + (fmt(full, 1) + " s" if full is not None else "no alcanzado"),
        9,
    )
    if "capture" in d:
        text(
            45,
            681,
            f"Segunda captura: {d['capture']['started_utc']}; con drenaje: {d['capture']['elapsed_seconds']} s.",
            8,
        )
    before, after = d["observed_phases"]["before_full"], d["observed_phases"]["after_full"]
    table(
        660,
        [["Medida", "Antes de llenar", "Después de llenar"]]
        + [
            [label, fmt(before.get(key, 0)), fmt(after.get(key, 0))]
            for label, key in (
                ("Observaciones", "observations"),
                ("Observaciones ya vistas", "duplicate_observations"),
                ("Análisis ejecutados", "analysis_calls"),
                ("Reanálisis de huellas ya vistas", "reanalysis_calls"),
                ("Hits de caché", "cache_hits"),
                ("Hits en vuelo", "inflight_hits"),
            )
        ]
        + [
            [
                "Reanálisis / análisis",
                fmt((before["reanalysis_fraction_of_analysis"] or 0) * 100, 2) + " %",
                fmt((after["reanalysis_fraction_of_analysis"] or 0) * 100, 2) + " %",
            ]
        ],
        [245, 125, 125],
    )
    minutes = sorted((int(k), v) for k, v in d["observed_minute_buckets"].items())
    active_minutes = d.get("capture", {}).get("configured_seconds", 1200) / 60
    minutes = [(m, v) for m, v in minutes if m < active_minutes]
    chart(
        409,
        "Porcentajes de reanálisis por minuto",
        [
            (
                "Reanálisis / análisis",
                [
                    (m + 1, 100 * v["reanalysis_calls"] / v["analysis_calls"])
                    for m, v in minutes
                    if v["analysis_calls"]
                ],
                blue,
            ),
            (
                "Reanálisis / duplicados",
                [
                    (m + 1, 100 * v["reanalysis_calls"] / v["duplicate_observations"])
                    for m, v in minutes
                    if v["duplicate_observations"]
                ],
                teal,
            ),
        ],
        "%; eje X: minuto desde primer lookup",
    )
    text(
        45,
        120,
        "La caché reutiliza resultados; los eventos repetidos se resuelven después por event_id.",
        9,
    )
    text(
        45,
        101,
        "Totales con drenaje; gráfico de la ventana activa. No se ha medido entrega SQS ni entrega única.",
        8.5,
    )
    events = d.get("pre_persistence_event_repetition_counts", {})
    if events:
        before_events = events.get("before_cache_full", {})
        after_events = events.get("after_cache_full", {})
        text(
            45, 81, "IDs de evento repetidos antes de persistir (muestra pequeña de matches):", 8.5
        )
        text(
            45,
            63,
            f"Antes: {before_events.get('repeated_event_ids', 0)}/{before_events.get('observations', 0)}; "
            f"después: {after_events.get('repeated_event_ids', 0)}/{after_events.get('observations', 0)} observaciones.",
            8.5,
        )

    page("7. Distribución de hits por certificado", 2)
    hits = d["cache_hits_per_certificate"]
    text(45, 744, "Identidad: SHA-256 del DER, estable entre logs y residencias en caché.", 10)
    text(
        45,
        722,
        "Incluye todos los certificados, también los de cero hits. In-flight se cuenta por separado.",
        9,
    )
    labels = list(hits["buckets"])
    count = sum(hits["buckets"].values())
    table(
        682,
        [["Hits de caché", "Certificados", "% de certificados"]]
        + [
            [
                label,
                fmt(hits["buckets"][label]),
                fmt(hits["buckets"][label] / count * 100, 2) + " %",
            ]
            for label in labels
        ],
        [165, 165, 165],
    )
    perc = hits["percentiles_including_zero"]
    text(
        45, 403, "Hits por certificado: " + "; ".join(f"{k}: {fmt(v)}" for k, v in perc.items()), 9
    )
    share = hits["top_one_percent_share"]
    text(
        45,
        380,
        f"El 1 % más frecuente concentra {fmt(share * 100 if share is not None else None, 2)} % de los hits de caché.",
        10,
        True,
    )
    re = d["reanalyses_per_certificate"]
    text(
        45,
        351,
        f"Certificados sin reanálisis: {fmt(re['buckets']['0'])} / {fmt(re['population_entries'])}.",
        10,
    )
    text(
        45,
        326,
        "Se conserva el histograma exacto, no solo estos intervalos, en el JSON de auditoría.",
        9,
    )
    text(45, 293, "Cómo interpretar la popularidad", 12, True)
    for i, line in enumerate(
        [
            "Muchos certificados con pocos hits pueden coexistir con una cola de certificados muy repetidos.",
            "El número de hits por sí solo no decide cuánto debe ocupar la caché: importa la separación",
            "temporal entre repeticiones y cuántas otras identidades aparecen entre ellas.",
            "La curva de capacidad siguiente conserva el orden temporal de todas las observaciones.",
            "Los precertificados y certificados finales se mantienen como DER distintos.",
            "Una captura corta favorece a los certificados que aparecen al principio de la ventana.",
        ]
    ):
        text(45, 265 - 21 * i, line, 9)

    page("8. Curva de capacidad de la caché", 3)
    curve = d["ideal_lru_capacity_curve"]
    table(
        744,
        [["Capacidad", "Hits", "Hit rate", "Análisis", "Reanálisis"]]
        + [
            [
                fmt(row["capacity"]),
                fmt(row["hits"]),
                fmt(row["hit_fraction"] * 100, 2) + " %",
                fmt(row["analysis_calls"]),
                fmt(row["reanalyses"]),
            ]
            for row in curve
        ],
        [95, 110, 90, 100, 100],
    )
    chart(
        562,
        "Análisis necesarios según capacidad simulada",
        [
            (
                "Simulación LRU ideal",
                [(r["capacity"] / 1000, r["analysis_calls"] / 1000) for r in curve],
                teal,
            )
        ],
        "miles de análisis; eje X: capacidad (miles de entradas)",
    )
    text(45, 286, "Modelo y límites", 12, True)
    for i, line in enumerate(
        [
            "Simulación sobre la misma secuencia de huellas, TTL fijo de 1 h y sin extenderlo en los hits.",
            "Inserción inmediata al primer lookup; el motor real inserta al completar el análisis.",
            "Por tanto, la curva orienta el dimensionamiento y no sustituye un ensayo con cada tamaño.",
            "No estima directamente CPU ahorrada ni memoria de tamaños que no se han ejecutado.",
            "La captura auditada incluye escritura de traza y caché de archivos; la referencia exacta",
            "y las simulaciones se calculan fuera del contenedor limitado.",
            "Veinte minutos no cubren el vencimiento TTL de una hora ni un ciclo diario de emisiones.",
            "1M no se llena en esta captura: cero reanálisis simulados no implica cero en producción.",
            "Fuente: " + args.source.name,
        ]
    ):
        text(45, 261 - 21 * i, line, 9)
    pdf.save()


if __name__ == "__main__":
    main()
