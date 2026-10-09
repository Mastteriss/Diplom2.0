"""Анализ Парето по видам дефектов (docs/contract.md, §7.2).

Сортировка — по количеству по убыванию (стабильная, при равенстве — по коду
дефекта по возрастанию). ``share = qty / total`` и накопленная доля
``cumulative`` — доли 0..1 с округлением до ``ROUND_DP``. Класс ABC
определяется по ОКРУГЛЁННОЙ накопленной доле: ``cumulative ≤ 0.80`` → ``A``,
``cumulative ≤ 0.95`` → ``B``, иначе ``C``. ``top_share`` — доля первой
строки. Пустой вход (или нулевое суммарное количество) даёт пустой результат
с ``total_qty = 0``, ``top_share = 0.0`` и ``is_empty = True``.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from app.config import ROUND_DP
from app.domain.models import DefectCount, ParetoResult, ParetoRow


def compute_pareto(counts: Sequence[DefectCount], *, months: int) -> ParetoResult:
    """Диаграмма Парето за период в ``months`` месяцев (§7.2)."""
    as_of = date.today().isoformat()
    ordered = sorted(counts, key=lambda c: (-c.qty, c.code))
    total_qty = sum(c.qty for c in ordered)

    if not ordered or total_qty <= 0:
        return ParetoResult(
            rows=(),
            total_qty=0,
            top_share=0.0,
            as_of=as_of,
            months=months,
        )

    rows: list[ParetoRow] = []
    cumulative_raw = 0.0
    for count in ordered:
        cumulative_raw += count.qty / total_qty
        cumulative = round(cumulative_raw, ROUND_DP)
        if cumulative <= 0.80:
            abc = "A"
        elif cumulative <= 0.95:
            abc = "B"
        else:
            abc = "C"
        rows.append(
            ParetoRow(
                code=count.code,
                name=count.name,
                group_name=count.group_name,
                qty=count.qty,
                share=round(count.qty / total_qty, ROUND_DP),
                cumulative=cumulative,
                abc=abc,
            )
        )

    return ParetoResult(
        rows=tuple(rows),
        total_qty=total_qty,
        top_share=rows[0].share,
        as_of=as_of,
        months=months,
    )
