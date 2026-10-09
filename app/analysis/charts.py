"""Серверные SVG-графики без JavaScript (docs/contract.md, §7.2).

Четыре строителя: ``pareto_chart``, ``trend_chart``, ``spc_chart``,
``weight_bar``. Требования контракта: весь динамический текст проходит через
``html.escape``; корневой элемент имеет ``role="img"`` и ``<title>`` с
русским описанием; цвета серий — из ``config.CHART_SERIES``, оси и подписи —
``CHART_AXIS_TEXT``, сетка — ``CHART_GRID``; при пустом или недостаточном
входе возвращается ровно
``f'<p class="chart-empty">{CHART_EMPTY_TEXT}</p>'``; скриптов, внешних
шрифтов и URL нет; идентификаторы элементов уникальны внутри каждого вызова
(локальный префикс + ``uuid``, глобального счётчика нет); длинные подписи
обрезаются символом ``…``, полный текст помещается в ``<title>``.
"""

from __future__ import annotations

import html
import uuid
from typing import Sequence

from app.config import (
    CHART_AXIS_TEXT,
    CHART_EMPTY_TEXT,
    CHART_GRID,
    CHART_SERIES,
    SPC_VALUE_DP,
)
from app.domain.models import IndexWeights, ParetoResult, SpcResult, TrendPoint

# Цвета серий (только из config.CHART_SERIES).
_BAR = CHART_SERIES[0]        # столбцы, обычные точки
_DASH = CHART_SERIES[1]       # пунктир ±2σ̂
_ACCENT = CHART_SERIES[2]     # UCL / LCL
_LINE = CHART_SERIES[3]       # центральные линии, накопленная доля
_CONNECT = CHART_SERIES[4]    # соединение точек на картах
_HILITE = CHART_SERIES[5]     # нарушенные точки SPC

# Знаков после запятой в подписях сетки карт SPC (значения границ — SPC_VALUE_DP).
SPC_TICK_DP = 3


def _empty() -> str:
    """Контрактная пустая заглушка (§7.2)."""
    return f'<p class="chart-empty">{CHART_EMPTY_TEXT}</p>'


def _esc(value: object) -> str:
    """Экранирование любого динамического текста (HTML + XML безопасно)."""
    return html.escape(str(value), quote=True)


def _uid() -> str:
    """Локальный уникальный префикс идентификатора (без глобального счётчика)."""
    return uuid.uuid4().hex[:8]


def _num(value: float, dp: int = 2) -> str:
    return f"{value:.{dp}f}"


def _pt(value: float) -> str:
    return f"{value:.1f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _count_ticks(max_value: int) -> list[int]:
    """Деления целочисленной оси количества (столбцы Парето и партий).

    Шаг — «круглое» целое ``1/2/5×10^k``, подписей не более пяти; верхняя
    подпись — точное значение максимума, поэтому дробных делений
    («1.75 дефекта») не бывает.
    """
    max_value = int(max_value)
    if max_value < 1:
        return [0]
    nice = (mant * 10 ** exp for exp in range(18) for mant in (1, 2, 5))
    step = next((s for s in nice if -(-max_value // s) <= 4), max_value)
    ticks = list(range(0, max_value, step))
    ticks.append(max_value)
    return ticks


def _truncate(text: object, limit: int) -> str:
    """Обрезка длинной подписи с многоточием (полный текст — в <title>)."""
    raw = str(text)
    if limit < 2:
        limit = 2
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def _scale(values: Sequence[float]) -> tuple[float, float]:
    """Диапазон оси Y с запасом 12 % — общий для сетки и точек карт SPC."""
    lo, hi = min(values), max(values)
    span = hi - lo
    if span <= 0:
        span = abs(hi) * 0.01 or 1.0
    pad = span * 0.12
    return lo - pad, hi + pad


def _text(x: float, y: float, content: object, *, size: int = 12,
          anchor: str = "start", fill: str = CHART_AXIS_TEXT,
          tooltip: object = None, weight: str | None = None) -> str:
    weight_attr = f' font-weight="{_esc(weight)}"' if weight else ""
    head = (f'<text x="{_pt(x)}" y="{_pt(y)}" font-size="{size}" '
            f'fill="{_esc(fill)}" text-anchor="{_esc(anchor)}"{weight_attr}>')
    tip = f"<title>{_esc(tooltip)}</title>" if tooltip is not None else ""
    return f"{head}{tip}{_esc(content)}</text>"


def _line(x1: float, y1: float, x2: float, y2: float, *, stroke: str,
          width: float = 1.0, dash: str | None = None) -> str:
    dash_attr = f' stroke-dasharray="{_esc(dash)}"' if dash else ""
    return (f'<line x1="{_pt(x1)}" y1="{_pt(y1)}" x2="{_pt(x2)}" '
            f'y2="{_pt(y2)}" stroke="{_esc(stroke)}" '
            f'stroke-width="{width}"{dash_attr} />')


def _rect(x: float, y: float, w: float, h: float, *, fill: str,
          tooltip: object = None) -> str:
    tip = f"<title>{_esc(tooltip)}</title>" if tooltip is not None else ""
    return (f'<rect x="{_pt(x)}" y="{_pt(y)}" width="{_pt(w)}" '
            f'height="{_pt(h)}" fill="{_esc(fill)}">{tip}</rect>')


def _circle(cx: float, cy: float, r: float, *, fill: str,
            tooltip: object = None, stroke: str | None = None,
            stroke_width: float | None = None) -> str:
    stroke_attr = ""
    if stroke is not None:
        stroke_attr = (f' stroke="{_esc(stroke)}" '
                       f'stroke-width="{stroke_width if stroke_width is not None else 1}"')
    tip = f"<title>{_esc(tooltip)}</title>" if tooltip is not None else ""
    return (f'<circle cx="{_pt(cx)}" cy="{_pt(cy)}" r="{_pt(r)}" '
            f'fill="{_esc(fill)}"{stroke_attr}>{tip}</circle>')


def _svg_open(prefix: str, *, width: int, height: int, title: str,
              aria: str) -> str:
    # Без xmlns: графики вставляются в text/html как инлайновый SVG,
    # а namespace-URI выглядит как внешняя ссылка (контракт запрещает URL).
    return (f'<svg id="{prefix}-{_uid()}" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{_esc(aria)}">'
            f'<title>{_esc(title)}</title>')


def _legend(x: float, y: float, items: Sequence[tuple[str, str, str]]) -> str:
    """Легенда: ``items`` — (вид, цвет, подпись); вид rect|line|dash."""
    out: list[str] = []
    cursor = x
    for kind, color, label in items:
        if kind == "rect":
            out.append(_rect(cursor, y - 9, 12, 10, fill=color))
        elif kind == "dash":
            out.append(_line(cursor, y - 4, cursor + 14, y - 4,
                             stroke=color, width=2.0, dash="5 3"))
        else:
            out.append(_line(cursor, y - 4, cursor + 14, y - 4,
                             stroke=color, width=2.0))
        out.append(_text(cursor + 18, y, label, size=11))
        cursor += 18 + 7 * len(label) + 20
    return "".join(out)


# --- Парето ---------------------------------------------------------------

def pareto_chart(result: ParetoResult, *, width: int = 980,
                 height: int = 430) -> str:
    """Столбцы количества + линия накопленной доли + порог 80 % (§7.2)."""
    if result.is_empty or not result.rows:
        return _empty()
    rows = result.rows
    n = len(rows)

    ml, mr, mt, mb = 66, 78, 52, 84
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    plot_w, plot_h = x1 - x0, y1 - y0

    max_qty = max(row.qty for row in rows)
    if max_qty <= 0:
        return _empty()
    slot = plot_w / n
    bar_w = max(3.0, slot * 0.62)

    def y_qty(qty: float) -> float:
        return y1 - (qty / max_qty) * plot_h

    def y_cum(share: float) -> float:
        return y1 - share * plot_h

    parts = [
        _svg_open(
            "pareto",
            width=width,
            height=height,
            title=f"Диаграмма Парето: {n} кодов дефектов за {result.months} мес.",
            aria=f"Диаграмма Парето дефектов, всего {result.total_qty} шт.",
        ),
        _legend(x0, 22, [
            ("rect", _BAR, "Количество дефектов"),
            ("line", _LINE, "Накопленная доля"),
            ("dash", CHART_AXIS_TEXT, "Порог 80 %"),
        ]),
    ]

    # Сетка левой оси (количество): целочисленные деления.
    for value in _count_ticks(max_qty):
        y = y_qty(value)
        parts.append(_line(x0, y, x1, y, stroke=CHART_GRID, width=1))
        parts.append(_text(x0 - 8, y + 4, str(value), size=11, anchor="end"))

    # Правая ось накопленной доли.
    for frac, label in ((0.0, "0 %"), (0.5, "50 %"), (1.0, "100 %")):
        parts.append(_text(x1 + 8, y_cum(frac) + 4, label, size=11))

    # Порог 80 %.
    y80 = y_cum(0.80)
    parts.append(_line(x0, y80, x1, y80, stroke=CHART_AXIS_TEXT,
                       width=1.2, dash="6 5"))
    parts.append(_text(x1 + 8, y80 + 4, "80 %", size=11, weight="bold"))

    # Оси.
    parts.append(_line(x0, y0, x0, y1, stroke=CHART_AXIS_TEXT, width=1.2))
    parts.append(_line(x0, y1, x1, y1, stroke=CHART_AXIS_TEXT, width=1.2))

    # Столбцы и подписи.
    centers: list[float] = []
    chars = max(3, int(slot / 7))
    for i, row in enumerate(rows):
        cx = x0 + i * slot + slot / 2
        centers.append(cx)
        bar_h = (row.qty / max_qty) * plot_h
        if bar_h > 0:
            parts.append(_rect(
                cx - bar_w / 2, y1 - bar_h, bar_w, bar_h, fill=_BAR,
                tooltip=f"{row.code}: {row.name} — {row.qty} шт. "
                        f"({_pct(row.share)})",
            ))
        parts.append(_text(cx, y1 - bar_h - 6, str(row.qty), size=10,
                           anchor="middle"))
        parts.append(_text(cx, y1 + 18, _truncate(row.name, chars), size=11,
                           anchor="middle", tooltip=f"{row.code}: {row.name}"))
        parts.append(_text(cx, y1 + 34, row.code, size=10, anchor="middle"))

    # Линия накопленной доли.
    points_attr = " ".join(
        f"{_pt(cx)},{_pt(y_cum(row.cumulative))}"
        for cx, row in zip(centers, rows)
    )
    parts.append(f'<polyline points="{points_attr}" fill="none" '
                 f'stroke="{_esc(_LINE)}" stroke-width="2" />')
    for cx, row in zip(centers, rows):
        parts.append(_circle(cx, y_cum(row.cumulative), 3.5, fill=_LINE,
                             tooltip=f"{row.code}: {_pct(row.cumulative)}"))

    parts.append("</svg>")
    return "\n".join(parts)


# --- Динамика -------------------------------------------------------------

def trend_chart(points: Sequence[TrendPoint], *, width: int = 980,
                height: int = 360) -> str:
    """Столбцы числа партий + линия доли брака, % (§7.2)."""
    if not points:
        return _empty()
    if not any(p.batches > 0 or p.defect_rate is not None for p in points):
        return _empty()
    n = len(points)

    ml, mr, mt, mb = 66, 78, 52, 64
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    plot_w, plot_h = x1 - x0, y1 - y0
    slot = plot_w / n
    bar_w = max(3.0, slot * 0.5)

    max_batches = max(p.batches for p in points)
    rates = [p.defect_rate for p in points if p.defect_rate is not None]
    rmax: float | None = None
    if rates:
        rmax = max(rates)
        if rmax <= 0:
            rmax = 1.0

    def y_batches(value: float) -> float:
        return y1 - (value / max_batches) * plot_h

    def y_rate(value: float) -> float:
        return y1 - (value / (rmax or 1.0)) * plot_h

    legend: list[tuple[str, str, str]] = []
    if max_batches > 0:
        legend.append(("rect", _BAR, "Выпущено партий"))
    if rmax is not None:
        legend.append(("line", _LINE, "Доля брака, %"))

    parts = [
        _svg_open(
            "trend",
            width=width,
            height=height,
            title=f"Динамика по {n} месяцам: партии и доля брака",
            aria=f"Динамика по {n} месяцам: выпущено партий и доля брака",
        ),
        _legend(x0, 22, legend),
    ]

    # Сетка левой оси (партии): целочисленные деления; при нулевых партиях
    # ось и её подписи не строятся вовсе (легенда тоже — см. выше).
    if max_batches > 0:
        for value in _count_ticks(max_batches):
            y = y_batches(value)
            parts.append(_line(x0, y, x1, y, stroke=CHART_GRID, width=1))
            parts.append(_text(x0 - 8, y + 4, str(value), size=11, anchor="end"))

    # Правая ось доли брака.
    if rmax is not None:
        for i in range(5):
            value = rmax * i / 4
            y = y_rate(value)
            if max_batches <= 0:
                parts.append(_line(x0, y, x1, y, stroke=CHART_GRID, width=1))
            parts.append(_text(x1 + 8, y + 4, f"{value * 100:.1f} %", size=11))

    # Оси.
    parts.append(_line(x0, y0, x0, y1, stroke=CHART_AXIS_TEXT, width=1.2))
    parts.append(_line(x0, y1, x1, y1, stroke=CHART_AXIS_TEXT, width=1.2))

    # Столбцы партий.
    for i, point in enumerate(points):
        cx = x0 + i * slot + slot / 2
        if point.batches > 0:
            bar_h = (point.batches / max_batches) * plot_h
            parts.append(_rect(
                cx - bar_w / 2, y1 - bar_h, bar_w, bar_h, fill=_BAR,
                tooltip=f"{point.month}: партий {point.batches}",
            ))

    # Линия доли брака.
    rate_items = [(i, p) for i, p in enumerate(points)
                  if p.defect_rate is not None]
    if rate_items:
        polyline = " ".join(
            f"{_pt(x0 + i * slot + slot / 2)},{_pt(y_rate(p.defect_rate))}"
            for i, p in rate_items
        )
        parts.append(f'<polyline points="{polyline}" fill="none" '
                     f'stroke="{_esc(_LINE)}" stroke-width="2" />')
        for i, p in rate_items:
            cx = x0 + i * slot + slot / 2
            parts.append(_circle(cx, y_rate(p.defect_rate), 3.5, fill=_LINE,
                                 tooltip=f"{p.month}: доля брака "
                                         f"{_pct(p.defect_rate)}"))

    # Подписи месяцев (прореженные, чтобы не наезжали).
    step = max(1, -(-n // 12))
    for i, point in enumerate(points):
        if i % step:
            continue
        cx = x0 + i * slot + slot / 2
        parts.append(_text(cx, y1 + 18, point.month, size=11, anchor="middle"))

    parts.append("</svg>")
    return "\n".join(parts)


# --- SPC ------------------------------------------------------------------

def _spc_frame(*, prefix: str, width: int, height: int,
               title: str, aria: str, values: list[float],
               limit_lines: list[tuple[float, str, str, str | None]],
               points_xy: list[tuple[float, float, bool, str]],
               x_labels: list[tuple[float, str]],
               legend: list[tuple[str, str, str]]) -> str:
    """Общая рамка карты: масштаб, сетка, границы, точки, подписи."""
    ml, mr, mt, mb = 84, 96, 52, 64
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    plot_w, plot_h = x1 - x0, y1 - y0

    lo, hi = _scale(values)

    def y(value: float) -> float:
        return y1 - (value - lo) / (hi - lo) * plot_h

    parts = [_svg_open(prefix, width=width, height=height, title=title,
                       aria=aria), _legend(x0, 22, legend)]

    # Горизонтальная сетка с подписями значений.
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        gy = y(value)
        parts.append(_line(x0, gy, x1, gy, stroke=CHART_GRID, width=1))
        parts.append(_text(x0 - 8, gy + 4, _num(value, SPC_TICK_DP), size=10,
                           anchor="end"))

    # Оси.
    parts.append(_line(x0, y0, x0, y1, stroke=CHART_AXIS_TEXT, width=1.2))
    parts.append(_line(x0, y1, x1, y1, stroke=CHART_AXIS_TEXT, width=1.2))

    # Границы карты и подписи справа.
    for value, label, color, dash in limit_lines:
        ly = y(value)
        parts.append(_line(x0, ly, x1, ly, stroke=color, width=1.4, dash=dash))
        parts.append(_text(x1 + 6, ly + 4, f"{label} = {_num(value, SPC_VALUE_DP)}",
                           size=10))

    # Соединение точек.
    if len(points_xy) > 1:
        polyline = " ".join(f"{_pt(px)},{_pt(py)}" for px, py, _, _ in points_xy)
        parts.append(f'<polyline points="{polyline}" fill="none" '
                     f'stroke="{_esc(_CONNECT)}" stroke-width="1.2" />')

    # Точки: обычные и нарушенные.
    for px, py, violated, tip in points_xy:
        if violated:
            parts.append(_circle(px, py, 6, fill=_HILITE,
                                 stroke=CHART_AXIS_TEXT, stroke_width=1.5,
                                 tooltip=tip))
        else:
            parts.append(_circle(px, py, 3.5, fill=_BAR, tooltip=tip))

    # Подписи оси X (прореженные).
    step = max(1, -(-len(x_labels) // 24))
    for i, (px, label) in enumerate(x_labels):
        if i % step:
            continue
        parts.append(_text(px, y1 + 16, label, size=10, anchor="middle"))

    parts.append("</svg>")
    return "\n".join(parts)


def _limit_legend(
    limit_lines: Sequence[tuple[float, str, str, str | None]],
) -> list[tuple[str, str, str]]:
    """Пункты легенды из ТЕХ ЖЕ строк ``limit_lines``, что рисуют границы.

    Граница, которой нет на карте (``enough = False``), нет и в легенде;
    парные подписи (UCL+LCL, CL±2σ̂) схлопываются в один пункт.
    """
    collapse = {"UCL": "UCL / LCL", "LCL": "UCL / LCL",
                "CL+2σ̂": "±2σ̂", "CL−2σ̂": "±2σ̂"}
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for _, label, color, dash in limit_lines:
        key = collapse.get(label, label)
        if key in seen:
            continue
        seen.add(key)
        items.append(("dash" if dash else "line", color, key))
    return items


def _xbar_chart(result: SpcResult, *, width: int, height: int) -> str:
    """Карта контролируемых средних X̄ с точками и границами."""
    points = result.points
    parameter = result.parameter

    ml, mr, mt, mb = 84, 96, 52, 64
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    plot_w, plot_h = x1 - x0, y1 - y0
    slot = plot_w / len(points)

    values = [p.xbar for p in points]
    for v in (result.cl, result.ucl, result.lcl):
        if v is not None:
            values.append(v)
    if result.cl is not None and result.sigma is not None:
        values += [result.cl - 2 * result.sigma, result.cl + 2 * result.sigma]

    lo, hi = _scale(values)

    def y(value: float) -> float:
        return y1 - (value - lo) / (hi - lo) * plot_h

    limit_lines: list[tuple[float, str, str, str | None]] = []
    if result.cl is not None:
        limit_lines.append((result.cl, "CL", _LINE, None))
    if result.ucl is not None:
        limit_lines.append((result.ucl, "UCL", _ACCENT, None))
    if result.lcl is not None:
        limit_lines.append((result.lcl, "LCL", _ACCENT, None))
    if result.cl is not None and result.sigma is not None:
        limit_lines.append((result.cl + 2 * result.sigma, "CL+2σ̂", _DASH, "6 4"))
        limit_lines.append((result.cl - 2 * result.sigma, "CL−2σ̂", _DASH, "6 4"))

    points_xy: list[tuple[float, float, bool, str]] = []
    x_labels: list[tuple[float, str]] = []
    for i, p in enumerate(points):
        cx = x0 + i * slot + slot / 2
        tip = (f"№{p.subgroup_no} {p.label}: x̄ = {_num(p.xbar, SPC_VALUE_DP)}, "
               f"R = {_num(p.r, SPC_VALUE_DP)}")
        if p.violation_texts:
            tip += " — " + "; ".join(p.violation_texts)
        points_xy.append((cx, y(p.xbar), bool(p.violations), tip))
        x_labels.append((cx, str(p.subgroup_no)))

    legend: list[tuple[str, str, str]] = [("rect", _BAR, "x̄ подгрупп")]
    legend += _limit_legend(limit_lines)
    if any(p.violations for p in points):
        legend.append(("rect", _HILITE, "Нарушение"))

    return _spc_frame(
        prefix="spcx",
        width=width,
        height=height,
        title=f"Карта X̄: {parameter.name} ({parameter.code}) — {result.period}",
        aria=f"Карта контролируемых средних X̄ по параметру {parameter.name}, "
             f"период {result.period}",
        values=values,
        limit_lines=limit_lines,
        points_xy=points_xy,
        x_labels=x_labels,
        legend=legend,
    )


def _r_chart(result: SpcResult, *, width: int, height: int) -> str:
    """Карта размахов R."""
    points = result.points
    parameter = result.parameter

    ml, mr, mt, mb = 84, 96, 52, 64
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    plot_w, plot_h = x1 - x0, y1 - y0
    slot = plot_w / len(points)

    values = [p.r for p in points]
    for v in (result.r_cl, result.r_ucl, result.r_lcl):
        if v is not None:
            values.append(v)

    lo, hi = _scale(values)

    def y(value: float) -> float:
        return y1 - (value - lo) / (hi - lo) * plot_h

    limit_lines: list[tuple[float, str, str, str | None]] = []
    if result.r_cl is not None:
        limit_lines.append((result.r_cl, "CL", _LINE, None))
    if result.r_ucl is not None:
        limit_lines.append((result.r_ucl, "UCL", _ACCENT, None))
    if result.r_lcl is not None:
        limit_lines.append((result.r_lcl, "LCL", _ACCENT, None))

    points_xy: list[tuple[float, float, bool, str]] = []
    x_labels: list[tuple[float, str]] = []
    for i, p in enumerate(points):
        cx = x0 + i * slot + slot / 2
        tip = f"№{p.subgroup_no} {p.label}: R = {_num(p.r, SPC_VALUE_DP)}"
        points_xy.append((cx, y(p.r), False, tip))
        x_labels.append((cx, str(p.subgroup_no)))

    return _spc_frame(
        prefix="spcr",
        width=width,
        height=height,
        title=f"Карта размахов R: {parameter.name} ({parameter.code}) — "
              f"{result.period}",
        aria=f"Карта размахов R по параметру {parameter.name}, "
             f"период {result.period}",
        values=values,
        limit_lines=limit_lines,
        points_xy=points_xy,
        x_labels=x_labels,
        legend=[("rect", _BAR, "R подгрупп")] + _limit_legend(limit_lines),
    )


def spc_chart(result: SpcResult, *, width: int = 980, height: int = 430) -> str:
    """Карта X̄ (и карта R ниже) в обёртке ``div.spc-charts`` (§7.2)."""
    if not result.points:
        return _empty()
    with_r = (result.r_cl is not None and result.r_ucl is not None
              and height >= 360)
    height_x = int(height * 0.6) if with_r else height
    height_r = height - height_x

    parts = ['<div class="spc-charts">',
             _xbar_chart(result, width=width, height=height_x)]
    if with_r:
        parts.append(_r_chart(result, width=width, height=height_r))
    parts.append("</div>")
    return "\n".join(parts)


# --- Веса -----------------------------------------------------------------

def weight_bar(weights: IndexWeights, *, width: int = 520) -> str:
    """Четыре горизонтальных столбца весов критериев (§7.2)."""
    items: tuple[tuple[str, float, str], ...] = (
        ("Дефектность (defect)", weights.w_defect, CHART_SERIES[0]),
        ("Соответствие ТЗ (spec)", weights.w_spec, CHART_SERIES[1]),
        ("Повторяемость (repeat)", weights.w_repeat, CHART_SERIES[2]),
        ("Чистые партии (clean)", weights.w_clean, CHART_SERIES[3]),
    )
    if all(value == 0 for _, value, _ in items):
        return _empty()

    row_h = 46
    top = 24
    height = top + row_h * len(items) + 10
    bar_max = float(width - 90)

    parts = [
        _svg_open(
            "weight",
            width=width,
            height=height,
            title="Веса критериев индекса качества",
            aria="Веса четырёх критериев индекса качества",
        ),
    ]
    for i, (label, value, color) in enumerate(items):
        y = top + i * row_h
        parts.append(_text(10, y + 12, label, size=12))
        parts.append(_rect(10, y + 18, bar_max, 14, fill=CHART_GRID))
        filled = bar_max * max(0.0, min(1.0, value))
        if filled > 0:
            parts.append(_rect(10, y + 18, filled, 14, fill=color,
                               tooltip=f"{label}: {_num(value, 2)}"))
        parts.append(_text(10 + bar_max + 8, y + 30, _num(value, 2), size=12))

    parts.append("</svg>")
    return "\n".join(parts)
