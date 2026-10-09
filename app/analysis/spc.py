"""Статистическое управление процессом — контрольные карты (§4 контракта).

Подгруппа = все измерения одного акта ОТК по одному параметру. Подгруппы с
``n`` вне ``SPC_N_MIN..SPC_N_MAX`` (2..10) исключаются и учитываются в
``excluded_subgroups``. Модальная ``n`` (при равенстве частот — меньшая)
задаёт константы ``d2/D3/D4/A2`` (ГОСТ Р ИСО 7870-2 / AIAG SPC) для всех
подгрупп. Формулы: ``R̄`` — среднее размахов, ``X̿`` — среднее средних;
карта X̄: ``CL = X̿``, ``UCL/LCL = X̿ ± A2·R̄``; карта R: ``CL_r = R̄``,
``UCL_r = D4·R̄``, ``LCL_r = D3·R̄`` (при ``D3 = 0`` — 0.0);
``σ̂ = R̄ / d2``. Меньше ``SPC_MIN_SUBGROUPS`` включённых подгрупп →
``enough = False``, все границы и Cp/Cpk — ``None`` (НЕ 0) и русское
предупреждение в ``warnings``. Cp/Cpk: при обоих допусках
``Cp = (usl − lsl)/(6σ̂)`` и ``Cpk = min((usl − X̿)/(3σ̂), (X̿ − lsl)/(3σ̂))``
(``cpk_side = "two"``); при одном — ``Cpu``/``Cpl``
(``cpk_side = "upper"``/``"lower"``); нет допусков — оба ``None``. При
нулевом размахе (``R̄ = 0``) Cp/Cpk — ``None`` и предупреждение
«Нулевой размах: Cp/Cpk не рассчитаны» в ``warnings``.
Нарушения (только при ``enough``): ``beyond_limit`` — ``x̄`` вне UCL/LCL;
``zone_a_2_of_3`` — среди трёх последовательных подгрупп не менее двух на
одной стороне от ``CL`` с ``|x̄ − CL| ≥ 2·σ̂`` (относится к третьей
подгруппе тройки). Округление: значения карт — ``SPC_VALUE_DP``,
Cp/Cpk — ``SPC_CPDP``.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from app.config import (
    SPC_CPDP,
    SPC_MIN_SUBGROUPS,
    SPC_N_MAX,
    SPC_N_MIN,
    SPC_VALUE_DP,
)
from app.domain.models import ParameterSpec, SpcPoint, SpcResult, SpcSubgroup

# Таблица констант (§4.1), n = 2..10: (d2, D3, D4, A2).
_CONSTANTS: tuple[tuple[float, float, float, float], ...] = (
    (1.128, 0.0, 3.267, 1.880),
    (1.693, 0.0, 2.574, 1.023),
    (2.059, 0.0, 2.282, 0.729),
    (2.326, 0.0, 2.114, 0.577),
    (2.534, 0.0, 2.004, 0.483),
    (2.704, 0.076, 1.924, 0.419),
    (2.847, 0.136, 1.864, 0.373),
    (2.970, 0.184, 1.816, 0.337),
    (3.078, 0.223, 1.777, 0.308),
)


def compute_spc(
    subgroups: Sequence[SpcSubgroup],
    parameter: ParameterSpec,
    *,
    period: str,
) -> SpcResult:
    """Контрольные карты X̄ и R по подгруппам за ``period`` (§4)."""
    included: list[SpcSubgroup] = []
    excluded_subgroups = 0
    for sg in subgroups:
        if SPC_N_MIN <= sg.n <= SPC_N_MAX:
            included.append(sg)
        else:
            excluded_subgroups += 1

    enough = len(included) >= SPC_MIN_SUBGROUPS
    warnings: list[str] = []
    if not enough:
        warnings.append(
            "Недостаточно подгрупп для расчётных границ "
            f"({len(included)} < {SPC_MIN_SUBGROUPS})"
        )

    modal_n: int | None = None
    if included:
        frequencies = Counter(sg.n for sg in included)
        top = max(frequencies.values())
        modal_n = min(n for n, cnt in frequencies.items() if cnt == top)

    cl = ucl = lcl = None
    r_cl = r_ucl = r_lcl = None
    sigma = None
    cp = cpk = None
    cpk_side: str | None = None

    r_bar = 0.0
    xbar_bar = 0.0
    if included:
        r_bar = sum(sg.r for sg in included) / len(included)
        xbar_bar = sum(sg.xbar for sg in included) / len(included)

    d2 = d3 = d4 = a2 = 0.0
    sigma_hat = 0.0
    if enough and modal_n is not None:
        d2, d3, d4, a2 = _CONSTANTS[modal_n - 2]
        cl = round(xbar_bar, SPC_VALUE_DP)
        ucl = round(xbar_bar + a2 * r_bar, SPC_VALUE_DP)
        lcl = round(xbar_bar - a2 * r_bar, SPC_VALUE_DP)
        r_cl = round(r_bar, SPC_VALUE_DP)
        r_ucl = round(d4 * r_bar, SPC_VALUE_DP)
        r_lcl = 0.0 if d3 == 0.0 else round(d3 * r_bar, SPC_VALUE_DP)
        sigma_hat = r_bar / d2
        sigma = round(sigma_hat, SPC_VALUE_DP)

        lsl, usl = parameter.lsl, parameter.usl
        if sigma_hat > 0.0:
            if lsl is not None and usl is not None:
                cp = round((usl - lsl) / (6.0 * sigma_hat), SPC_CPDP)
                cpu = (usl - xbar_bar) / (3.0 * sigma_hat)
                cpl = (xbar_bar - lsl) / (3.0 * sigma_hat)
                cpk = round(min(cpu, cpl), SPC_CPDP)
                cpk_side = "two"
            elif usl is not None:
                cpk = round((usl - xbar_bar) / (3.0 * sigma_hat), SPC_CPDP)
                cpk_side = "upper"
            elif lsl is not None:
                cpk = round((xbar_bar - lsl) / (3.0 * sigma_hat), SPC_CPDP)
                cpk_side = "lower"
        elif lsl is not None or usl is not None:
            # Вырожденный случай: R̄ = 0 → σ̂ = 0, Cp/Cpk не определены,
            # а UCL = LCL = CL. Без предупреждения пользователь видит «—»
            # и «вне границ» без объяснения (числа не выдумываем).
            warnings.append("Нулевой размах: Cp/Cpk не рассчитаны")

    # Нарушения считаются ТОЛЬКО при enough (§4.3), по неокруглённым
    # величинам; в точки попадают значения, округлённые до SPC_VALUE_DP.
    violation_codes: list[tuple[str, ...]] = [()] * len(included)
    violation_texts: list[tuple[str, ...]] = [()] * len(included)
    if enough and included:
        codes: list[list[str]] = [[] for _ in included]
        texts: list[list[str]] = [[] for _ in included]

        cl_raw = xbar_bar
        ucl_raw = xbar_bar + a2 * r_bar
        lcl_raw = xbar_bar - a2 * r_bar
        for i, sg in enumerate(included):
            if sg.xbar > ucl_raw or sg.xbar < lcl_raw:
                codes[i].append("beyond_limit")
                texts[i].append(
                    "Значение вне контрольных границ: "
                    f"x̄ = {sg.xbar:.{SPC_VALUE_DP}f}, "
                    f"UCL = {ucl_raw:.{SPC_VALUE_DP}f}, "
                    f"LCL = {lcl_raw:.{SPC_VALUE_DP}f}"
                )

        if sigma_hat > 0.0:
            two_sigma = 2.0 * sigma_hat
            for i in range(2, len(included)):
                window = included[i - 2:i + 1]
                above = sum(
                    1 for sg in window
                    if sg.xbar > cl_raw and (sg.xbar - cl_raw) >= two_sigma
                )
                below = sum(
                    1 for sg in window
                    if sg.xbar < cl_raw and (cl_raw - sg.xbar) >= two_sigma
                )
                if above >= 2:
                    side = "выше центра"
                elif below >= 2:
                    side = "ниже центра"
                else:
                    continue
                codes[i].append("zone_a_2_of_3")
                texts[i].append(
                    "Зона A: не менее двух из трёх последовательных подгрупп "
                    f"{side} от CL с |x̄ − CL| ≥ 2σ̂"
                )

        violation_codes = [tuple(items) for items in codes]
        violation_texts = [tuple(items) for items in texts]

    points = [
        SpcPoint(
            subgroup_no=sg.subgroup_no,
            label=sg.label,
            n=sg.n,
            xbar=round(sg.xbar, SPC_VALUE_DP),
            r=round(sg.r, SPC_VALUE_DP),
            violations=violation_codes[i],
            violation_texts=violation_texts[i],
        )
        for i, sg in enumerate(included)
    ]

    return SpcResult(
        parameter=parameter,
        points=tuple(points),
        excluded_subgroups=excluded_subgroups,
        cl=cl,
        ucl=ucl,
        lcl=lcl,
        r_cl=r_cl,
        r_ucl=r_ucl,
        r_lcl=r_lcl,
        sigma=sigma,
        cp=cp,
        cpk=cpk,
        cpk_side=cpk_side,
        modal_n=modal_n,
        enough=enough,
        warnings=tuple(warnings),
        period=period,
    )
