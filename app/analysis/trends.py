"""Динамика показателей по месяцам (docs/contract.md, §7.2).

Для каждого месяца ``"YYYY-MM"`` из возрастающей последовательности
``months_seq`` строится одна точка: месяцы без фактов получают
``batches = 0``, ``defect_rate = None``, ``accepted_share = None``.
Формулы: ``defect_rate = rejected_qty / sample_qty`` (при ``sample_qty = 0``
— ``None``); ``accepted_share = accepted_batches / batches`` (при
``batches = 0`` — ``None``).
"""

from __future__ import annotations

from typing import Sequence

from app.domain.models import MonthlyFacts, TrendPoint


def compute_trends(
    months_seq: Sequence[str],
    facts: Sequence[MonthlyFacts],
) -> list[TrendPoint]:
    """Точки динамики: ровно по одной на каждый месяц из ``months_seq``
    в заданном порядке (§7.2)."""
    by_month: dict[str, MonthlyFacts] = {}
    for item in facts:
        by_month.setdefault(item.month, item)

    points: list[TrendPoint] = []
    for month in months_seq:
        item = by_month.get(month)
        if item is None:
            points.append(
                TrendPoint(month=month, batches=0, defect_rate=None,
                           accepted_share=None)
            )
            continue
        defect_rate = (
            item.rejected_qty / item.sample_qty if item.sample_qty > 0 else None
        )
        accepted_share = (
            item.accepted_batches / item.batches if item.batches > 0 else None
        )
        points.append(
            TrendPoint(
                month=month,
                batches=item.batches,
                defect_rate=defect_rate,
                accepted_share=accepted_share,
            )
        )
    return points
