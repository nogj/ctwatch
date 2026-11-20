"""Build the measured CTWatch report with vector time-series charts (ReportLab)."""
# ruff: noqa: E501

import hashlib
import json
import math
from pathlib import Path

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/ctwatch-informe-volumetrias-recursos-2026-11-19.pdf"
SOURCE = ROOT / "docs/benchmarks/resources-2026-11-19-5min.json"
BASELINE = ROOT / "docs/benchmarks/global-2026-11-19-apple-caa.json"
D = json.loads(SOURCE.read_text())
B = json.loads(BASELINE.read_text())
R = D["resources"]
A = R["active_window_sampled_summary"]
TOTAL = R["ingestion_and_drain"]
W = 495
NAVY = colors.HexColor("#132D46")
BLUE = colors.HexColor("#2465AD")
TEAL = colors.HexColor("#087E8B")
GRAY = colors.HexColor("#526477")
LIGHT = colors.HexColor("#EAF0F5")
ORANGE = colors.HexColor("#AD6017")

FONT_DIR = Path("/System/Library/Fonts/Supplemental")
for name, filename in [
    ("Body", "Arial.ttf"),
    ("BodyBold", "Arial Bold.ttf"),
    ("BodyItalic", "Arial Italic.ttf"),
]:
    pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="BodyBold", italic="BodyItalic")
ST = getSampleStyleSheet()
ST.add(
    ParagraphStyle(
        "BodyTextReport", fontName="Body", fontSize=9.4, leading=13.2, textColor=NAVY, spaceAfter=7
    )
)
ST.add(
    ParagraphStyle(
        "SmallReport",
        parent=ST["BodyTextReport"],
        fontSize=8,
        leading=10.8,
        textColor=GRAY,
        spaceAfter=5,
    )
)
ST.add(
    ParagraphStyle(
        "ReportTitle", fontName="BodyBold", fontSize=25, leading=29, textColor=NAVY, spaceAfter=10
    )
)
ST.add(
    ParagraphStyle(
        "ReportH1", fontName="BodyBold", fontSize=19, leading=24, textColor=NAVY, spaceAfter=10
    )
)
ST.add(
    ParagraphStyle(
        "ReportH2",
        fontName="BodyBold",
        fontSize=11.5,
        leading=15,
        textColor=TEAL,
        spaceBefore=9,
        spaceAfter=6,
    )
)
ST.add(
    ParagraphStyle(
        "CellReport", parent=ST["BodyTextReport"], fontSize=8.8, leading=11.4, spaceAfter=0
    )
)
ST.add(
    ParagraphStyle(
        "CellWhite", parent=ST["CellReport"], fontName="BodyBold", textColor=colors.white
    )
)
ST.add(
    ParagraphStyle(
        "KPI", fontName="BodyBold", fontSize=25, leading=30, textColor=TEAL, alignment=TA_CENTER
    )
)
ST.add(ParagraphStyle("KPILabel", parent=ST["SmallReport"], alignment=TA_CENTER))


def n(value, digits=0):
    return f"{value:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def p(text, style="BodyTextReport"):
    return Paragraph(text, ST[style])


def table(rows, widths=None):
    cells = [
        [p(str(cell), "CellWhite" if index == 0 else "CellReport") for cell in row]
        for index, row in enumerate(rows)
    ]
    t = Table(cells, colWidths=widths or [W * 0.61, W * 0.39], repeatRows=1, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F1F5F8"), colors.white]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#CCD7E0")),
            ]
        )
    )
    return t


def note(text):
    box = Table([[p(text, "SmallReport")]], colWidths=[W])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D2DFE9")),
                ("LEFTPADDING", (0, 0), (-1, -1), 11),
                ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return box


POINTS = R["series"][len(R["series"]) - TOTAL["samples"] :]
ZERO = POINTS[0]["monotonic_seconds"]
X = [row["monotonic_seconds"] - ZERO for row in POINTS]
CPU, NET, SMOOTH_CPU, SMOOTH_NET = [], [], [], []
for i in range(1, len(POINTS)):
    before, after = POINTS[i - 1], POINTS[i]
    dt = X[i] - X[i - 1]
    if dt <= 0:
        continue
    CPU.append((X[i], (after["cpu_seconds"] - before["cpu_seconds"]) / dt))
    NET.append((X[i], (after["network_rx_bytes"] - before["network_rx_bytes"]) / dt * 8 / 1e6))
    j = i - 1
    while j > 0 and X[j] > X[i] - 10:
        j -= 1
    span = X[i] - X[j]
    SMOOTH_CPU.append((X[i], (after["cpu_seconds"] - POINTS[j]["cpu_seconds"]) / span))
    SMOOTH_NET.append(
        (X[i], (after["network_rx_bytes"] - POINTS[j]["network_rx_bytes"]) / span * 8 / 1e6)
    )


def chart(lines, maximum, step, unit, legends):
    drawing = Drawing(W, 201)
    left, bottom, width, height = 45, 33, W - 61, 145
    xmax = TOTAL["elapsed_seconds"]
    drawing.add(
        Rect(left, bottom, width, height, fillColor=colors.HexColor("#FAFCFD"), strokeColor=None)
    )
    shade = left + width * 300 / xmax
    drawing.add(
        Rect(
            shade,
            bottom,
            left + width - shade,
            height,
            fillColor=colors.HexColor("#FCEFD9"),
            strokeColor=None,
        )
    )
    plot = LinePlot()
    plot.x, plot.y, plot.width, plot.height = left, bottom, width, height
    plot.data = [values for values, _, _, _ in lines]
    plot.xValueAxis.valueMin = 0
    plot.xValueAxis.valueMax = xmax
    plot.xValueAxis.valueSteps = [0, 60, 120, 180, 240, 300, 330]
    plot.xValueAxis.labelTextFormat = "%d"
    plot.yValueAxis.valueMin = 0
    plot.yValueAxis.valueMax = maximum
    plot.yValueAxis.valueStep = step
    for axis in (plot.xValueAxis, plot.yValueAxis):
        axis.labels.fontName = "Body"
        axis.labels.fontSize = 7.4
        axis.strokeColor = colors.HexColor("#B7C5D0")
        axis.strokeWidth = 0.5
    plot.yValueAxis.visibleGrid = True
    plot.yValueAxis.gridStrokeColor = colors.HexColor("#DCE5EC")
    plot.yValueAxis.gridStrokeWidth = 0.4
    plot.yValueAxis.labelTextFormat = lambda value: n(value, 1 if step < 1 else 0)
    for i, (_, color, line_width, dashed) in enumerate(lines):
        plot.lines[i].strokeColor = color
        plot.lines[i].strokeWidth = line_width
        if dashed:
            plot.lines[i].strokeDashArray = [4, 3]
    drawing.add(plot)
    drawing.add(String(left, 189, unit, fontName="Body", fontSize=8, fillColor=GRAY))
    drawing.add(String(shade - 4, 183, "Drenaje", fontName="Body", fontSize=7, fillColor=ORANGE))
    drawing.add(
        String(
            W / 2,
            3,
            "Segundos desde el inicio de la ingesta",
            fontName="Body",
            fontSize=7.6,
            fillColor=GRAY,
            textAnchor="middle",
        )
    )
    # Legends outside plotting area, aligned at the top.
    offset = 125
    for label, color in legends:
        drawing.add(Line(offset, 192, offset + 14, 192, strokeColor=color, strokeWidth=1.5))
        drawing.add(String(offset + 18, 189, label, fontName="Body", fontSize=7.4, fillColor=GRAY))
        offset += pdfmetrics.stringWidth(label, "Body", 7.4) + 35
    return drawing


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, A4[1] - 13, A4[0], 13, stroke=0, fill=1)
    canvas.setFont("BodyBold", 8)
    canvas.drawString(44, A4[1] - 35, "CTWATCH / VALIDACIÓN LOCAL")
    canvas.setFillColor(GRAY)
    canvas.setFont("Body", 7.5)
    canvas.drawRightString(A4[0] - 44, A4[1] - 35, "19 NOV 2026")
    canvas.setStrokeColor(colors.HexColor("#D6E0E8"))
    canvas.line(44, 36, A4[0] - 44, 36)
    canvas.drawString(44, 23, "Mediciones Docker ARM64. Sin despliegue ni publicación en AWS.")
    canvas.drawRightString(A4[0] - 44, 23, f"{doc.page} / 5")
    canvas.restoreState()


story = [
    p("Volumetrías y consumo<br/>de recursos", "ReportTitle"),
    p("Ingesta directa de Certificate Transparency · Chrome + Apple", "BodyTextReport"),
    p(
        "Informe de pruebas del 19 de noviembre de 2026. Referencia principal: ejecución instrumentada de cinco minutos.",
        "SmallReport",
    ),
    Spacer(1, 9),
]
cards = Table(
    [
        [
            [
                p(n(A["average_cpu_cores"], 3), "KPI"),
                p("núcleos CPU de media<br/>durante la ventana activa", "KPILabel"),
            ],
            [
                p(n(R["memory_kernel_peak_whole_container_lifetime_bytes"] / 2**20), "KPI"),
                p("MiB de memoria máxima<br/>agregada del contenedor", "KPILabel"),
            ],
            [
                p(n(D["entries_processed"]), "KPI"),
                p("entradas CT procesadas<br/>incluidos semilla y drenaje", "KPILabel"),
            ],
        ]
    ],
    colWidths=[W / 3] * 3,
)
cards.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]
    )
)
story += [
    cards,
    Spacer(1, 12),
    note(
        "<b>Lectura principal.</b> El consumo observado está muy por debajo de los límites asignados. Persisten errores y backlog de algunos logs; estas cifras no acreditan cobertura completa ni un tamaño definitivo de producción."
    ),
    p("1. Condiciones de ejecución", "ReportH2"),
    table(
        [
            ["Condición", "Configuración"],
            ["Inicio de la prueba principal", "19/11/2026, 17:28:16 Madrid (16:28:16 UTC)"],
            ["Entorno", "Docker local, Linux aarch64, Python 3.12.16"],
            ["Límites / procesos de análisis", "3 vCPU, 3 GiB RAM / 3 workers"],
            ["Ventanas", "300 s de ingesta; 330,34 s con drenaje; 335,87 s con preparación"],
            [
                "Catálogos y protocolos",
                "87 logs: 66 Static CT + 21 RFC6962; 57 usable + 30 qualified",
            ],
            ["Lectura", "16 peticiones concurrentes; polling 15 s al alcanzar el head"],
            [
                "Lista de vigilancia",
                "10.000 nombres sintéticos + 2 de ejemplo + 3 controles reales",
            ],
            [
                "Semilla y cursores",
                "Hasta 256 entradas recientes por log; cursores solo en memoria",
            ],
            ["Caché de certificados", "500.000 entradas; TTL 1 h; expulsión LRU anticipada"],
            ["Caché de nombres / CAA", "100.000 por worker, TTL 1 h / CCADB + DNS tras exact"],
        ]
    ),
    Spacer(1, 9),
    p(
        "No se ejecutaron DynamoDB, SQS, Lambda ni operaciones de cuenta AWS. No se archivó el flujo bruto; se guardaron informes y muestras de eventos. El coste de la instrumentación está incluido.",
        "SmallReport",
    ),
]

story += [
    PageBreak(),
    p("2. Volumetría y cobertura", "ReportH1"),
    table(
        [
            ["Indicador", "Prueba principal"],
            [
                "Entradas procesadas / posteriores al head inicial",
                f"{n(D['entries_processed'])} / {n(D['new_entries_processed'])}",
            ],
            ["Caudal sobre ingesta + drenaje", f"{n(D['entries_per_second'], 2)} entradas/s"],
            [
                "Análisis resueltos por caché",
                f"{n(D['certificate_cache_hits'])} ({n(D['cache_hit_fraction'] * 100, 2)} %)",
            ],
            [
                "Fallos de caché / entradas al terminar",
                f"{n(D['certificate_cache_misses'])} / {n(D['certificate_cache_entries_at_end'])}",
            ],
            [
                "HTTP: peticiones / cuerpos descargados",
                f"{n(D['http_requests'])} / {n(D['http_body_downloaded_bytes'] / 1e9, 3)} GB",
            ],
            ["Heads firmados verificados / logs con datos", "87/87 / 86/87"],
            ["Errores HTTP observados", "97 respuestas 429; 34 respuestas 404; 10 respuestas 403"],
            ["Backlog respecto a últimos heads observados", "184.164 entradas"],
            ["Edad desde aceptación CT: p50 / p95 / p99", "16,705 s / 215,336 s / 354,824 s"],
        ]
    ),
    Spacer(1, 9),
    note(
        "<b>No confundir edad CT con latencia interna.</b> Los percentiles incluyen publicación del log, polling y backlog. La muestra toma una entrada de cada 32 en los lotes y no incluye entrega a SQS. No es un SLA."
    ),
    p("Controles positivos y CAA", "ReportH2"),
    p(
        "Cuatro observaciones recientes de preparación contenían tres certificados distintos y tres nombres DNS. Produjeron cuatro eventos exactos, al distinguir wildcard y nombre base. En vivo hubo 11 observaciones de match, equivalentes a esos mismos cuatro eventos únicos. No son cuatro emisiones nuevas posteriores al inicio."
    ),
    p(
        "CCADB identificó sus emisores y DNS devolvió <b>unrestricted</b> para los cuatro eventos: no había un RRset CAA efectivo. No hubo error DNS en estos controles. No se ejercitó una política restrictiva real ni se midió CAA bajo alta tasa de matches.",
        "SmallReport",
    ),
    p("Comparación con la prueba anterior de dos minutos", "ReportH2"),
    table(
        [
            ["Indicador", "2 min + drenaje", "5 min + drenaje"],
            ["Tiempo total", "132,79 s", "330,34 s"],
            ["Entradas procesadas", n(B["entries_processed"]), n(D["entries_processed"])],
            ["Caché: capacidad / aciertos", "200.000 / 59,55 %", "500.000 / 65,66 %"],
            ["Logs con entradas", "86/87", "86/87"],
        ],
        [W * 0.44, W * 0.28, W * 0.28],
    ),
    Spacer(1, 7),
    p(
        "La actividad CT y la capacidad de caché fueron distintas; el cambio de caudal o aciertos no se atribuye causalmente a una única modificación. Tuscolo2027h1 no aportó entradas en la prueba principal por respuestas 429.",
        "SmallReport",
    ),
]

story += [
    PageBreak(),
    p("3. CPU y memoria en el tiempo", "ReportH1"),
    p("CPU agregada del contenedor", "ReportH2"),
    chart(
        [(CPU, colors.HexColor("#ACCAE6"), 0.6, False), (SMOOTH_CPU, BLUE, 1.7, False)],
        3,
        0.5,
        "Núcleos equivalentes",
        [("Intervalos ~1 s", colors.HexColor("#ACCAE6")), ("Ventana móvil ~10 s", BLUE)],
    ),
    p(
        "Media activa: <b>0,335 núcleos</b>; p95: 0,530; máximo muestreado: 2,138. La media equivale al 11,2 % de los 3 núcleos asignados. Se consumieron 100,68 s de CPU en la ventana activa y 104,33 s con drenaje. Cero periodos limitados por cuota.",
        "SmallReport",
    ),
    p("Memoria agregada del contenedor", "ReportH2"),
    chart(
        [
            (
                [
                    (x, row["memory_current_bytes"] / 2**20)
                    for x, row in zip(X, POINTS, strict=True)
                ],
                TEAL,
                1.6,
                False,
            ),
            (
                [
                    (0, R["memory_kernel_peak_whole_container_lifetime_bytes"] / 2**20),
                    (X[-1], R["memory_kernel_peak_whole_container_lifetime_bytes"] / 2**20),
                ],
                ORANGE,
                0.8,
                True,
            ),
        ],
        700,
        100,
        "MiB",
        [("Memoria actual", TEAL), ("Pico kernel", ORANGE)],
    ),
    p(
        "Media activa: <b>429,75 MiB</b>; pico kernel: <b>580,83 MiB</b>. Incluye padre, workers, caché de páginas y memoria del kernel imputada al cgroup. El descenso final coincide con el cierre de workers; no se han sumado máximos RSS independientes.",
        "SmallReport",
    ),
    table(
        [
            ["Ventana de medición", "CPU media", "Memoria media"],
            ["Activa, muestra próxima a 300 s", "0,335 núcleos", "429,75 MiB"],
            ["Ingesta + drenaje, 330,34 s", "0,316 núcleos", "440,67 MiB"],
        ],
        [W * 0.5, W * 0.25, W * 0.25],
    ),
]

netmax = max(20, math.ceil(max(y for _, y in NET) / 20) * 20)
story += [
    PageBreak(),
    p("4. Red y crecimiento de caché", "ReportH1"),
    p("Recepción por las interfaces del contenedor", "ReportH2"),
    chart(
        [(NET, colors.HexColor("#ACCAE6"), 0.6, False), (SMOOTH_NET, BLUE, 1.7, False)],
        netmax,
        netmax / 4,
        "Mbit/s",
        [("Intervalos ~1 s", colors.HexColor("#ACCAE6")), ("Ventana móvil ~10 s", BLUE)],
    ),
    p(
        "Recepción media activa: <b>4,27 MB/s (34,1 Mbit/s)</b>. En ingesta + drenaje se recibieron 1,304 GB y se enviaron 7,46 MB. Son bytes de interfaz, no volumen almacenado ni tráfico AWS facturable.",
        "SmallReport",
    ),
    p("Entradas de la caché exacta de certificados", "ReportH2"),
    chart(
        [
            (
                [
                    (x, row["certificate_cache_entries"] / 1000)
                    for x, row in zip(X, POINTS, strict=True)
                ],
                TEAL,
                1.6,
                False,
            ),
            ([(0, 500), (X[-1], 500)], ORANGE, 0.8, True),
        ],
        550,
        100,
        "Miles de entradas",
        [("Entradas", TEAL), ("Capacidad configurada", ORANGE)],
    ),
    p(
        "Al terminar: <b>243.616 / 500.000 entradas (48,7 %)</b>. El crecimiento de memoria continúa junto al calentamiento de cachés. El máximo medido no representa la huella con todas las cachés llenas ni con seis workers.",
        "SmallReport",
    ),
    p("Extrapolación lineal ilustrativa", "ReportH2"),
    table(
        [
            ["Base: 330,34 s, incluido drenaje", "Por hora", "Por día"],
            [
                "Entradas procesadas, incluida semilla",
                n(D["entries_processed"] / D["elapsed_seconds"] * 3600 / 1e6, 2) + " M",
                n(D["entries_processed"] / D["elapsed_seconds"] * 86400 / 1e6, 1) + " M",
            ],
            [
                "Cuerpos HTTP descargados",
                n(D["http_body_downloaded_bytes"] / D["elapsed_seconds"] * 3600 / 1e9, 2) + " GB",
                n(D["http_body_downloaded_bytes"] / D["elapsed_seconds"] * 86400 / 1e9, 1) + " GB",
            ],
        ],
        [W * 0.54, W * 0.23, W * 0.23],
    ),
    Spacer(1, 5),
    p(
        "No es una previsión garantizada: incluye semilla/preparación en HTTP, actividad variable y cobertura incompleta. Unidades: MB/GB decimales; MiB/GiB binarios.",
        "SmallReport",
    ),
]

story += [
    PageBreak(),
    p("5. Método, límites y trazabilidad", "ReportH1"),
    p("Cómo se midió", "ReportH2"),
    p(
        "La instrumentación lee Linux cgroup v2: <b>cpu.stat</b>, <b>memory.current</b>, <b>memory.peak</b> y <b>memory.stat</b>. El tráfico procede de <b>/proc/net/dev</b>, excluido loopback. El coste de muestreo y de generar eventos locales queda incluido."
    ),
    p(
        "Se tomaron 336 muestras en el ensayo completo. En ingesta + drenaje hubo 331, con un intervalo máximo de 1,16 s. Las medias CPU son deltas acumulados divididos por tiempo real; la memoria media se pondera por duración. La ventana activa termina en la muestra más próxima al segundo 300 (300,32 s)."
    ),
    p(
        "Los gráficos muestran la ingesta y el drenaje. La banda ámbar empieza a los 300 s. La línea móvil de CPU/red usa deltas acumulados sobre aproximadamente 10 s; no es el consumo de un proceso aislado. El pico de memoria del kernel cubre toda la vida del contenedor, incluida preparación.",
        "SmallReport",
    ),
    p("Qué queda fuera de la conclusión", "ReportH2"),
    p(
        "<b>Cobertura:</b> 87 heads verificados no equivalen a 87 logs al día. Persisten rate limits, errores y 184.164 entradas pendientes respecto a los últimos heads observados."
    ),
    p(
        "<b>Dimensionamiento:</b> las cachés no se llenaron y se usaron tres workers, frente a seis en la configuración inicial de producción. Cinco minutos no prueban memoria máxima, capacidad sostenida ni comportamiento durante recuperaciones prolongadas."
    ),
    p(
        "<b>CAA:</b> se observa la política DNS actual. El lookup automático compara AKI/SKI y Subject de CCADB, pero no verifica la firma ni la cadena X.509. Una discrepancia no demuestra por sí sola incumplimiento al emitir."
    ),
    p(
        "<b>AWS:</b> no se midieron red de la cuenta, persistencia, leases, Streams, Lambda ni publicación SQS. No se calculan precios ni se acredita un SLA."
    ),
    note(
        "<b>Implicación operativa.</b> Los 8 vCPU/16 GiB inicialmente previstos superan ampliamente el consumo de esta muestra. Antes de fijar una reducción, conviene repetir con cachés llenas y un periodo largo que permita evaluar backlog, errores y volumen real de matches."
    ),
    p("Fuentes reproducibles", "ReportH2"),
    p(
        "Datos principales: <b>docs/benchmarks/resources-2026-11-19-5min.json</b><br/>Incluye serie temporal, límites, eventos de muestra y métricas por log.",
        "SmallReport",
    ),
    p("SHA-256: " + hashlib.sha256(SOURCE.read_bytes()).hexdigest(), "SmallReport"),
    p(
        "Comparación: <b>docs/benchmarks/global-2026-11-19-apple-caa.json</b><br/>SHA-256: "
        + hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "SmallReport",
    ),
    p(
        "Reproducción: <b>scripts/live_probe.py</b> con <b>--measure-resources --seconds 300 --workers 3 --cache-entries 500000 --caa --positive-controls 4</b>, dentro de un contenedor Linux con cgroup v2, limitado a 3 CPU/3 GiB.",
        "SmallReport",
    ),
]

OUT.parent.mkdir(parents=True, exist_ok=True)


class ReportDocument(SimpleDocTemplate):
    def afterPage(self):
        footer(self.canv, self)


document = ReportDocument(
    str(OUT),
    pagesize=A4,
    leftMargin=44,
    rightMargin=44,
    topMargin=58,
    bottomMargin=49,
    title="CTWatch - Volumetrías y recursos",
    author="CTWatch",
    subject="Condiciones y resultados de pruebas locales de CT",
)
document.build(story)
print(OUT)
