"""Генератор демо-данных (docs/contract.md §8).

Запуск::

    python -m scripts.generate_demo_data [--products 10] [--batches-per-product 18]
        [--months 24] [--seed 20260101] [--anchor 2026-09-30]
        [--db data/quality.sqlite3] [--force-reference]

Инварианты (§8):

* только добавление: естественные ключи — продукция ``sku``, параметр и
  дефект ``code``, партия ``(product_id, batch_no)``; существующая партия
  пропускается целиком (без актов, дефектов и измерений), ничего не
  удаляется и не обновляется — повторный запуск не меняет ни одной строки;
* чистый ``random.Random(seed)``: все случайные решения берутся только из
  него, итерирование по ``set``/``dict``, влияющее на id и порядок,
  отсутствует (списки, кортежи и ключи ``ORDER BY``) — одинаковый seed даёт
  одинаковые числовые значения;
* генератор НЕ импортирует ``app.analysis``; справочники сажает
  ``app.db.seed.seed_reference_data``, статусы партий считает только
  ``app.db.repositories.recompute_batch_status`` (никаких собственных правил
  статуса);
* данные: ``products`` партий ``batches-per-product`` растянуты на ``months``
  месяцев до ``anchor``; 1–2 акта ОТК на партию (всегда финальный, иногда
  повторный после доработки); ровно 5 измерений на пару (акт, параметр) для
  ВСЕХ параметров; события дефектов с «тяжёлой головой» (код ``SCRATCH``
  даёт ~45 % количества за последние 12 месяцев, что ≥ 35 % для Парето);
  ``is_repeat = 1`` проставляет только ``create_inspection`` (повторный акт
  намеренно наследует 1–2 кода из первого — ожидается ≥ 30 повторов);
  присутствуют все четыре статуса, включая партии ``pending`` без актов.

Внутренняя таблица sigma (по ``code``; нормальные значения вокруг ``target``,
``SURFACE`` без допусков — location 3.2 мкм при ``target = NULL``)::

    DIAM 0.06 | BORE 0.05 | LENGTH 0.40 | WEIGHT 0.06 | HARDNESS 1.00
    SURFACE 0.80

Доля выбросов: со сдвигом процесса ``N(0, 0.4σ)`` на (акт, параметр) плюс
6 % значений принудительно за пределом допуска (при наличии) — Cp/Cpk и C2
получаются «живыми», а не идеальными.

Посадочная аномалия SPC: у продукта ``SKU-1004`` параметр ``BORE`` в двух
ПОСЛЕДОВАТЕЛЬНЫх актах (последний акт партии ``batches-per-product - 1``)
все пять значений равны ``target + 6σ = 12.0 + 6·0.05 = 12.30`` (≥ UCL с
запасом) → гарантировано нарушение ``beyond_limit``; даты этих актов —
``anchor − 3`` и ``anchor − 1``, все прочие акты ``SKU-1004`` принудительно
не позже ``anchor − 4``, поэтому подгруппы идут подряд. Остальные значения
``BORE`` распределены нормально вокруг ``target``.

Перед завершением: ``PRAGMA wal_checkpoint(TRUNCATE)``, закрытие соединения
и удаление файлов ``-wal``/``-shm``.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.config import PROJECT_DIR
from app.db.connection import connect, init_db
from app.db.repositories import (
    create_inspection,
    load_pareto_counts,
    recompute_batch_status,
)
from app.db.seed import seed_reference_data
from app.domain.models import InspectionDraft

# --- Значения по умолчанию (contract.md §8) --------------------------------
DEFAULT_PRODUCTS = 10
DEFAULT_BATCHES_PER_PRODUCT = 18
DEFAULT_MONTHS = 24
DEFAULT_SEED = 20260101
DEFAULT_ANCHOR = "2026-09-30"
DEFAULT_DB = "data/quality.sqlite3"

# --- Генерация измерений ---------------------------------------------------
_SIGMA_BY_CODE: dict[str, float] = {
    "DIAM": 0.06,
    "BORE": 0.05,
    "LENGTH": 0.40,
    "WEIGHT": 0.06,
    "HARDNESS": 1.00,
    "SURFACE": 0.80,
}
_LOCATION_BY_CODE: dict[str, float] = {"SURFACE": 3.2}
_VALUES_PER_ACT = 5          # измерений на (акт, параметр) — §8 п.5
_OOT_PROB = 0.06             # доля принудительно вне допуска
_SHIFT_SIGMA = 0.4           # сдвинут процесс: N(0, 0.4σ) на (акт, параметр)

# --- Посадка: SKU-1004 / BORE ---------------------------------------------
_PLANTED_SKU = "SKU-1004"
_PLANTED_PARAM = "BORE"
_PLANTED_BACK_DAYS = (13, 25)   # выпуск посадочной партии: anchor − 13..25
_PLANTED_ACT1_DAYS = 3          # первый акт: anchor − 3
_PLANTED_ACT2_DAYS = 1          # второй акт: anchor − 1
_OTHER_ACT_LIMIT_DAYS = 4       # прочие акты SKU-1004 ≤ anchor − 4

# --- Генерация дефектов ----------------------------------------------------
_DEFECT_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("SCRATCH", 35.0),
    ("DENT", 13.0),
    ("OVERSIZE", 10.0),
    ("UNDERSIZE", 9.0),
    ("THREAD", 7.0),
    ("POROUS", 6.0),
    ("CRACK", 5.0),
    ("INCLUSION", 4.0),
    ("LEAK", 4.0),
    ("PACK", 3.0),
    ("LABEL", 2.0),
    ("MISC", 2.0),
)
_TOP_DEFECT = "SCRATCH"        # «тяжёлая голова» для Парето (≥ 35 %)
_TOP_DEFECT_QTY = (3, 21)
_OTHER_DEFECT_QTY = (1, 12)
_CODES_PER_ACT = (1, 2, 2, 3, 3, 4)
_REWORK_PROB = 0.45            # вероятность второго (повторного) акта
_REWORK_CARRY_PROB = 0.65      # повторный акт наследует 1–2 кода первого

# --- Генерация партий и актов ---------------------------------------------
_LINES = ("Линия-1", "Линия-2", "Линия-3")
_INSPECTORS = ("Иванов И.И.", "Петров П.П.", "Сидоров С.С.", "Кузнецов А.А.")
_PRODUCED_QTY = (400, 2500)
_SAMPLE_QTY = (12, 45)
_MAX_PRODUCED_DAY = 28
_REJECTED_MODULO = (11, 5)     # g % 11 == 5 → партия «в брак»
_ACCEPTED_MODULO = (7, 3)      # g % 7 == 3  → партия «без брака»
_PENDING_MULTIPLIER = 4        # последняя партия products с p % 4 == 1
_STATUSES = ("pending", "accepted", "partial", "rejected")
_TABLES = ("batches", "inspections", "defect_events", "measurements")


# --- Вспомогательные типы --------------------------------------------------

@dataclass(frozen=True)
class _Spec:
    """Строка ``parameters_ref`` + sigma/location генератора."""

    code: str
    target: float | None
    lsl: float | None
    usl: float | None
    sigma: float
    location: float


@dataclass
class _ActPlan:
    """Черновик одного акта ОТК до записи в БД."""

    inspected_at: str
    inspector: str
    sample_qty: int
    accepted_qty: int
    rejected_qty: int
    defects: list[tuple[str, int]]
    measurements: list[tuple[str, tuple[float, ...]]]


@dataclass
class _BatchPlan:
    """Черновик одной партии: реквизиты + акты (пусто — pending)."""

    product_id: int
    sku: str
    batch_no: str
    line: str
    produced_qty: int
    produced_at: str
    is_pending: bool
    is_planted: bool
    acts: list[_ActPlan]


# --- Даты ------------------------------------------------------------------

def _add_months(day: date, delta: int) -> date:
    """Дата того же числа через ``delta`` месяцев (число клампится по месяцу)."""
    index = day.year * 12 + (day.month - 1) + delta
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


# --- Измерения -------------------------------------------------------------

def _out_of_tolerance(rng: random.Random, spec: _Spec) -> float:
    """Значение гарантированно за границей допуска (на величину ~σ)."""
    margin = spec.sigma * (0.4 + abs(rng.gauss(0.0, 0.5)))
    if spec.lsl is not None and spec.usl is not None:
        if rng.random() < 0.5:
            return spec.lsl - margin
        return spec.usl + margin
    if spec.lsl is not None:
        return spec.lsl - margin
    if spec.usl is not None:
        return spec.usl + margin
    return spec.location


def _draw_measurements(
    rng: random.Random, specs: list[_Spec], *, planted: bool
) -> list[tuple[str, tuple[float, ...]]]:
    """5 значений на каждый параметр; при ``planted`` — BORE = target + 6σ."""
    planted_value: float | None = None
    if planted:
        for spec in specs:
            if spec.code == _PLANTED_PARAM and spec.target is not None:
                sigma = _SIGMA_BY_CODE.get(_PLANTED_PARAM, 0.05)
                planted_value = round(spec.target + 6.0 * sigma, 4)
    rows: list[tuple[str, tuple[float, ...]]] = []
    for spec in specs:
        shift = rng.gauss(0.0, _SHIFT_SIGMA * spec.sigma)
        values: list[float] = []
        for _ in range(_VALUES_PER_ACT):
            if spec.lsl is None and spec.usl is None:
                value = spec.location + shift + rng.gauss(0.0, spec.sigma)
            elif rng.random() < _OOT_PROB:
                value = _out_of_tolerance(rng, spec)
            else:
                value = spec.location + shift + rng.gauss(0.0, spec.sigma)
            values.append(round(value, 4))
        if planted and spec.code == _PLANTED_PARAM and planted_value is not None:
            values = [planted_value] * _VALUES_PER_ACT
        rows.append((spec.code, tuple(values)))
    return rows


# --- Дефекты ---------------------------------------------------------------

def _weighted_code(rng: random.Random, pool: list[tuple[str, float]]) -> str:
    """Выбор кода пропорционально весам из фиксированного списка кортежей."""
    total = sum(weight for _, weight in pool)
    point = rng.random() * total
    acc = 0.0
    for code, weight in pool:
        acc += weight
        if point < acc:
            return code
    return pool[-1][0]


def _draw_defects(
    rng: random.Random,
    pool: list[tuple[str, float]],
    carry_from: list[str] | None,
) -> list[tuple[str, int]]:
    """1–4 события дефектов акта; повторный акт наследует коды первого."""
    target = rng.choice(_CODES_PER_ACT)
    codes: list[str] = []
    if carry_from and rng.random() < _REWORK_CARRY_PROB:
        shared = rng.randint(1, min(2, len(carry_from)))
        codes.extend(rng.sample(carry_from, shared))
    while len(codes) < target:
        code = _weighted_code(rng, pool)
        if code not in codes:
            codes.append(code)
    defects: list[tuple[str, int]] = []
    for code in codes:
        qty = (
            rng.randint(*_TOP_DEFECT_QTY)
            if code == _TOP_DEFECT
            else rng.randint(*_OTHER_DEFECT_QTY)
        )
        defects.append((code, qty))
    return defects


# --- Акты и партии ---------------------------------------------------------

def _batch_mode(g: int) -> str:
    """Режим партии «в брак» / «без брака» / «частично» (только объёмы актов)."""
    mod, rem = _REJECTED_MODULO
    if g % mod == rem:
        return "rejected"
    mod, rem = _ACCEPTED_MODULO
    if g % mod == rem:
        return "accepted"
    return "partial"


def _draw_act(
    rng: random.Random,
    when: date,
    specs: list[_Spec],
    pool: list[tuple[str, float]],
    mode: str,
    carry_from: list[str] | None,
    *,
    planted: bool,
) -> _ActPlan:
    """Черновик одного акта: выборка, брак, дефекты, измерения."""
    sample_qty = rng.randint(*_SAMPLE_QTY)
    if mode == "accepted":
        rejected_qty = 0
    elif mode == "rejected":
        rejected_qty = sample_qty
    else:
        rejected_qty = rng.randint(1, sample_qty - 1)
    inspector = rng.choice(_INSPECTORS)
    defects = _draw_defects(rng, pool, carry_from)
    measurements = _draw_measurements(rng, specs, planted=planted)
    return _ActPlan(
        inspected_at=when.isoformat(),
        inspector=inspector,
        sample_qty=sample_qty,
        accepted_qty=sample_qty - rejected_qty,
        rejected_qty=rejected_qty,
        defects=defects,
        measurements=measurements,
    )


def _clamp(day: date, low: date, high: date) -> date:
    """Кламп даты в ``[low, high]`` (high ≥ low по построению)."""
    if day < low:
        return low
    if day > high:
        return high
    return day


def _draw_batch(
    rng: random.Random,
    *,
    product_id: int,
    sku: str,
    p_idx: int,
    b_idx: int,
    batch_total: int,
    months: int,
    window_start: date,
    anchor: date,
    specs: list[_Spec],
    pool: list[tuple[str, float]],
    pending: bool,
    planted: bool,
    date_limit: date | None,
) -> _BatchPlan:
    """Черновик партии со всеми актами (рисуется ДО проверки на существование)."""
    g = p_idx * batch_total + b_idx
    mode = _batch_mode(g)
    batch_no = f"B{b_idx + 1:04d}-{sku}"

    if planted:
        produced = anchor - timedelta(days=rng.randint(*_PLANTED_BACK_DAYS))
        line = rng.choice(_LINES)
        produced_qty = rng.randint(*_PRODUCED_QTY)
        act_dates = [
            anchor - timedelta(days=_PLANTED_ACT1_DAYS),
            anchor - timedelta(days=_PLANTED_ACT2_DAYS),
        ]
    else:
        month_idx = min(months - 1, (b_idx * months + p_idx) // batch_total)
        month_start = _add_months(window_start, month_idx)
        days_in_month = monthrange(month_start.year, month_start.month)[1]
        cap = min(_MAX_PRODUCED_DAY, days_in_month)
        if month_start.year == anchor.year and month_start.month == anchor.month:
            cap = min(cap, anchor.day)
        produced = date(month_start.year, month_start.month, rng.randint(1, max(1, cap)))
        line = rng.choice(_LINES)
        produced_qty = rng.randint(*_PRODUCED_QTY)
        first = min(produced + timedelta(days=rng.randint(1, 10)), anchor)
        act_dates = [first]
        if rng.random() < _REWORK_PROB:
            act_dates.append(min(first + timedelta(days=rng.randint(4, 15)), anchor))

    if date_limit is not None:
        if produced > date_limit:
            produced = date_limit
        act_dates = [_clamp(day, produced, date_limit) for day in act_dates]

    acts: list[_ActPlan] = []
    carry: list[str] | None = None
    for when in act_dates:
        act = _draw_act(
            rng, when, specs, pool, mode, carry, planted=planted
        )
        acts.append(act)
        carry = [code for code, _ in act.defects]

    return _BatchPlan(
        product_id=product_id,
        sku=sku,
        batch_no=batch_no,
        line=line,
        produced_qty=produced_qty,
        produced_at=produced.isoformat(),
        is_pending=pending,
        is_planted=planted,
        acts=acts,
    )


def _build_plans(
    rng: random.Random,
    selected: list[sqlite3.Row],
    specs: list[_Spec],
    pool: list[tuple[str, float]],
    *,
    months: int,
    batches_per_product: int,
    anchor: date,
) -> list[_BatchPlan]:
    """Планы всех партий: рисуются для КАЖДОЙ партии ДО любой записи в БД."""
    window_start = _add_months(anchor.replace(day=1), -(months - 1))
    last_batch = batches_per_product - 1
    pending_keys = {
        (p_idx, last_batch)
        for p_idx in range(len(selected))
        if p_idx % _PENDING_MULTIPLIER == 1
    }
    if not pending_keys and selected:
        pending_keys = {(len(selected) - 1, last_batch)}
    planted_p = None
    for p_idx, product in enumerate(selected):
        if product["sku"] == _PLANTED_SKU:
            planted_p = p_idx
            break
    planted_key = (planted_p, last_batch) if planted_p is not None else None
    if planted_key is not None and planted_key in pending_keys:
        pending_keys.discard(planted_key)
    date_limit = anchor - timedelta(days=_OTHER_ACT_LIMIT_DAYS)

    plans: list[_BatchPlan] = []
    for p_idx, product in enumerate(selected):
        for b_idx in range(batches_per_product):
            key = (p_idx, b_idx)
            is_pending = key in pending_keys
            is_planted = key == planted_key
            plan = _draw_batch(
                rng,
                product_id=int(product["id"]),
                sku=str(product["sku"]),
                p_idx=p_idx,
                b_idx=b_idx,
                batch_total=batches_per_product,
                months=months,
                window_start=window_start,
                anchor=anchor,
                specs=specs,
                pool=pool,
                pending=is_pending,
                planted=is_planted,
                date_limit=date_limit if p_idx == planted_p and not is_planted else None,
            )
            if is_pending:
                plan.acts = []  # черновики уже израсходованы rng — партия без актов
            plans.append(plan)
    return plans


# --- Запись в БД (только добавление) ---------------------------------------

def _insert_plans(
    conn: sqlite3.Connection,
    plans: list[_BatchPlan],
    params_ref: dict[str, int],
    defects_ref: dict[str, int],
) -> tuple[int, int, int]:
    """Вставляет только отсутствующие партии; возвращает (добавлено, пропущено, актов)."""
    existing: set[tuple[int, str]] = set()
    for row in conn.execute("SELECT product_id, batch_no FROM batches"):
        existing.add((int(row["product_id"]), str(row["batch_no"])))

    added = skipped = acts_added = 0
    for plan in plans:
        if (plan.product_id, plan.batch_no) in existing:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO batches "
            "(product_id, batch_no, line, produced_qty, produced_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (plan.product_id, plan.batch_no, plan.line,
             plan.produced_qty, plan.produced_at),
        )
        if cur.rowcount == 0:
            skipped += 1
            continue
        existing.add((plan.product_id, plan.batch_no))
        batch_id = int(cur.lastrowid)
        added += 1
        for act in plan.acts:
            draft = InspectionDraft(
                inspected_at=act.inspected_at,
                inspector=act.inspector,
                sample_qty=act.sample_qty,
                accepted_qty=act.accepted_qty,
                rejected_qty=act.rejected_qty,
                defects=tuple(act.defects),
                measurements=tuple(
                    (code, tuple(values)) for code, values in act.measurements
                ),
            )
            create_inspection(
                conn,
                batch_id=batch_id,
                draft=draft,
                defects_ref=defects_ref,
                params_ref=params_ref,
            )
            acts_added += 1
        recompute_batch_status(conn, batch_id)
    return added, skipped, acts_added


# --- Справочники -----------------------------------------------------------

def _reference_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Число строк справочников (ключи как у ``seed_reference_data``)."""
    return {
        "products": int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]),
        "parameters": int(
            conn.execute("SELECT COUNT(*) FROM parameters_ref").fetchone()[0]
        ),
        "defects": int(conn.execute("SELECT COUNT(*) FROM defects_ref").fetchone()[0]),
        "config": int(
            conn.execute("SELECT COUNT(*) FROM quality_index_config").fetchone()[0]
        ),
    }


def _ensure_reference(
    conn: sqlite3.Connection, force: bool
) -> tuple[dict[str, int], bool]:
    """Сажает справочники (идемпотентно): всегда при ``force`` либо если пусты."""
    counts = _reference_counts(conn)
    empty = (
        counts["products"] == 0
        or counts["parameters"] == 0
        or counts["defects"] == 0
    )
    if force or empty:
        return seed_reference_data(conn), True
    return counts, False


def _load_specs(conn: sqlite3.Connection) -> list[_Spec]:
    """Параметры в порядке ``id`` + sigma/location из внутренней таблицы."""
    specs: list[_Spec] = []
    for row in conn.execute(
        "SELECT code, target, lsl, usl FROM parameters_ref ORDER BY id"
    ):
        code = str(row["code"])
        target = row["target"]
        specs.append(
            _Spec(
                code=code,
                target=target,
                lsl=row["lsl"],
                usl=row["usl"],
                sigma=float(_SIGMA_BY_CODE.get(code, 0.1)),
                location=float(
                    target if target is not None else _LOCATION_BY_CODE.get(code, 0.0)
                ),
            )
        )
    return specs


def _load_defect_pool(conn: sqlite3.Connection) -> tuple[list[tuple[str, float]], dict[str, int]]:
    """Весовой пул кодов (фиксированный порядок) + код → id."""
    defects_ref: dict[str, int] = {}
    for row in conn.execute("SELECT id, code FROM defects_ref ORDER BY id"):
        defects_ref[str(row["code"])] = int(row["id"])
    pool = [
        (code, weight)
        for code, weight in _DEFECT_WEIGHTS
        if code in defects_ref
    ]
    return pool, defects_ref


# --- Итоги -----------------------------------------------------------------

def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _TABLES
    }


def _status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = dict.fromkeys(_STATUSES, 0)
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM batches GROUP BY status"
    ):
        counts[str(row["status"])] = int(row["n"])
    return counts


def _print_summary(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    args: argparse.Namespace,
    reference: dict[str, int],
    seeded: bool,
    added: int,
    skipped: int,
    acts_added: int,
    planted_value: float | None,
    checkpoint: str,
) -> None:
    """Человекочитаемая сводка по таблицам, статусам и посадкам (§8)."""
    since = _add_months(args_anchor(args).replace(day=1), -11).isoformat()
    until = args_anchor(args).isoformat()
    counts = _table_counts(conn)
    statuses = _status_counts(conn)
    rows = load_pareto_counts(conn, since=since, until=until)
    total_qty = sum(row.qty for row in rows)
    top = rows[0] if rows else None
    repeats = int(
        conn.execute(
            "SELECT COUNT(*) FROM defect_events WHERE is_repeat = 1"
        ).fetchone()[0]
    )

    print("=== Демо-данные сгенерированы ===")
    print(f"База: {db_path}")
    print(
        f"Параметры: products={args.products} "
        f"batches-per-product={args.batches_per_product} months={args.months} "
        f"seed={args.seed} anchor={args.anchor}"
    )
    print(
        "Справочники: "
        f"продукты={reference['products']} параметры={reference['parameters']} "
        f"дефекты={reference['defects']} конфиг={reference['config']}"
        + (" (посажены)" if seeded else " (уже были — пропуск)")
    )
    print("Таблицы:")
    print(f"  batches: {counts['batches']} (новых: {added}, пропущено существующих: {skipped})")
    print(f"  inspections: {counts['inspections']} (добавлено актов: {acts_added})")
    print(f"  defect_events: {counts['defect_events']}")
    print(f"  measurements: {counts['measurements']}")
    print(
        "Статусы партий: "
        + ", ".join(f"{status}={statuses[status]}" for status in _STATUSES)
    )
    if top is not None and total_qty > 0:
        share = top.qty / total_qty
        print(
            f"Топ-дефект за 12 мес. ({since}..{until}): {top.code} — "
            f"{top.qty} из {total_qty} ({share:.1%})"
        )
    else:
        print(f"Топ-дефект за 12 мес. ({since}..{until}): нет данных")
    print(f"Повторные дефекты (is_repeat=1): {repeats}")
    if planted_value is not None:
        print(
            f"Посадка: {_PLANTED_SKU}/{_PLANTED_PARAM} — 2 последовательных акта "
            f"со значениями target + 6σ = {planted_value:.4f}"
        )
    else:
        print(f"Посадка: {_PLANTED_SKU} в выборке отсутствует — аномалия не сажалась")
    print(f"PRAGMA wal_checkpoint(TRUNCATE): {checkpoint}")


def args_anchor(args: argparse.Namespace) -> date:
    """Дата ``anchor`` из аргументов CLI."""
    return date.fromisoformat(args.anchor)


# --- CLI -------------------------------------------------------------------

def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"не целое число: {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("значение должно быть ≥ 1")
    return value


def _seed_int(text: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"не целое число: {text!r}") from exc


def _anchor_date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"некорректная дата (ожидается YYYY-MM-DD): {text!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Аргументы CLI строго по contract.md §8."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.generate_demo_data",
        description="Генератор демо-данных (только добавление, §8 контракта).",
    )
    parser.add_argument("--products", type=_positive_int, default=DEFAULT_PRODUCTS,
                        help="число позиций продукции (по умолчанию 10)")
    parser.add_argument("--batches-per-product", type=_positive_int,
                        default=DEFAULT_BATCHES_PER_PRODUCT,
                        help="партий на позицию (по умолчанию 18)")
    parser.add_argument("--months", type=_positive_int, default=DEFAULT_MONTHS,
                        help="месяцев до anchor (по умолчанию 24)")
    parser.add_argument("--seed", type=_seed_int, default=DEFAULT_SEED,
                        help="seed генератора (по умолчанию 20260101)")
    parser.add_argument("--anchor", type=_anchor_date, default=DEFAULT_ANCHOR,
                        help="конечная дата периода (по умолчанию 2026-09-30)")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="путь к файлу БД (по умолчанию data/quality.sqlite3)")
    parser.add_argument("--force-reference", action="store_true",
                        help="пересажать справочники (INSERT OR IGNORE, без изменений)")
    return parser


def _resolve_db(raw: str) -> Path:
    """Путь к БД: абсолютный как есть, относительный — от корня проекта."""
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


# --- Точка входа -----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Генерация демо-данных: справочники → планы → добавление → сводка."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - нестандартный поток
            pass
    args = build_parser().parse_args(argv)
    db_path = _resolve_db(args.db)
    anchor = args_anchor(args)

    init_db(db_path)
    conn = connect(db_path)
    try:
        reference, seeded = _ensure_reference(conn, force=args.force_reference)

        selected = [
            row
            for row in conn.execute(
                "SELECT id, sku FROM products ORDER BY sku, id"
            )
        ][: args.products]
        if len(selected) < args.products:
            print(
                f"Внимание: доступно только {len(selected)} позиций "
                f"из запрошенных {args.products}."
            )
        specs = _load_specs(conn)
        pool, defects_ref = _load_defect_pool(conn)
        params_ref: dict[str, int] = {}
        for row in conn.execute("SELECT id, code FROM parameters_ref"):
            params_ref[str(row["code"])] = int(row["id"])

        rng = random.Random(args.seed)
        plans = _build_plans(
            rng,
            selected,
            specs,
            pool,
            months=args.months,
            batches_per_product=args.batches_per_product,
            anchor=anchor,
        )
        added, skipped, acts_added = _insert_plans(
            conn, plans, params_ref, defects_ref
        )

        planted_value: float | None = None
        for spec in specs:
            if spec.code == _PLANTED_PARAM and spec.target is not None:
                sigma = _SIGMA_BY_CODE.get(_PLANTED_PARAM, 0.05)
                planted_value = round(spec.target + 6.0 * sigma, 4)
        if all(plan.sku != _PLANTED_SKU or not plan.is_planted for plan in plans):
            planted_value = None

        checkpoint_row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        checkpoint = str(tuple(checkpoint_row)) if checkpoint_row else "—"
        _print_summary(
            conn,
            db_path=db_path,
            args=args,
            reference=reference,
            seeded=seeded,
            added=added,
            skipped=skipped,
            acts_added=acts_added,
            planted_value=planted_value,
            checkpoint=checkpoint,
        )
    finally:
        conn.close()

    removed = 0
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
            removed += 1
    print(f"Боковые файлы удалены: {removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
