"""Индекс качества выпускаемой продукции (docs/contract.md, §3).

Формулы на одну позицию продукции за период ``months`` до месяца, в котором
лежит ``as_of`` (агрегаты построены слоем данных; параметры периода
фиксируются в контрактной сигнатуре §7.2):

* ``defect`` (C1) = clamp(1 − (rejected_qty / sample_qty) / defect_rate_cap,
  0, 1); критерий n/a, если ``sample_qty = 0``;
* ``spec`` (C2) = clamp(in_spec / with_specs, 0, 1); n/a при
  ``with_specs = 0``;
* ``repeat`` (C3) = clamp(1 − (repeat_qty / defect_qty) / repeat_rate_cap,
  0, 1); n/a при ``defect_qty = 0``;
* ``clean`` (C4) = clamp(batches_clean / batches_total, 0, 1); n/a при
  ``batches_total = 0``.

Веса нормируются ТОЛЬКО над доступными критериями:
``w'_i = w_i / Σ w_available``; ``raw_score = Σ w'_i · f_i`` с округлением до
``ROUND_DP``. Статусы (приоритет сверху вниз): ``no_data`` — ни одного
доступного критерия (score и raw_score = None); ``insufficient_data`` —
``sample_qty < min_sample`` (score = None, raw_score заполнен); ``ok`` —
иначе (score = raw_score, кламп в [0, 1]).

Поправка к §3 (решение координатора, применяется здесь): если доступные
критерии есть, но сумма их весов равна нулю, статус остаётся ``no_data``,
а ``notes`` — ровно «Сумма весов доступных критериев равна нулю».
"""

from __future__ import annotations

from datetime import date
from typing import Mapping

from app.config import ROUND_DP
from app.domain.models import IndexResult, IndexTerms, IndexWeights, ProductFacts

# Критерий -> поле IndexWeights (контракт §3: defect/spec/repeat/clean).
_CRITERION_FIELDS: tuple[tuple[str, str], ...] = (
    ("defect", "w_defect"),
    ("spec", "w_spec"),
    ("repeat", "w_repeat"),
    ("clean", "w_clean"),
)


def _clamp(value: float) -> float:
    """Ограничение значения диапазоном [0, 1]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _factors(f: ProductFacts, weights: IndexWeights) -> dict[str, float | None]:
    """Значения четырёх критериев; None — критерий недоступен (n/a)."""
    defect_rate = f.rejected_qty / f.sample_qty if f.sample_qty > 0 else None
    f_defect = None
    if defect_rate is not None:
        f_defect = _clamp(1.0 - defect_rate / weights.defect_rate_cap)

    f_spec = None
    if f.with_specs > 0:
        f_spec = _clamp(f.in_spec / f.with_specs)

    f_repeat = None
    if f.defect_qty > 0:
        repeat_rate = f.repeat_qty / f.defect_qty
        f_repeat = _clamp(1.0 - repeat_rate / weights.repeat_rate_cap)

    f_clean = None
    if f.batches_total > 0:
        f_clean = _clamp(f.batches_clean / f.batches_total)

    return {
        "defect": f_defect,
        "spec": f_spec,
        "repeat": f_repeat,
        "clean": f_clean,
    }


def compute_one(
    f: ProductFacts,
    weights: IndexWeights,
    *,
    as_of: date,
    months: int,
) -> IndexResult:
    """Индекс качества одной позиции продукции (§3).

    ``as_of`` и ``months`` фиксируют период, за который построены агрегаты
    ``f`` (сигнатура §7.2); арифметика опирается только на факты и веса.
    """
    factors = _factors(f, weights)
    available = tuple(key for key, _ in _CRITERION_FIELDS if factors[key] is not None)

    used_weights: dict[str, float] = {}
    raw_score: float | None = None

    if not available:
        status = "no_data"
        notes = "Нет данных за период: ни один критерий индекса недоступен."
    elif sum(
        getattr(weights, field)
        for key, field in _CRITERION_FIELDS
        if factors[key] is not None
    ) <= 0.0:
        # Поправка к §3 (решение координатора): нормировка невозможна
        # (Σ w_available = 0), но статус остаётся no_data, а notes —
        # ровно заданный текст (пользователь мог сохранить нулевые веса).
        status = "no_data"
        notes = "Сумма весов доступных критериев равна нулю"
    else:
        used_weights = weights.normalized(available)
        raw_score = round(
            _clamp(sum(used_weights[key] * factors[key] for key in available)),
            ROUND_DP,
        )
        if f.sample_qty < weights.min_sample:
            status = "insufficient_data"
            notes = (
                f"Недостаточно данных: выборка {f.sample_qty} меньше "
                f"минимальной ({weights.min_sample}) — балл не рассчитан."
            )
        else:
            status = "ok"
            notes = (
                f"Расчёт выполнен по {len(available)} из 4 критериев; "
                "веса перенормированы над доступными."
            )

    score = raw_score if status == "ok" else None
    defect_rate = (
        f.rejected_qty / f.sample_qty if f.sample_qty > 0 else None
    )

    return IndexResult(
        product_id=f.product_id,
        sku=f.sku,
        name=f.name,
        status=status,
        score=score,
        raw_score=raw_score,
        terms=IndexTerms(
            f_defect=factors["defect"],
            f_spec=factors["spec"],
            f_repeat=factors["repeat"],
            f_clean=factors["clean"],
            used_weights=used_weights,
        ),
        notes=notes,
        sample_qty=f.sample_qty,
        defect_rate=defect_rate,
    )


def compute_all(
    facts: Mapping[int, ProductFacts],
    weights: IndexWeights,
    *,
    as_of: date,
    months: int,
) -> list[IndexResult]:
    """Индекс качества по всем позициям (§7.2).

    Сортировка: score по убыванию, записи со ``score = None`` — в конец,
    при равенстве score — по возрастанию sku.
    """
    results = [
        compute_one(f, weights, as_of=as_of, months=months)
        for f in facts.values()
    ]
    results.sort(
        key=lambda r: (
            r.score is None,
            -(r.score if r.score is not None else 0.0),
            r.sku,
        )
    )
    return results
