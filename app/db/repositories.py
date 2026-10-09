"""Репозиторный слой (docs/contract.md §7.1) — все запросы к SQLite.

Модуль зависит только от ``app.config`` и ``app.domain.models`` (спецификация
запрещает импорт ``app.analysis``). Соединение приходит первым позиционным
аргументом и имеет ``isolation_level=None`` (autocommit), поэтому многобазовые
операции выполняются в явной транзакции через :func:`_atomic`. Все параметры
передаются через ``?``-плейсхолдеры; выборки возвращаются списками ``dict``
либо доменными датаклассами, как того требует контракт.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import PAGE_SIZE
from app.domain.models import (
    DefectCount,
    IndexWeights,
    InspectionDraft,
    MonthlyFacts,
    ProductFacts,
    SpcSubgroup,
)

# Максимум плейсхолдеров в одном IN (...): запас до лимита SQLite (999).
_CHUNK_SIZE: int = 400

# Диапазон дат в запросах: ISO-дата в колонке, границы включительно.
_PERIOD = "substr({column}, 1, 10) BETWEEN ? AND ?"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> list[dict]:
    """Выполняет запрос и возвращает строки обычными словарями."""
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _query_one(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> dict | None:
    """Первая строка запроса словарём либо ``None``."""
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row is not None else None


def _chunks(items: list[int], size: int) -> Iterator[list[int]]:
    """Делит список id на части для безопасного IN (...)."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Транзакция поверх autocommit-соединения: ``BEGIN``/``COMMIT``/``ROLLBACK``.

    Если вызов уже внутри транзакции, используется SAVEPOINT, чтобы не ломать
    внешнюю транзакцию; при любой ошибке изменения откатываются.
    """
    if conn.in_transaction:
        conn.execute("SAVEPOINT _repo_atomic")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO _repo_atomic")
            conn.execute("RELEASE _repo_atomic")
            raise
        else:
            conn.execute("RELEASE _repo_atomic")
    else:
        conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _product_where(
    q: str | None, category: str | None, active_only: bool
) -> tuple[str, list[object]]:
    """Фильтры списка продукции: LIKE по названию/SKU, точная категория, активность."""
    clauses: list[str] = []
    params: list[object] = []
    if q:
        clauses.append("(p.name LIKE ? OR p.sku LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if category:
        clauses.append("p.category = ?")
        params.append(category)
    if active_only:
        clauses.append("p.is_active = 1")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _batch_where(
    product_id: int | None, status: str | None
) -> tuple[str, list[object]]:
    """Фильтры списка партий: продукция и/или статус."""
    clauses: list[str] = []
    params: list[object] = []
    if product_id is not None:
        clauses.append("b.product_id = ?")
        params.append(product_id)
    if status:
        clauses.append("b.status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# --- Справочники -----------------------------------------------------------

def list_products(conn, *, q=None, category=None, active_only=False,
                  limit=PAGE_SIZE, offset=0) -> list[dict]:
    """Строки продукции с фильтром поиска/категории и пагинацией."""
    where, params = _product_where(q, category, active_only)
    sql = (
        "SELECT p.* FROM products p"
        f"{where} ORDER BY p.sku LIMIT ? OFFSET ?"
    )
    params.extend([max(int(limit), 0), max(int(offset), 0)])
    return _query(conn, sql, params)


def count_products(conn, *, q=None, category=None, active_only=False) -> int:
    """Число продукции по тем же фильтрами, что и :func:`list_products`."""
    where, params = _product_where(q, category, active_only)
    row = _query_one(conn, f"SELECT COUNT(*) AS n FROM products p{where}", params)
    return int(row["n"]) if row else 0


def get_product(conn, product_id) -> dict | None:
    """Продукция по id либо ``None``."""
    return _query_one(conn, "SELECT * FROM products WHERE id = ?", (product_id,))


def list_categories(conn) -> list[str]:
    """Непустые категории продукции по алфавиту (для фильтра списка)."""
    rows = _query(
        conn,
        "SELECT DISTINCT category FROM products "
        "WHERE category <> '' ORDER BY category",
    )
    return [row["category"] for row in rows]


def list_defects(conn, *, active_only=True) -> list[dict]:
    """Справочник дефектов (опционально только активные) по коду."""
    where = " WHERE d.is_active = 1" if active_only else ""
    return _query(conn, f"SELECT d.* FROM defects_ref d{where} ORDER BY d.code")


def get_defect_by_code(conn, code) -> dict | None:
    """Дефект по коду либо ``None``."""
    return _query_one(conn, "SELECT * FROM defects_ref WHERE code = ?", (code,))


def list_parameters(conn, *, critical_first=True) -> list[dict]:
    """Контролируемые параметры; критичные первыми (если ``critical_first``)."""
    order = "is_critical DESC, code" if critical_first else "code"
    return _query(conn, f"SELECT * FROM parameters_ref ORDER BY {order}")


def get_parameter_by_code(conn, code) -> dict | None:
    """Параметр по коду либо ``None``."""
    return _query_one(conn, "SELECT * FROM parameters_ref WHERE code = ?", (code,))


# --- Партии и акты ОТК -----------------------------------------------------

def list_batches(conn, *, product_id=None, status=None,
                 limit=PAGE_SIZE, offset=0) -> list[dict]:
    """Партии с полями продукта и числом актов, новые сверху."""
    where, params = _batch_where(product_id, status)
    sql = (
        "SELECT b.*, p.sku AS product_sku, p.name AS product_name, "
        "(SELECT COUNT(*) FROM inspections i WHERE i.batch_id = b.id) AS inspections_count "
        "FROM batches b JOIN products p ON p.id = b.product_id"
        f"{where} ORDER BY b.produced_at DESC, b.id DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(int(limit), 0), max(int(offset), 0)])
    return _query(conn, sql, params)


def count_batches(conn, *, product_id=None, status=None) -> int:
    """Число партий по тем же фильтрам, что и :func:`list_batches`."""
    where, params = _batch_where(product_id, status)
    row = _query_one(
        conn, f"SELECT COUNT(*) AS n FROM batches b{where}", params
    )
    return int(row["n"]) if row else 0


def get_batch(conn, batch_id) -> dict | None:
    """Партия по id с полями продукта либо ``None``."""
    return _query_one(
        conn,
        "SELECT b.*, p.sku AS product_sku, p.name AS product_name, "
        "p.category AS product_category, p.unit AS product_unit "
        "FROM batches b JOIN products p ON p.id = b.product_id "
        "WHERE b.id = ?",
        (batch_id,),
    )


def recompute_batch_status(conn, batch_id) -> str:
    """Пересчитывает ``batches.status`` по суммам актов и возвращает его.

    Правило contract.md §2.2: нет актов → ``pending``; Σrejected = 0 и
    Σaccepted > 0 → ``accepted``; Σaccepted = 0 и Σrejected > 0 →
    ``rejected``; иначе → ``partial``. Если партии нет — ``ValueError``.
    """
    row = _query_one(
        conn,
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(accepted_qty), 0) AS accepted, "
        "COALESCE(SUM(rejected_qty), 0) AS rejected "
        "FROM inspections WHERE batch_id = ?",
        (batch_id,),
    )
    assert row is not None
    if row["n"] == 0:
        status = "pending"
    elif row["rejected"] == 0 and row["accepted"] > 0:
        status = "accepted"
    elif row["accepted"] == 0 and row["rejected"] > 0:
        status = "rejected"
    else:
        status = "partial"
    cur = conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch_id))
    if cur.rowcount == 0:
        raise ValueError(f"Партия с id={batch_id} не найдена")
    return status


def list_inspections(conn, batch_id) -> list[dict]:
    """Акты ОТК партии в хронологическом порядке."""
    return _query(
        conn,
        "SELECT * FROM inspections WHERE batch_id = ? "
        "ORDER BY inspected_at, id",
        (batch_id,),
    )


def defects_for_inspections(conn, inspection_ids) -> dict[int, list[dict]]:
    """Дефекты актов, сгруппированные по ``inspection_id`` (с полями кода)."""
    ids = list(dict.fromkeys(int(i) for i in inspection_ids))
    result: dict[int, list[dict]] = {iid: [] for iid in ids}
    for part in _chunks(ids, _CHUNK_SIZE):
        placeholders = ",".join("?" * len(part))
        rows = _query(
            conn,
            "SELECT de.id, de.inspection_id, de.defect_id, de.qty, de.is_repeat, "
            "de.note, d.code, d.name, d.group_name, d.severity "
            "FROM defect_events de JOIN defects_ref d ON d.id = de.defect_id "
            f"WHERE de.inspection_id IN ({placeholders}) ORDER BY de.id",
            part,
        )
        for row in rows:
            result[row["inspection_id"]].append(row)
    return result


def measurements_for_inspections(conn, inspection_ids) -> dict[int, list[dict]]:
    """Измерения актов, сгруппированные по ``inspection_id`` (с кодом параметра)."""
    ids = list(dict.fromkeys(int(i) for i in inspection_ids))
    result: dict[int, list[dict]] = {iid: [] for iid in ids}
    for part in _chunks(ids, _CHUNK_SIZE):
        placeholders = ",".join("?" * len(part))
        rows = _query(
            conn,
            "SELECT m.id, m.inspection_id, m.parameter_id, m.value, "
            "p.code, p.name AS parameter_name, p.unit "
            "FROM measurements m JOIN parameters_ref p ON p.id = m.parameter_id "
            f"WHERE m.inspection_id IN ({placeholders}) ORDER BY m.id",
            part,
        )
        for row in rows:
            result[row["inspection_id"]].append(row)
    return result


def create_inspection(conn, *, batch_id: int, draft: InspectionDraft,
                      defects_ref: dict[str, int],
                      params_ref: dict[str, int]) -> dict:
    """Регистрирует акт ОТК в одной транзакции и возвращает его строку.

    Вставляет акт с ``result`` по правилу (rejected = 0 → ``accepted``,
    accepted = 0 → ``rejected``, иначе ``partial``), затем дефекты
    (``is_repeat = 1``, если тот же код уже встречался в предыдущих актах
    ЭТОЙ ЖЕ партии) и измерения по кодам параметров, пересчитывает статус
    партии. Несогласованный черновик или неизвестный код → ``ValueError``
    до начала транзакции; любая ошибка внутри — полный откат.
    """
    if draft.sample_qty <= 0:
        raise ValueError("Количество выборки должно быть больше нуля.")
    if draft.accepted_qty < 0 or draft.rejected_qty < 0:
        raise ValueError("Принято и отклонено не могут быть отрицательными.")
    if draft.accepted_qty + draft.rejected_qty != draft.sample_qty:
        raise ValueError(
            "Сумма принятых и отклонённых единиц должна равняться количеству выборки."
        )
    defect_rows: list[tuple[int, int]] = []
    for code, qty in draft.defects:
        if qty <= 0:
            raise ValueError(f"Дефект «{code}»: количество должно быть больше нуля.")
        defect_id = defects_ref.get(code)
        if defect_id is None:
            raise ValueError(f"Неизвестный код дефекта: «{code}»")
        defect_rows.append((int(defect_id), int(qty)))
    measurement_rows: list[tuple[int, tuple[float, ...]]] = []
    for code, values in draft.measurements:
        parameter_id = params_ref.get(code)
        if parameter_id is None:
            raise ValueError(f"Неизвестный код параметра: «{code}»")
        measurement_rows.append((int(parameter_id), tuple(float(v) for v in values)))
    if draft.rejected_qty == 0:
        result = "accepted"
    elif draft.accepted_qty == 0:
        result = "rejected"
    else:
        result = "partial"

    with _atomic(conn):
        cur = conn.execute(
            "INSERT INTO inspections "
            "(batch_id, inspected_at, inspector, sample_qty, accepted_qty, "
            "rejected_qty, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                draft.inspected_at,
                draft.inspector,
                draft.sample_qty,
                draft.accepted_qty,
                draft.rejected_qty,
                result,
            ),
        )
        inspection_id = int(cur.lastrowid)
        seen = {
            row["defect_id"]
            for row in _query(
                conn,
                "SELECT DISTINCT de.defect_id FROM defect_events de "
                "JOIN inspections i ON i.id = de.inspection_id "
                "WHERE i.batch_id = ?",
                (batch_id,),
            )
        }
        for defect_id, qty in defect_rows:
            conn.execute(
                "INSERT INTO defect_events "
                "(inspection_id, defect_id, qty, is_repeat, note) "
                "VALUES (?, ?, ?, ?, '')",
                (inspection_id, defect_id, qty, 1 if defect_id in seen else 0),
            )
        for parameter_id, values in measurement_rows:
            for value in values:
                conn.execute(
                    "INSERT INTO measurements (inspection_id, parameter_id, value) "
                    "VALUES (?, ?, ?)",
                    (inspection_id, parameter_id, value),
                )
        recompute_batch_status(conn, batch_id)
    row = _query_one(conn, "SELECT * FROM inspections WHERE id = ?", (inspection_id,))
    assert row is not None
    return row


# --- Аналитические выборки (contract.md §3, §4, §7.1) -----------------------

def load_period_facts(conn, *, since: str, until: str) -> dict[int, ProductFacts]:
    """Агрегаты по каждой продукции за период (границы ISO включительно).

    Учитываются акты ОТК с датой в периоде (объёмы, дефекты, повторы,
    измерения в допуске), партии, выпущенные в периоде, и «чистые» партии
    без дефектных событий. Продукция без активности попадает в результат
    с нулями — индекс обязан отличать «нет данных» от нуля.
    """
    period_i = _PERIOD.format(column="i.inspected_at")
    period_b = _PERIOD.format(column="b.produced_at")
    products = _query(conn, "SELECT id, sku, name, category FROM products ORDER BY id")

    sample: dict[int, tuple[int, int]] = {}
    for row in _query(
        conn,
        "SELECT b.product_id AS product_id, SUM(i.sample_qty) AS sample_qty, "
        "SUM(i.rejected_qty) AS rejected_qty "
        f"FROM inspections i JOIN batches b ON b.id = i.batch_id "
        f"WHERE {period_i} GROUP BY b.product_id",
        (since, until),
    ):
        sample[row["product_id"]] = (int(row["sample_qty"]), int(row["rejected_qty"]))

    defects: dict[int, tuple[int, int]] = {}
    for row in _query(
        conn,
        "SELECT b.product_id AS product_id, SUM(de.qty) AS defect_qty, "
        "SUM(CASE WHEN de.is_repeat = 1 THEN de.qty ELSE 0 END) AS repeat_qty "
        "FROM defect_events de "
        "JOIN inspections i ON i.id = de.inspection_id "
        "JOIN batches b ON b.id = i.batch_id "
        f"WHERE {period_i} GROUP BY b.product_id",
        (since, until),
    ):
        defects[row["product_id"]] = (int(row["defect_qty"]), int(row["repeat_qty"]))

    specs: dict[int, tuple[int, int]] = {}
    for row in _query(
        conn,
        "SELECT b.product_id AS product_id, "
        "SUM(CASE WHEN m.value >= p.lsl AND m.value <= p.usl THEN 1 ELSE 0 END) AS in_spec, "
        "COUNT(*) AS with_specs "
        "FROM measurements m "
        "JOIN inspections i ON i.id = m.inspection_id "
        "JOIN batches b ON b.id = i.batch_id "
        "JOIN parameters_ref p ON p.id = m.parameter_id "
        f"WHERE p.lsl IS NOT NULL AND p.usl IS NOT NULL AND {period_i} "
        "GROUP BY b.product_id",
        (since, until),
    ):
        specs[row["product_id"]] = (int(row["in_spec"]), int(row["with_specs"]))

    batches: dict[int, tuple[int, int]] = {}
    for row in _query(
        conn,
        "SELECT b.product_id AS product_id, COUNT(*) AS batches_total, "
        "SUM(CASE WHEN EXISTS ("
        "SELECT 1 FROM inspections i JOIN defect_events de "
        "ON de.inspection_id = i.id "
        f"WHERE i.batch_id = b.id AND {period_i}) THEN 0 ELSE 1 END) AS batches_clean "
        f"FROM batches b WHERE {period_b} GROUP BY b.product_id",
        (since, until, since, until),
    ):
        batches[row["product_id"]] = (int(row["batches_total"]), int(row["batches_clean"]))

    facts: dict[int, ProductFacts] = {}
    for product in products:
        pid = product["id"]
        sample_qty, rejected_qty = sample.get(pid, (0, 0))
        defect_qty, repeat_qty = defects.get(pid, (0, 0))
        in_spec, with_specs = specs.get(pid, (0, 0))
        batches_total, batches_clean = batches.get(pid, (0, 0))
        facts[pid] = ProductFacts(
            product_id=pid,
            sku=product["sku"],
            name=product["name"],
            category=product["category"],
            sample_qty=sample_qty,
            rejected_qty=rejected_qty,
            defect_qty=defect_qty,
            repeat_qty=repeat_qty,
            in_spec=in_spec,
            with_specs=with_specs,
            batches_total=batches_total,
            batches_clean=batches_clean,
        )
    return facts


def load_monthly_facts(conn, *, since: str, until: str) -> list[MonthlyFacts]:
    """Помесячные агрегаты (``YYYY-MM``) только по месяцам с данными, по возрастанию.

    ``defect_rate`` — rejected/sample за месяц, ``None`` при нулевой выборке;
    ``accepted_batches`` — партии текущего статуса ``accepted``, выпущенные
    в месяце.
    """
    period_i = _PERIOD.format(column="i.inspected_at")
    period_b = _PERIOD.format(column="b.produced_at")
    inspections: dict[str, dict] = {}
    for row in _query(
        conn,
        "SELECT substr(i.inspected_at, 1, 7) AS month, COUNT(*) AS inspections, "
        "SUM(i.sample_qty) AS sample_qty, SUM(i.rejected_qty) AS rejected_qty "
        f"FROM inspections i WHERE {period_i} GROUP BY month",
        (since, until),
    ):
        inspections[row["month"]] = row
    batches: dict[str, dict] = {}
    for row in _query(
        conn,
        "SELECT substr(b.produced_at, 1, 7) AS month, COUNT(*) AS batches, "
        "SUM(CASE WHEN b.status = 'accepted' THEN 1 ELSE 0 END) AS accepted_batches "
        f"FROM batches b WHERE {period_b} GROUP BY month",
        (since, until),
    ):
        batches[row["month"]] = row

    result: list[MonthlyFacts] = []
    for month in sorted(set(inspections) | set(batches)):
        ins = inspections.get(month, {})
        bat = batches.get(month, {})
        sample_qty = int(ins.get("sample_qty") or 0)
        rejected_qty = int(ins.get("rejected_qty") or 0)
        result.append(
            MonthlyFacts(
                month=month,
                batches=int(bat.get("batches") or 0),
                inspections=int(ins.get("inspections") or 0),
                sample_qty=sample_qty,
                rejected_qty=rejected_qty,
                defect_rate=(rejected_qty / sample_qty) if sample_qty else None,
                accepted_batches=int(bat.get("accepted_batches") or 0),
            )
        )
    return result


def load_pareto_counts(conn, *, since: str, until: str) -> list[DefectCount]:
    """Количество дефектов за период: только qty > 0, по убыванию, затем код."""
    period_i = _PERIOD.format(column="i.inspected_at")
    rows = _query(
        conn,
        "SELECT d.code, d.name, d.group_name, d.severity, SUM(de.qty) AS qty "
        "FROM defect_events de "
        "JOIN inspections i ON i.id = de.inspection_id "
        "JOIN defects_ref d ON d.id = de.defect_id "
        f"WHERE {period_i} GROUP BY d.id "
        "HAVING SUM(de.qty) > 0 ORDER BY qty DESC, d.code",
        (since, until),
    )
    return [
        DefectCount(
            code=row["code"],
            name=row["name"],
            group_name=row["group_name"],
            severity=int(row["severity"]),
            qty=int(row["qty"]),
        )
        for row in rows
    ]


def load_spc_subgroups(conn, *, parameter_id: int, since: str, until: str) -> list[SpcSubgroup]:
    """Подгруппы SPC: измерения одного акта по одному параметру (§4).

    Порядок подгрупп — по ``inspected_at, id`` акта; значения внутри
    подгруппы — по id измерения; метка — номер партии.
    """
    period_i = _PERIOD.format(column="i.inspected_at")
    rows = _query(
        conn,
        "SELECT m.id AS measurement_id, m.value, i.id AS inspection_id, "
        "i.inspected_at, b.batch_no "
        "FROM measurements m "
        "JOIN inspections i ON i.id = m.inspection_id "
        "JOIN batches b ON b.id = i.batch_id "
        f"WHERE m.parameter_id = ? AND {period_i} "
        "ORDER BY i.inspected_at, i.id, m.id",
        (parameter_id, since, until),
    )
    subgroups: list[SpcSubgroup] = []
    current_id: int | None = None
    values: list[float] = []
    label = ""
    for row in rows:
        if row["inspection_id"] != current_id:
            if current_id is not None:
                subgroups.append(
                    SpcSubgroup(
                        subgroup_no=len(subgroups) + 1,
                        label=label,
                        values=tuple(values),
                    )
                )
            current_id = row["inspection_id"]
            label = row["batch_no"]
            values = []
        values.append(float(row["value"]))
    if current_id is not None:
        subgroups.append(
            SpcSubgroup(subgroup_no=len(subgroups) + 1, label=label, values=tuple(values))
        )
    return subgroups


def dashboard_stats(conn, *, since: str, until: str) -> dict:
    """Сводные KPI главной страницы — ровно ключи contract.md §7.1.

    Партии считаются по дате выпуска в периоде; ``accepted_share`` по §2.2
    (принятые / (всего − ожидают), ``None`` при пустом знаменателе),
    ``defect_rate`` — rejected/sample актов периода (``None`` при нулевой
    выборке). ``index_overall`` всегда ``None``: расчёт индекса принадлежит
    слою анализа, а не репозиторию.
    """
    period_b = _PERIOD.format(column="b.produced_at")
    period_i = _PERIOD.format(column="i.inspected_at")

    batches = _query_one(
        conn,
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN b.status = 'pending' THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN b.status = 'accepted' THEN 1 ELSE 0 END) AS accepted "
        f"FROM batches b WHERE {period_b}",
        (since, until),
    ) or {"total": 0, "pending": 0, "accepted": 0}
    total = int(batches["total"] or 0)
    pending = int(batches["pending"] or 0)
    accepted = int(batches["accepted"] or 0)
    decided = total - pending
    accepted_share = (accepted / decided) if decided > 0 else None

    inspections = _query_one(
        conn,
        "SELECT COUNT(*) AS n, COALESCE(SUM(sample_qty), 0) AS sample_qty, "
        "COALESCE(SUM(rejected_qty), 0) AS rejected_qty "
        f"FROM inspections i WHERE {period_i}",
        (since, until),
    ) or {"n": 0, "sample_qty": 0, "rejected_qty": 0}
    sample_qty = int(inspections["sample_qty"] or 0)
    defect_rate = (
        int(inspections["rejected_qty"] or 0) / sample_qty if sample_qty else None
    )

    top = load_pareto_counts(conn, since=since, until=until)
    parameters_critical = _query_one(
        conn, "SELECT COUNT(*) AS n FROM parameters_ref WHERE is_critical = 1"
    )
    products_active = _query_one(
        conn, "SELECT COUNT(*) AS n FROM products WHERE is_active = 1"
    )

    return {
        "batches_total": total,
        "batches_pending": pending,
        "accepted_share": accepted_share,
        "defect_rate": defect_rate,
        "index_overall": None,
        "top_defect_code": top[0].code if top else None,
        "top_defect_name": top[0].name if top else None,
        "top_defect_qty": top[0].qty if top else 0,
        "parameters_critical": int(parameters_critical["n"]) if parameters_critical else 0,
        "products_active": int(products_active["n"]) if products_active else 0,
        "inspections_count": int(inspections["n"] or 0),
    }


# --- Веса индекса качества --------------------------------------------------

_WEIGHT_FIELDS = ("w_defect", "w_spec", "w_repeat", "w_clean")
_CAP_FIELDS = ("defect_rate_cap", "repeat_rate_cap")
_UPDATABLE_FIELDS = _WEIGHT_FIELDS + _CAP_FIELDS + ("min_sample",)


def _weights_from_row(row: dict) -> IndexWeights:
    """Собирает ``IndexWeights`` из строки ``quality_index_config``."""
    return IndexWeights(
        version=int(row["version"]),
        w_defect=float(row["w_defect"]),
        w_spec=float(row["w_spec"]),
        w_repeat=float(row["w_repeat"]),
        w_clean=float(row["w_clean"]),
        defect_rate_cap=float(row["defect_rate_cap"]),
        repeat_rate_cap=float(row["repeat_rate_cap"]),
        min_sample=int(row["min_sample"]),
        updated_at=str(row["updated_at"]),
        updated_by=str(row["updated_by"]),
    )


def get_weights(conn) -> IndexWeights:
    """Веса индекса; при отсутствии строки (id = 1) вставляет базовую (§7.1)."""
    conn.execute(
        "INSERT OR IGNORE INTO quality_index_config "
        "(id, version, w_defect, w_spec, w_repeat, w_clean, defect_rate_cap, "
        "repeat_rate_cap, min_sample, notes, updated_at, updated_by) "
        "VALUES (1, 1, 0.40, 0.30, 0.15, 0.15, 0.10, 0.20, 10, '', ?, 'system')",
        (_now_iso(),),
    )
    row = _query_one(conn, "SELECT * FROM quality_index_config WHERE id = 1")
    assert row is not None
    return _weights_from_row(row)


def update_weights(conn, *, author: str, **fields) -> IndexWeights:
    """Обновляет веса/нормативы одной записью, ``version + 1`` (§7.1).

    Валидация (нарушение → ``ValueError``): неизвестное поле; веса ≥ 0 и
    Σвесов ≈ 1 (|Δ| < 1e-6); нормативы > 0; ``min_sample`` ≥ 0; непустой
    ``author``. ``updated_at`` — текущий ISO-moment, ``updated_by`` — author.
    """
    author_text = str(author or "").strip()
    if not author_text:
        raise ValueError("Укажите автора настроек.")

    unknown = [key for key in fields if key not in _UPDATABLE_FIELDS]
    if unknown:
        raise ValueError(f"Неизвестные поля настроек: {', '.join(sorted(unknown))}")

    current = get_weights(conn)
    values: dict[str, float | int] = {
        field: getattr(current, field) for field in _UPDATABLE_FIELDS
    }
    values.update(fields)
    try:
        numeric: dict[str, float] = {
            field: float(values[field]) for field in _WEIGHT_FIELDS + _CAP_FIELDS
        }
        min_sample = int(values["min_sample"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Некорректное значение настроек: {exc}") from exc

    weights_total = sum(numeric[field] for field in _WEIGHT_FIELDS)
    if any(numeric[field] < 0 for field in _WEIGHT_FIELDS):
        raise ValueError("Веса не могут быть отрицательными.")
    if any(not math.isfinite(numeric[field]) for field in _WEIGHT_FIELDS):
        raise ValueError("Веса должны быть конечными числами.")
    if abs(weights_total - 1.0) >= 1e-6:
        raise ValueError(
            f"Сумма четырёх весов должна быть равна 1 (сейчас: {weights_total:.6f})."
        )
    for field, label in (("defect_rate_cap", "Порог дефектности"),
                         ("repeat_rate_cap", "Порог повторяемости")):
        if not math.isfinite(numeric[field]):
            raise ValueError(f"{label} должен быть конечным числом.")
        if numeric[field] <= 0:
            raise ValueError(f"{label} должен быть больше нуля.")
    if min_sample < 0:
        raise ValueError("Минимальный размер выборки не может быть отрицательным.")

    conn.execute(
        "UPDATE quality_index_config SET version = version + 1, "
        "w_defect = ?, w_spec = ?, w_repeat = ?, w_clean = ?, "
        "defect_rate_cap = ?, repeat_rate_cap = ?, min_sample = ?, "
        "updated_at = ?, updated_by = ? WHERE id = 1",
        (
            numeric["w_defect"],
            numeric["w_spec"],
            numeric["w_repeat"],
            numeric["w_clean"],
            numeric["defect_rate_cap"],
            numeric["repeat_rate_cap"],
            min_sample,
            _now_iso(),
            author_text,
        ),
    )
    row = _query_one(conn, "SELECT * FROM quality_index_config WHERE id = 1")
    assert row is not None
    return _weights_from_row(row)
