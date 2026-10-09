"""Pure unit tests for layer B2 (analysis), per docs/contract.md §3, §4, §7.2.

No database, no network, no clock: inputs are built by hand from the frozen
dataclasses in app/domain/models.py and the public API in app/analysis/*.py.
Every assertion targets observable behaviour (returned values, flags, emitted
SVG text) so the tests can actually fail on a real defect.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.analysis import charts, pareto, quality_index, spc, trends
from app.config import (
    CHART_EMPTY_TEXT,
    ROUND_DP,
    SPC_CPDP,
    SPC_VALUE_DP,
)
from app.domain.models import (
    DefectCount,
    IndexWeights,
    MonthlyFacts,
    ParameterSpec,
    ParetoResult,
    ProductFacts,
    SpcResult,
    SpcSubgroup,
    TrendPoint,
)

AS_OF = date(2026, 9, 15)
MONTHS = 12


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

def _facts(**overrides: object) -> ProductFacts:
    """ProductFacts with all criteria n/a unless overridden."""
    base: dict[str, object] = dict(
        product_id=1,
        sku="SKU",
        name="Позиция",
        category="Категория",
        sample_qty=0,
        rejected_qty=0,
        defect_qty=0,
        repeat_qty=0,
        in_spec=0,
        with_specs=0,
        batches_total=0,
        batches_clean=0,
    )
    base.update(overrides)
    return ProductFacts(**base)


def _dc(code: str, qty: int, *, name: str = "дефект",
        group: str = "Прочее", severity: int = 3) -> DefectCount:
    return DefectCount(code=code, name=name, group_name=group,
                       severity=severity, qty=qty)


def _sg(no: int, label: str, values: tuple[float, ...]) -> SpcSubgroup:
    return SpcSubgroup(subgroup_no=no, label=label, values=tuple(values))


def _param(*, lsl: float | None = None, usl: float | None = None,
           code: str = "P", name: str = "Параметр") -> ParameterSpec:
    return ParameterSpec(id=1, code=code, name=name, unit="мм", target=None,
                         lsl=lsl, usl=usl, is_critical=True)


# n=2..10 constants table exactly as frozen in docs/contract.md §4.1.
_CONSTANTS: dict[int, tuple[float, float, float, float]] = {
    2: (1.128, 0.0, 3.267, 1.880),
    3: (1.693, 0.0, 2.574, 1.023),
    4: (2.059, 0.0, 2.282, 0.729),
    5: (2.326, 0.0, 2.114, 0.577),
    6: (2.534, 0.0, 2.004, 0.483),
    7: (2.704, 0.076, 1.924, 0.419),
    8: (2.847, 0.136, 1.864, 0.373),
    9: (2.970, 0.184, 1.816, 0.337),
    10: (3.078, 0.223, 1.777, 0.308),
}


def _uniform_subgroups(n: int) -> list[SpcSubgroup]:
    """10 subgroups of size ``n``; subgroup i has range i (i = 1..10).

    Values are ``(10, 10, ..., 10 + range)`` so ``min = 10``, ``max = 10+range``
    and therefore ``R̄ = 5.5`` and ``x̄ = 10 + range / n``.
    """
    return [
        _sg(i, f"B{i}", (10.0,) * (n - 1) + (10.0 + i,))
        for i in range(1, 11)
    ]


def _spc_result(*, parameter: ParameterSpec | None = None,
                label: str = "B") -> SpcResult:
    """A valid, `enough=True` SPC result on the n=2, ranges 1..10 fixture."""
    subs = [_sg(i, f"{label}{i}", (10.0, 10.0 + i)) for i in range(1, 11)]
    return spc.compute_spc(subs, parameter or _param(), period="2026-01..2026-12")


# ==========================================================================
# §3 — quality index
# ==========================================================================

def test_compute_one_c1_formula_defect_rate_and_all_other_criteria_na() -> None:
    w = IndexWeights(min_sample=0)
    r = quality_index.compute_one(
        _facts(sample_qty=100, rejected_qty=5), w, as_of=AS_OF, months=MONTHS
    )
    assert r.status == "ok"
    assert r.defect_rate == pytest.approx(0.05)
    assert r.terms.f_defect == pytest.approx(1.0 - 0.05 / w.defect_rate_cap)
    assert r.terms.f_spec is None
    assert r.terms.f_repeat is None
    assert r.terms.f_clean is None
    assert r.score == pytest.approx(0.5)
    assert r.raw_score == pytest.approx(0.5)


def test_compute_one_c1_clamps_low_and_high() -> None:
    w = IndexWeights(min_sample=0)
    over = _facts(sample_qty=100, rejected_qty=20)   # rate 0.20 > cap 0.10
    clean = _facts(sample_qty=100, rejected_qty=0)   # rate 0.0
    r_over = quality_index.compute_one(over, w, as_of=AS_OF, months=MONTHS)
    r_clean = quality_index.compute_one(clean, w, as_of=AS_OF, months=MONTHS)
    assert r_over.terms.f_defect == 0.0
    assert r_clean.terms.f_defect == 1.0


def test_compute_one_c2_formula() -> None:
    w = IndexWeights(min_sample=0)
    # C1 n/a (sample_qty=0); repeat/clean n/a.
    r = quality_index.compute_one(
        _facts(sample_qty=0, with_specs=10, in_spec=7), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.status == "ok"
    assert r.terms.f_defect is None
    assert r.terms.f_spec == pytest.approx(0.7)
    assert r.raw_score == pytest.approx(0.7)


def test_compute_one_c3_formula() -> None:
    w = IndexWeights(min_sample=0)
    # defect_qty=10, repeat_qty=1 -> rate 0.1 over cap 0.2 -> 0.5
    r = quality_index.compute_one(
        _facts(sample_qty=0, defect_qty=10, repeat_qty=1), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.status == "ok"
    assert r.terms.f_repeat == pytest.approx(0.5)
    assert r.raw_score == pytest.approx(0.5)


def test_compute_one_c4_formula() -> None:
    w = IndexWeights(min_sample=0)
    r = quality_index.compute_one(
        _facts(sample_qty=0, batches_total=4, batches_clean=3), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.status == "ok"
    assert r.terms.f_clean == pytest.approx(0.75)
    assert r.raw_score == pytest.approx(0.75)


def test_compute_one_na_guards_for_each_criterion() -> None:
    w = IndexWeights(min_sample=0)
    # sample_qty = 0 -> defect n/a (spec still available to keep status ok)
    r = quality_index.compute_one(
        _facts(sample_qty=0, rejected_qty=0, with_specs=10, in_spec=5), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.terms.f_defect is None
    assert r.defect_rate is None
    assert r.terms.f_spec == pytest.approx(0.5)

    # with_specs = 0 -> spec n/a; defect available via sample_qty>0
    r = quality_index.compute_one(
        _facts(sample_qty=10, rejected_qty=0, with_specs=0), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.terms.f_spec is None
    assert r.terms.f_defect == pytest.approx(1.0)

    # defect_qty = 0 -> repeat n/a; clean available
    r = quality_index.compute_one(
        _facts(sample_qty=0, defect_qty=0, batches_total=2, batches_clean=1), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.terms.f_repeat is None
    assert r.terms.f_clean == pytest.approx(0.5)

    # batches_total = 0 -> clean n/a; defect available
    r = quality_index.compute_one(
        _facts(sample_qty=10, rejected_qty=0, batches_total=0), w,
        as_of=AS_OF, months=MONTHS,
    )
    assert r.terms.f_clean is None
    assert r.terms.f_defect == pytest.approx(1.0)


def test_compute_one_renormalizes_only_over_available_criteria() -> None:
    w = IndexWeights(min_sample=0)  # 0.40 / 0.30 / 0.15 / 0.15
    # defect available (f=1.0), clean available (f=0.5); spec & repeat n/a
    f = _facts(sample_qty=100, rejected_qty=0, batches_total=2, batches_clean=1)
    r = quality_index.compute_one(f, w, as_of=AS_OF, months=MONTHS)

    assert set(r.terms.used_weights) == {"defect", "clean"}
    assert r.terms.used_weights["defect"] == pytest.approx(0.40 / 0.55)
    assert r.terms.used_weights["clean"] == pytest.approx(0.15 / 0.55)
    assert sum(r.terms.used_weights.values()) == pytest.approx(1.0)

    expected = (0.40 * 1.0 + 0.15 * 0.5) / 0.55
    assert r.raw_score == pytest.approx(round(expected, ROUND_DP))
    assert r.raw_score == pytest.approx(0.863636)
    assert r.score == pytest.approx(r.raw_score)


def test_compute_one_raw_score_rounded_to_round_dp() -> None:
    w = IndexWeights(min_sample=0, defect_rate_cap=0.35)
    # C1 = 1 - (2/7)/0.35 = 0.183673469... -> rounded to 6 dp
    f = _facts(sample_qty=7, rejected_qty=2)
    r = quality_index.compute_one(f, w, as_of=AS_OF, months=MONTHS)
    unrounded = 1.0 - (2 / 7) / 0.35
    assert r.raw_score == pytest.approx(round(unrounded, ROUND_DP))
    assert r.raw_score == pytest.approx(0.183673)
    assert r.raw_score != unrounded


def test_compute_one_insufficient_data_keeps_raw_score() -> None:
    w = IndexWeights()  # min_sample = 10
    f = _facts(sample_qty=5, rejected_qty=0)  # C1 available and = 1.0
    r = quality_index.compute_one(f, w, as_of=AS_OF, months=MONTHS)
    assert r.status == "insufficient_data"
    assert r.score is None
    assert r.raw_score == pytest.approx(1.0)
    assert "Недостаточно данных" in r.notes


def test_compute_one_no_data_when_no_criterion_available() -> None:
    w = IndexWeights()
    f = _facts(sample_qty=0, with_specs=0, defect_qty=0, batches_total=0)
    r = quality_index.compute_one(f, w, as_of=AS_OF, months=MONTHS)
    assert r.status == "no_data"
    assert r.score is None
    assert r.raw_score is None
    assert r.terms.used_weights == {}
    assert r.terms.f_defect is None


def test_compute_one_no_data_takes_priority_over_insufficient() -> None:
    # sample_qty < min_sample AND no available criterion -> no_data, not
    # insufficient_data (§3 status order).
    r = quality_index.compute_one(
        _facts(sample_qty=0), IndexWeights(), as_of=AS_OF, months=MONTHS
    )
    assert r.status == "no_data"
    assert r.score is None


def test_compute_all_sorts_score_desc_none_last_then_sku() -> None:
    w = IndexWeights()
    facts = {
        1: _facts(product_id=1, sku="BBB", sample_qty=100, rejected_qty=5),
        2: _facts(product_id=2, sku="AAA", sample_qty=100, rejected_qty=0),
        3: _facts(product_id=3, sku="CCC", sample_qty=100, rejected_qty=10),
        4: _facts(product_id=4, sku="DDD", sample_qty=0),          # no_data
        5: _facts(product_id=5, sku="EEE", sample_qty=5),          # insufficient
    }
    results = quality_index.compute_all(facts, w, as_of=AS_OF, months=MONTHS)
    assert [r.sku for r in results] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert [r.status for r in results] == [
        "ok", "ok", "ok", "no_data", "insufficient_data",
    ]
    scores = [r.score for r in results]
    assert scores[0] > scores[1] > scores[2]
    assert scores[3] is None and scores[4] is None


def test_compute_all_ties_break_by_sku_ascending() -> None:
    facts = {
        1: _facts(product_id=1, sku="ZZZ", sample_qty=100, rejected_qty=0),
        2: _facts(product_id=2, sku="AAA", sample_qty=100, rejected_qty=0),
    }
    results = quality_index.compute_all(
        facts, IndexWeights(), as_of=AS_OF, months=MONTHS
    )
    assert [r.sku for r in results] == ["AAA", "ZZZ"]
    assert results[0].score == results[1].score == pytest.approx(1.0)


def test_compute_all_empty_mapping() -> None:
    assert quality_index.compute_all(
        {}, IndexWeights(), as_of=AS_OF, months=MONTHS
    ) == []


# ==========================================================================
# §7.2 — Pareto
# ==========================================================================

def test_compute_pareto_abc_boundaries_and_shares() -> None:
    counts = [_dc("D1", 50), _dc("D2", 30), _dc("D3", 15), _dc("D4", 5)]
    res = pareto.compute_pareto(counts, months=MONTHS)

    assert [r.code for r in res.rows] == ["D1", "D2", "D3", "D4"]
    assert [r.qty for r in res.rows] == [50, 30, 15, 5]
    assert [r.share for r in res.rows] == [0.5, 0.3, 0.15, 0.05]
    # cumulative exactly 0.80 -> A, exactly 0.95 -> B, else C
    assert [r.cumulative for r in res.rows] == [0.5, 0.8, 0.95, 1.0]
    assert [r.abc for r in res.rows] == ["A", "A", "B", "C"]
    assert res.total_qty == 100
    assert res.top_share == 0.5
    assert res.months == MONTHS
    assert res.is_empty is False


def test_compute_pareto_sorts_by_qty_desc_then_code_asc() -> None:
    counts = [_dc("B", 5), _dc("A", 5), _dc("C", 10)]
    res = pareto.compute_pareto(counts, months=3)
    assert [r.code for r in res.rows] == ["C", "A", "B"]


def test_compute_pareto_rounds_share_and_cumulative_to_round_dp() -> None:
    counts = [_dc("A", 1), _dc("B", 1), _dc("C", 1)]
    res = pareto.compute_pareto(counts, months=1)
    assert res.rows[0].share == round(1 / 3, ROUND_DP)
    assert res.rows[0].share == 0.333333
    assert res.rows[1].cumulative == round(2 / 3, ROUND_DP)
    assert res.rows[1].cumulative == 0.666667
    assert res.rows[2].cumulative == 1.0
    assert res.top_share == res.rows[0].share


def test_compute_pareto_empty_input() -> None:
    res = pareto.compute_pareto([], months=6)
    assert res.rows == ()
    assert res.total_qty == 0
    assert res.top_share == 0.0
    assert res.months == 6
    assert res.is_empty is True


def test_compute_pareto_zero_total_qty_is_empty() -> None:
    res = pareto.compute_pareto([_dc("A", 0)], months=6)
    assert res.rows == ()
    assert res.total_qty == 0
    assert res.is_empty is True


# ==========================================================================
# §7.2 — Trends
# ==========================================================================

def test_compute_trends_missing_month_yields_none_defect_rate() -> None:
    facts = [
        MonthlyFacts(
            month="2026-02", batches=4, inspections=1, sample_qty=100,
            rejected_qty=10, defect_rate=0.99, accepted_batches=3,
        )
    ]
    points = trends.compute_trends(["2026-01", "2026-02", "2026-03"], facts)
    assert [p.month for p in points] == ["2026-01", "2026-02", "2026-03"]

    assert points[0].batches == 0
    assert points[0].defect_rate is None
    assert points[0].accepted_share is None

    # formula recomputed from qty, not copied from MonthlyFacts.defect_rate
    assert points[1].batches == 4
    assert points[1].defect_rate == pytest.approx(0.1)
    assert points[1].accepted_share == pytest.approx(0.75)

    assert points[2].batches == 0
    assert points[2].defect_rate is None


def test_compute_trends_zero_denominators_yield_none() -> None:
    facts = [
        MonthlyFacts(
            month="2026-01", batches=0, inspections=0, sample_qty=0,
            rejected_qty=0, defect_rate=None, accepted_batches=0,
        )
    ]
    points = trends.compute_trends(["2026-01"], facts)
    assert points[0].batches == 0
    assert points[0].defect_rate is None
    assert points[0].accepted_share is None


def test_compute_trends_empty_sequence() -> None:
    assert trends.compute_trends([], []) == []


# ==========================================================================
# §4 — SPC
# ==========================================================================

def test_compute_spc_constants_table_applies_for_n() -> None:
    """n=2,5,7,10: sigma, R limits and X̄ limits must match §4.1/§4.2."""
    for n in (2, 5, 7, 10):
        res = spc.compute_spc(_uniform_subgroups(n), _param(), period="P")
        assert res.modal_n == n
        assert res.enough is True
        assert res.excluded_subgroups == 0
        assert len(res.points) == 10

        d2, d3, d4, a2 = _CONSTANTS[n]
        r_bar = 5.5
        xbar_bar = 10.0 + r_bar / n

        assert res.sigma == pytest.approx(round(r_bar / d2, SPC_VALUE_DP))
        assert res.r_ucl == pytest.approx(round(d4 * r_bar, SPC_VALUE_DP))
        assert res.r_lcl == pytest.approx(
            0.0 if d3 == 0.0 else round(d3 * r_bar, SPC_VALUE_DP)
        )
        assert res.cl == pytest.approx(round(xbar_bar, SPC_VALUE_DP))
        assert res.ucl == pytest.approx(
            round(xbar_bar + a2 * r_bar, SPC_VALUE_DP)
        )
        assert res.lcl == pytest.approx(
            round(xbar_bar - a2 * r_bar, SPC_VALUE_DP)
        )

    # Concrete spot values for n=2 (D3 = 0 -> LCL_r exactly 0.0).
    res2 = spc.compute_spc(_uniform_subgroups(2), _param(), period="P")
    assert res2.sigma == 4.8759
    assert res2.r_ucl == 17.9685
    assert res2.r_lcl == 0.0


def test_compute_spc_modal_n_tie_uses_smaller_for_all_subgroups() -> None:
    subs = []
    for i in range(1, 7):                     # 6 x n=5
        subs.append(_sg(i, f"B{i}", (10.0, 10.0, 10.0, 10.0, 12.0)))
    for i in range(7, 13):                    # 6 x n=7
        subs.append(_sg(i, f"B{i}", (10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 12.0)))

    res = spc.compute_spc(subs, _param(), period="P")
    assert res.modal_n == 5                   # tie -> smaller
    assert res.enough is True
    assert {p.n for p in res.points} == {5, 7}

    r_bar = 2.0
    assert res.sigma == pytest.approx(round(r_bar / 2.326, SPC_VALUE_DP))
    assert res.sigma == 0.8598                # n=5 d2, not n=7 (0.7396)
    assert res.r_ucl == pytest.approx(round(2.114 * r_bar, SPC_VALUE_DP))
    assert res.r_ucl == 4.228                 # n=5 D4, not n=7 (3.848)
    assert res.r_lcl == 0.0


def test_compute_spc_excludes_n_outside_bounds() -> None:
    subs = [_sg(i, f"B{i}", (1.0, 2.0)) for i in range(1, 9)]  # 8 x n=2
    subs.append(_sg(9, "B9", (1.0,)))                          # n=1 excluded
    subs.append(_sg(10, "B10", tuple(float(x) for x in range(11))))  # n=11
    subs.append(_sg(11, "B11", tuple(float(x) for x in range(10))))  # n=10 ok
    subs.append(_sg(12, "B12", (1.0, 2.0)))                    # n=2 ok

    res = spc.compute_spc(subs, _param(), period="P")
    assert res.excluded_subgroups == 2
    assert len(res.points) == 10
    assert {p.n for p in res.points} == {2, 10}
    assert all(p.n not in (1, 11) for p in res.points)


def test_compute_spc_label_is_batch_no_and_subgroup_no_preserved() -> None:
    res = spc.compute_spc(_uniform_subgroups(2), _param(), period="P")
    assert res.points[0].label == "B1"
    assert res.points[0].subgroup_no == 1
    assert res.points[9].label == "B10"
    assert res.points[9].subgroup_no == 10


def test_compute_spc_not_enough_subgroups_no_limits_and_warning() -> None:
    subs = [_sg(i, f"B{i}", (10.0, 12.0)) for i in range(1, 4)]  # 3 < 10
    res = spc.compute_spc(subs, _param(lsl=0.0, usl=20.0), period="P")

    assert res.enough is False
    assert len(res.points) == 3          # included but no limits computed
    assert res.cl is None
    assert res.ucl is None
    assert res.lcl is None
    assert res.r_cl is None
    assert res.r_ucl is None
    assert res.r_lcl is None
    assert res.sigma is None
    assert res.cp is None
    assert res.cpk is None
    assert res.cpk_side is None
    assert res.modal_n == 2
    assert res.warnings == (
        "Недостаточно подгрупп для расчётных границ (3 < 10)",
    )


def test_compute_spc_cp_cpk_two_sided_limits() -> None:
    res = spc.compute_spc(
        _uniform_subgroups(2), _param(lsl=0.0, usl=50.0), period="P"
    )
    r_bar, xbar_bar = 5.5, 12.75
    sigma = r_bar / _CONSTANTS[2][0]

    assert res.sigma == pytest.approx(round(sigma, SPC_VALUE_DP))
    assert res.sigma == 4.8759
    assert res.cp == pytest.approx(round((50.0 - 0.0) / (6 * sigma), SPC_CPDP))
    assert res.cp == 1.709
    cpu = (50.0 - xbar_bar) / (3 * sigma)
    cpl = (xbar_bar - 0.0) / (3 * sigma)
    assert res.cpk == pytest.approx(round(min(cpu, cpl), SPC_CPDP))
    assert res.cpk == 0.872
    assert res.cpk_side == "two"


def test_compute_spc_cpu_cpk_upper_one_limit() -> None:
    res = spc.compute_spc(
        _uniform_subgroups(2), _param(lsl=None, usl=50.0), period="P"
    )
    sigma = 5.5 / _CONSTANTS[2][0]
    assert res.cp is None
    assert res.cpk == pytest.approx(round((50.0 - 12.75) / (3 * sigma), SPC_CPDP))
    assert res.cpk == 2.547
    assert res.cpk_side == "upper"


def test_compute_spc_cpl_cpk_lower_one_limit() -> None:
    res = spc.compute_spc(
        _uniform_subgroups(2), _param(lsl=0.0, usl=None), period="P"
    )
    sigma = 5.5 / _CONSTANTS[2][0]
    assert res.cp is None
    assert res.cpk == pytest.approx(round((12.75 - 0.0) / (3 * sigma), SPC_CPDP))
    assert res.cpk == 0.872
    assert res.cpk_side == "lower"


def test_compute_spc_no_tolerances_cp_cpk_are_none_not_zero() -> None:
    res = spc.compute_spc(
        _uniform_subgroups(2), _param(lsl=None, usl=None), period="P"
    )
    assert res.cp is None
    assert res.cpk is None
    assert res.cpk_side is None
    assert res.cp != 0
    assert res.cpk != 0


def test_compute_spc_zero_range_warns_and_cp_cpk_none() -> None:
    subs = [_sg(i, f"B{i}", (10.0, 10.0)) for i in range(1, 11)]
    res = spc.compute_spc(subs, _param(lsl=0.0, usl=20.0), period="P")
    assert res.sigma == 0.0
    assert res.cp is None
    assert res.cpk is None
    assert "Нулевой размах: Cp/Cpk не рассчитаны" in res.warnings


def test_compute_spc_beyond_limit_violation() -> None:
    subs = [_sg(i, f"B{i}", (14.0, 15.0)) for i in range(1, 10)]
    subs.append(_sg(10, "OUT", (30.0, 31.0)))
    res = spc.compute_spc(subs, _param(), period="P")

    # X̿ = 16.1, R̄ = 1, sigma = 1/1.128, UCL = 16.1 + 1.88 = 17.98
    assert res.points[9].xbar == 30.5
    assert res.points[9].violations == ("beyond_limit",)
    assert res.points[9].violation_texts      # Russian text present
    assert res.violation_count == 1
    assert all(
        "beyond_limit" not in p.violations for p in res.points[:9]
    )


def test_compute_spc_zone_a_2_of_3_attributed_to_third_index() -> None:
    # CL = 0.45, R̄ = 1, 2σ̂ ≈ 1.773; highs at included indices 5 and 7.
    xbars = [0.0, 0.0, 0.0, 0.0, 0.0, 2.25, 0.0, 2.25, 0.0, 0.0]
    subs = [
        _sg(i + 1, f"B{i + 1}", (x - 0.5, x + 0.5))
        for i, x in enumerate(xbars)
    ]
    res = spc.compute_spc(subs, _param(), period="P")

    # only the window [5, 6, 7] (indices) has two points above CL beyond 2σ̂
    # and the violation is attributed to the THIRD one -> index 7
    assert res.points[7].violations == ("zone_a_2_of_3",)
    assert "zone_a_2_of_3" not in res.points[5].violations
    assert "zone_a_2_of_3" not in res.points[6].violations
    assert "zone_a_2_of_3" not in res.points[8].violations
    assert res.violation_count == 1


def test_compute_spc_point_values_rounded_to_spc_value_dp() -> None:
    res = spc.compute_spc(
        [_sg(i, f"B{i}", (10.0, 10.0, 10.0, 10.0, 12.0)) for i in range(1, 11)],
        _param(),
        period="P",
    )
    assert res.sigma == pytest.approx(round(2.0 / _CONSTANTS[5][0], SPC_VALUE_DP))
    assert res.sigma == 0.8598
    for point in res.points:
        assert point.xbar == 10.4
        assert point.r == 2.0
    # 72/7 is an exact case: 10.285714... -> 10.2857
    res7 = spc.compute_spc(
        [_sg(i, f"B{i}", (10.0,) * 6 + (12.0,)) for i in range(1, 11)],
        _param(),
        period="P",
    )
    assert res7.points[0].xbar == pytest.approx(round(72 / 7, SPC_VALUE_DP))
    assert res7.points[0].xbar == 10.2857


# ==========================================================================
# §7.2 — SVG charts
# ==========================================================================

def _ids(svg: str) -> list[str]:
    return re.findall(r'id="([^"]+)"', svg)


def test_charts_empty_input_returns_exact_contract_string() -> None:
    empty = f'<p class="chart-empty">{CHART_EMPTY_TEXT}</p>'

    assert charts.pareto_chart(
        ParetoResult(rows=(), total_qty=0, top_share=0.0,
                     as_of="2026-09-30", months=6)
    ) == empty

    assert charts.trend_chart([]) == empty
    assert charts.trend_chart(
        [TrendPoint(month="2026-01", batches=0, defect_rate=None,
                    accepted_share=None)]
    ) == empty

    assert charts.spc_chart(
        SpcResult(
            parameter=_param(), points=(), excluded_subgroups=0,
            cl=None, ucl=None, lcl=None, r_cl=None, r_ucl=None, r_lcl=None,
            sigma=None, cp=None, cpk=None, cpk_side=None, modal_n=None,
            enough=False, warnings=(), period="P",
        )
    ) == empty

    assert charts.weight_bar(
        IndexWeights(w_defect=0.0, w_spec=0.0, w_repeat=0.0, w_clean=0.0)
    ) == empty


def test_charts_svg_basics_no_external_fonts_or_scripts() -> None:
    pareto_res = pareto.compute_pareto([_dc("D1", 10)], months=3)
    trend_points = [TrendPoint(month="2026-01", batches=2, defect_rate=0.1,
                               accepted_share=0.5)]

    outputs = [
        charts.pareto_chart(pareto_res),
        charts.trend_chart(trend_points),
        charts.spc_chart(_spc_result()),
        charts.weight_bar(IndexWeights()),
    ]
    for out in outputs:
        assert 'role="img"' in out
        assert "<title>" in out
        assert "<script" not in out.lower()
        assert "http" not in out.lower()
        assert "url(" not in out.lower()


def test_charts_escape_dynamic_text() -> None:
    injection = '<b>&"x"'
    escaped = "&lt;b&gt;&amp;&quot;x&quot;"

    # Pareto: dynamic defect name (and code) must be escaped, not raw.
    pareto_res = pareto.compute_pareto(
        [_dc("D1", 10, name=injection)], months=3
    )
    out = charts.pareto_chart(pareto_res)
    assert injection not in out
    assert "<b>" not in out
    assert escaped in out

    # Trends: dynamic month label.
    out = charts.trend_chart([
        TrendPoint(month="2026-01", batches=2, defect_rate=0.1,
                   accepted_share=0.5),
        TrendPoint(month=injection, batches=0, defect_rate=None,
                   accepted_share=None),
    ])
    assert injection not in out
    assert escaped in out

    # SPC: dynamic parameter name and subgroup label.
    out = charts.spc_chart(_spc_result(parameter=_param(name=injection),
                                       label=injection))
    assert injection not in out
    assert "<b>" not in out
    assert escaped in out


def test_charts_element_ids_unique_and_not_global() -> None:
    pareto_res = pareto.compute_pareto(
        [_dc("D1", 10), _dc("D2", 5)], months=3
    )
    first = charts.pareto_chart(pareto_res)
    second = charts.pareto_chart(pareto_res)

    ids_first = _ids(first)
    ids_second = _ids(second)
    assert len(ids_first) >= 1
    assert len(ids_first) == len(set(ids_first))          # unique in one call
    assert set(ids_first).isdisjoint(ids_second)          # fresh per call

    out = charts.spc_chart(_spc_result())
    spc_ids = _ids(out)
    assert len(spc_ids) == len(set(spc_ids)) == 2
    assert any(i.startswith("spcx-") for i in spc_ids)
    assert any(i.startswith("spcr-") for i in spc_ids)


def test_charts_weight_bar_renders_weight_values() -> None:
    out = charts.weight_bar(
        IndexWeights(w_defect=0.4, w_spec=0.3, w_repeat=0.15, w_clean=0.15)
    )
    assert 'role="img"' in out
    assert "<title>" in out
    assert "0.40" in out
    assert "0.30" in out
    assert "0.15" in out
