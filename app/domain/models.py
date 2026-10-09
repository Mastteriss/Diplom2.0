"""Доменные модели — сквозной контракт между слоями.

Типы этого модуля совместно используют доступ к данным, анализ и веб-слой.
Имена полей и классов — часть зафиксированного контракта (docs/contract.md):
переименование или смена типа поля ломает соседние слои, поэтому менять их
без синхронного обновления contract.md нельзя.
"""

from __future__ import annotations

from dataclasses import dataclass

# Ключ критерия -> имя веса в IndexWeights.
_CRITERION_FIELDS: dict[str, str] = {
    "defect": "w_defect",
    "spec": "w_spec",
    "repeat": "w_repeat",
    "clean": "w_clean",
}


@dataclass(frozen=True, slots=True)
class IndexWeights:
    """Веса индекса качества и параметры расчёта (аудит в updated_at/updated_by)."""

    version: int = 1
    w_defect: float = 0.40
    w_spec: float = 0.30
    w_repeat: float = 0.15
    w_clean: float = 0.15
    defect_rate_cap: float = 0.10
    repeat_rate_cap: float = 0.20
    min_sample: int = 10
    updated_at: str = ""
    updated_by: str = "system"

    def normalized(self, available: tuple[str, ...]) -> dict[str, float]:
        """Нормирует четыре веса только над доступными критериями.

        ``available`` — подмножество ``("defect", "spec", "repeat", "clean")``.
        Для пустого входа возвращает ``{}``. Отрицательный вес или нулевая
        сумма доступных весов — ``ValueError``.
        """
        if not available:
            return {}
        chosen: dict[str, float] = {}
        for key in available:
            if key not in _CRITERION_FIELDS:
                raise ValueError(f"unknown criterion: {key!r}")
            value = getattr(self, _CRITERION_FIELDS[key])
            if value < 0:
                raise ValueError(f"weight {key!r} must be non-negative, got {value}")
            chosen[key] = value
        total = sum(chosen.values())
        if total <= 0:
            raise ValueError(f"sum of available weights must be > 0, got {total}")
        return {key: value / total for key, value in chosen.items()}


@dataclass(frozen=True, slots=True)
class ProductFacts:
    """Агрегаты по продукции за расчётный период (вход индекса качества)."""

    product_id: int
    sku: str
    name: str
    category: str
    sample_qty: int
    rejected_qty: int
    defect_qty: int
    repeat_qty: int
    in_spec: int
    with_specs: int
    batches_total: int
    batches_clean: int


@dataclass(frozen=True, slots=True)
class IndexTerms:
    """Значения критериев индекса (None — критерий недоступен) и веса расчёта."""

    f_defect: float | None
    f_spec: float | None
    f_repeat: float | None
    f_clean: float | None
    used_weights: dict[str, float]


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Результат индекса качества по одной позиции продукции."""

    product_id: int
    sku: str
    name: str
    status: str                  # "ok" | "insufficient_data" | "no_data"
    score: float | None
    raw_score: float | None
    terms: IndexTerms
    notes: str                   # по-русски
    sample_qty: int
    defect_rate: float | None


@dataclass(frozen=True, slots=True)
class DefectCount:
    """Количество дефектов одного кода за период (вход Парето)."""

    code: str
    name: str
    group_name: str
    severity: int
    qty: int


@dataclass(frozen=True, slots=True)
class ParetoRow:
    """Строка диаграммы Парето с накопленной долей и классом ABC."""

    code: str
    name: str
    group_name: str
    qty: int
    share: float
    cumulative: float
    abc: str                     # "A" | "B" | "C"


@dataclass(frozen=True, slots=True)
class ParetoResult:
    """Результат анализа Парето за период."""

    rows: tuple[ParetoRow, ...]
    total_qty: int
    top_share: float
    as_of: str
    months: int

    @property
    def is_empty(self) -> bool:
        return self.total_qty <= 0 or not self.rows


@dataclass(frozen=True, slots=True)
class MonthlyFacts:
    """Агрегаты одного месяца (вход динамики)."""

    month: str                   # "YYYY-MM"
    batches: int
    inspections: int
    sample_qty: int
    rejected_qty: int
    defect_rate: float | None
    accepted_batches: int


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """Точка динамики по одному месяцу."""

    month: str
    batches: int
    defect_rate: float | None
    accepted_share: float | None


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """Справочное описание контролируемого параметра и его допусков."""

    id: int
    code: str
    name: str
    unit: str
    target: float | None
    lsl: float | None
    usl: float | None
    is_critical: bool


@dataclass(frozen=True, slots=True)
class SpcSubgroup:
    """Подгруппа SPC: все измерения одного акта по одному параметру."""

    subgroup_no: int
    label: str
    values: tuple[float, ...]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def xbar(self) -> float:
        return sum(self.values) / self.n if self.n else 0.0

    @property
    def r(self) -> float:
        return max(self.values) - min(self.values) if self.values else 0.0


@dataclass(frozen=True, slots=True)
class SpcPoint:
    """Точка контрольной карты с признаками нарушений."""

    subgroup_no: int
    label: str
    n: int
    xbar: float
    r: float
    violations: tuple[str, ...]        # коды причин
    violation_texts: tuple[str, ...]   # тексты по-русски


@dataclass(frozen=True, slots=True)
class SpcResult:
    """Полный результат расчёта контрольных карт по параметру."""

    parameter: ParameterSpec
    points: tuple[SpcPoint, ...]
    excluded_subgroups: int
    cl: float | None
    ucl: float | None
    lcl: float | None
    r_cl: float | None
    r_ucl: float | None
    r_lcl: float | None
    sigma: float | None
    cp: float | None
    cpk: float | None
    cpk_side: str | None         # "two" | "upper" | "lower" | None
    modal_n: int | None
    enough: bool
    warnings: tuple[str, ...]    # по-русски
    period: str

    @property
    def violation_count(self) -> int:
        return sum(len(point.violations) for point in self.points)


@dataclass(frozen=True, slots=True)
class InspectionDraft:
    """Черновик акта ОТК до записи в БД (результат валидации формы)."""

    inspected_at: str
    inspector: str
    sample_qty: int
    accepted_qty: int
    rejected_qty: int
    defects: tuple[tuple[str, int], ...]
    measurements: tuple[tuple[str, tuple[float, ...]], ...]
