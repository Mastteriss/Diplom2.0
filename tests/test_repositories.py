"""Юнит-тесты репозиторного слоя B1 (``app/db/repositories.py``).

Проверяются ровно те свойства, что зафиксированы в ``docs/contract.md``:
§2 (схема и правила согласованности), §7.1 (контракт функций репозитория) и
соглашения о тестовых БД из §9. Каждый тест открывает своё соединение через
``app.db.connection.connect``, сам его закрывает, а временные файлы складывает
в ``tests_artifacts/`` с уникальным именем. Удаление — best-effort и никогда
не роняет тест. Тесты детерминированы и не опираются на поставляемую БД, за
исключением отдельной read-only проверки целостности ``data/quality.sqlite3``.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from app.config import PROJECT_DIR
from app.db import repositories as repo
from app.db.connection import connect, init_db, schema_version
from app.domain.models import (
    DefectCount,
    IndexWeights,
    InspectionDraft,
    MonthlyFacts,
    ProductFacts,
    SpcSubgroup,
)

ARTIFACTS_DIR = PROJECT_DIR / "tests_artifacts"
SHIPPED_DB = PROJECT_DIR / "data" / "quality.sqlite3"


# --- Инфраструктура тестов -------------------------------------------------

def _cleanup(path: Path) -> None:
    """Best-effort удаление файлов тестовой БД вместе с WAL/SHM.

    Ошибки удаления намеренно проглатываются: на Windows файл остаётся
    залоченным, если соединение не закрылось, но это не должно ронять тест.
    """
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass


@pytest.fixture()
def db():
    """Свежая временная БД в ``tests_artifacts/`` + открытое соединение."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"test_repos_{uuid.uuid4().hex}.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        yield conn, path
    finally:
        try:
            conn.close()
        except Exception:
            pass
        _cleanup(path)


def _add_product(conn, sku, name, category="", unit="шт.", active=1):
    cur = conn.execute(
        "INSERT INTO products (sku, name, category, unit, is_active, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (sku, name, category, unit, active),
    )
    return cur.lastrowid


def _add_parameter(conn, code, name, unit="", target=None, lsl=None, usl=None,
                   critical=0):
    cur = conn.execute(
        "INSERT INTO parameters_ref (code, name, unit, target, lsl, usl, is_critical) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, name, unit, target, lsl, usl, critical),
    )
    return cur.lastrowid


def _add_defect(conn, code, name, group_name="Прочее", severity=3, active=1):
    cur = conn.execute(
        "INSERT INTO defects_ref (code, name, group_name, severity, is_active) "
        "VALUES (?, ?, ?, ?, ?)",
        (code, name, group_name, severity, active),
    )
    return cur.lastrowid


def _add_batch(conn, product_id, batch_no, produced_at, line="Линия-1",
               produced_qty=100, status="pending"):
    cur = conn.execute(
        "INSERT INTO batches (product_id, batch_no, line, produced_qty, produced_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, batch_no, line, produced_qty, produced_at, status),
    )
    return cur.lastrowid


def _add_inspection(conn, batch_id, inspected_at, sample_qty, accepted_qty,
                    rejected_qty, result, inspector="Иванов"):
    cur = conn.execute(
        "INSERT INTO inspections (batch_id, inspected_at, inspector, sample_qty, "
        "accepted_qty, rejected_qty, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, inspected_at, inspector, sample_qty, accepted_qty,
         rejected_qty, result),
    )
    return cur.lastrowid


def _add_defect_event(conn, inspection_id, defect_id, qty, is_repeat=0):
    cur = conn.execute(
        "INSERT INTO defect_events (inspection_id, defect_id, qty, is_repeat, note) "
        "VALUES (?, ?, ?, ?, '')",
        (inspection_id, defect_id, qty, is_repeat),
    )
    return cur.lastrowid


def _add_measurements(conn, inspection_id, parameter_id, values):
    for value in values:
        conn.execute(
            "INSERT INTO measurements (inspection_id, parameter_id, value) "
            "VALUES (?, ?, ?)",
            (inspection_id, parameter_id, value),
        )


def _draft(inspected_at, sample, accepted, rejected, defects=(), measurements=()):
    return InspectionDraft(
        inspected_at=inspected_at,
        inspector="Иванов",
        sample_qty=sample,
        accepted_qty=accepted,
        rejected_qty=rejected,
        defects=tuple(defects),
        measurements=tuple(measurements),
    )


def _seed_analytics(conn):
    """Детерминированный набор данных за январь–март 2026 + акт за декабрь 2025."""
    p1 = _add_product(conn, "SKU-A", "Alpha", "CatA")
    p2 = _add_product(conn, "SKU-B", "Beta", "CatB")
    pa = _add_parameter(conn, "PA", "Param A", "мм", 5.0, 0.0, 10.0, critical=1)
    pb = _add_parameter(conn, "PB", "Param B", "мм", critical=0)
    d_a = _add_defect(conn, "DA", "Defect A", "Размеры", 3)
    d_b = _add_defect(conn, "DB", "Defect B", "Прочее", 2)

    b1 = _add_batch(conn, p1, "B1", "2026-01-05T09:00:00+00:00", status="accepted")
    b2 = _add_batch(conn, p1, "B2", "2026-02-10T09:00:00+00:00", status="partial")
    b3 = _add_batch(conn, p1, "B3", "2026-03-15T09:00:00+00:00", status="pending")

    i1 = _add_inspection(conn, b1, "2026-01-06T08:00:00+00:00", 10, 8, 2, "partial")
    _add_defect_event(conn, i1, d_a, 2, is_repeat=1)
    _add_defect_event(conn, i1, d_b, 1, is_repeat=0)
    _add_measurements(conn, i1, pa, [1.0, 2.0, 3.0, 4.0, 20.0])  # 4 в допуске, 1 вне
    _add_measurements(conn, i1, pb, [50.0, 51.0, 52.0, 53.0, 54.0])

    i2 = _add_inspection(conn, b2, "2026-02-11T08:00:00+00:00", 10, 5, 5, "partial")
    _add_defect_event(conn, i2, d_a, 4, is_repeat=0)
    _add_measurements(conn, i2, pa, [5.0, 6.0, 7.0, 8.0, 9.0])  # 5 в допуске

    # Акт вне периода: не должен попадать ни в одну выборку за 2026 год.
    i3 = _add_inspection(conn, b1, "2025-12-31T08:00:00+00:00", 10, 9, 1, "partial")
    _add_defect_event(conn, i3, d_a, 100, is_repeat=0)
    _add_measurements(conn, i3, pa, [1.0, 1.0, 1.0, 1.0, 1.0])

    return {
        "p1": p1, "p2": p2, "pa": pa, "pb": pb, "da": d_a, "db": d_b,
        "b1": b1, "b2": b2, "b3": b3, "i1": i1, "i2": i2, "i3": i3,
    }


# --- §2: схема и версия ----------------------------------------------------

def test_init_db_sets_schema_version_and_is_idempotent(db):
    conn, path = db
    assert schema_version(conn) == 1

    init_db(path)
    init_db(path)

    versions = [
        row["version"]
        for row in conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    ]
    assert versions == [1]
    assert schema_version(conn) == 1


# --- §7.1: справочник продукции -------------------------------------------

def test_list_products_filters_counts_and_categories(db):
    conn, _ = db
    p1 = _add_product(conn, "SKU-1", "Bolt M8", "Крепёж", active=1)
    _add_product(conn, "SKU-2", "Nut M8", "Крепёж", active=0)
    _add_product(conn, "SKU-3", "Washer", "Прочее", active=1)
    _add_product(conn, "SKU-4", "No Category", "", active=0)

    all_rows = repo.list_products(conn)
    assert [r["sku"] for r in all_rows] == ["SKU-1", "SKU-2", "SKU-3", "SKU-4"]
    assert repo.count_products(conn) == 4

    bolt = repo.list_products(conn, q="bolt")
    assert [r["sku"] for r in bolt] == ["SKU-1"]
    assert repo.count_products(conn, q="bolt") == 1

    by_sku = repo.list_products(conn, q="SKU-2")
    assert [r["sku"] for r in by_sku] == ["SKU-2"]

    assert repo.list_products(conn, q="нет-такого") == []
    assert repo.count_products(conn, q="нет-такого") == 0

    fasteners = repo.list_products(conn, category="Крепёж")
    assert [r["sku"] for r in fasteners] == ["SKU-1", "SKU-2"]
    assert repo.count_products(conn, category="Крепёж") == 2

    active = repo.list_products(conn, active_only=True)
    assert [r["sku"] for r in active] == ["SKU-1", "SKU-3"]
    assert repo.count_products(conn, active_only=True) == 2

    combo = repo.list_products(conn, category="Крепёж", active_only=True)
    assert [r["sku"] for r in combo] == ["SKU-1"]
    assert repo.count_products(conn, category="Крепёж", active_only=True) == 1

    assert [r["sku"] for r in repo.list_products(conn, limit=2)] == ["SKU-1", "SKU-2"]
    assert [r["sku"] for r in repo.list_products(conn, limit=2, offset=1)] == ["SKU-2", "SKU-3"]
    assert repo.list_products(conn, limit=2, offset=10) == []
    assert repo.list_products(conn, limit=-1, offset=-5) == []

    product = repo.get_product(conn, p1)
    assert product["sku"] == "SKU-1"
    assert product["name"] == "Bolt M8"
    assert product["category"] == "Крепёж"
    assert repo.get_product(conn, 99999) is None

    # пустая категория не попадает в список фильтра
    assert repo.list_categories(conn) == ["Крепёж", "Прочее"]


# --- §7.1: справочник дефектов --------------------------------------------

def test_list_defects_and_get_by_code(db):
    conn, _ = db
    _add_defect(conn, "D-01", "Царапина", "Внешний вид", 2, active=1)
    d2 = _add_defect(conn, "D-02", "Трещина", "Прочность", 5, active=0)

    active = repo.list_defects(conn, active_only=True)
    assert [r["code"] for r in active] == ["D-01"]
    assert active[0]["name"] == "Царапина"

    everything = repo.list_defects(conn, active_only=False)
    assert [r["code"] for r in everything] == ["D-01", "D-02"]

    got = repo.get_defect_by_code(conn, "D-02")
    assert got["id"] == d2
    assert got["severity"] == 5
    assert got["is_active"] == 0
    assert repo.get_defect_by_code(conn, "NOPE") is None


# --- §7.1: справочник параметров ------------------------------------------

def test_list_parameters_ordering_and_lookup(db):
    conn, _ = db
    _add_parameter(conn, "BORE", "Диаметр", "мм", 10.0, 9.5, 10.5, critical=1)
    _add_parameter(conn, "ANG", "Угол", "град", critical=0)
    _add_parameter(conn, "LEN", "Длина", "мм", critical=0)

    critical_first = repo.list_parameters(conn)
    assert [r["code"] for r in critical_first] == ["BORE", "ANG", "LEN"]

    by_code = repo.list_parameters(conn, critical_first=False)
    assert [r["code"] for r in by_code] == ["ANG", "BORE", "LEN"]

    param = repo.get_parameter_by_code(conn, "BORE")
    assert param["name"] == "Диаметр"
    assert param["unit"] == "мм"
    assert param["lsl"] == 9.5
    assert param["usl"] == 10.5
    assert param["is_critical"] == 1
    assert repo.get_parameter_by_code(conn, "ZZZ") is None


# --- §7.1: партии ----------------------------------------------------------

def test_list_batches_fields_filters_and_get(db):
    conn, _ = db
    p1 = _add_product(conn, "SKU-A", "Alpha")
    p2 = _add_product(conn, "SKU-B", "Beta")
    b1 = _add_batch(conn, p1, "B-001", "2026-01-10T08:00:00+00:00", status="accepted")
    b2 = _add_batch(conn, p1, "B-002", "2026-02-10T08:00:00+00:00", status="pending")
    b3 = _add_batch(conn, p2, "B-003", "2026-03-10T08:00:00+00:00", status="rejected")
    _add_inspection(conn, b1, "2026-01-11T08:00:00+00:00", 10, 10, 0, "accepted")
    _add_inspection(conn, b1, "2026-01-12T08:00:00+00:00", 10, 10, 0, "accepted")

    rows = repo.list_batches(conn)
    assert [r["id"] for r in rows] == [b3, b2, b1]  # produced_at DESC
    by_id = {r["id"]: r for r in rows}
    assert by_id[b1]["product_sku"] == "SKU-A"
    assert by_id[b1]["product_name"] == "Alpha"
    assert by_id[b1]["inspections_count"] == 2
    assert by_id[b3]["inspections_count"] == 0

    assert repo.count_batches(conn) == 3
    assert {r["id"] for r in repo.list_batches(conn, product_id=p1)} == {b1, b2}
    assert repo.count_batches(conn, product_id=p1) == 2
    assert [r["id"] for r in repo.list_batches(conn, status="pending")] == [b2]
    assert repo.count_batches(conn, status="pending") == 1
    assert repo.count_batches(conn, product_id=p2, status="pending") == 0

    assert [r["id"] for r in repo.list_batches(conn, limit=1)] == [b3]
    assert [r["id"] for r in repo.list_batches(conn, limit=1, offset=1)] == [b2]

    batch = repo.get_batch(conn, b1)
    assert batch["batch_no"] == "B-001"
    assert batch["product_sku"] == "SKU-A"
    assert batch["product_name"] == "Alpha"
    assert batch["product_category"] == ""
    assert batch["line"] == "Линия-1"
    assert repo.get_batch(conn, 99999) is None


def test_recompute_batch_status_all_four_states(db):
    conn, _ = db
    p = _add_product(conn, "SKU-R", "Recompute")
    no_act = _add_batch(conn, p, "R-0", "2026-01-01T00:00:00+00:00")
    acc = _add_batch(conn, p, "R-1", "2026-01-02T00:00:00+00:00")
    rej = _add_batch(conn, p, "R-2", "2026-01-03T00:00:00+00:00")
    part = _add_batch(conn, p, "R-3", "2026-01-04T00:00:00+00:00")
    mixed = _add_batch(conn, p, "R-4", "2026-01-05T00:00:00+00:00")

    _add_inspection(conn, acc, "2026-01-02T01:00:00+00:00", 10, 10, 0, "accepted")
    _add_inspection(conn, rej, "2026-01-03T01:00:00+00:00", 10, 0, 10, "rejected")
    _add_inspection(conn, part, "2026-01-04T01:00:00+00:00", 10, 6, 4, "partial")
    _add_inspection(conn, mixed, "2026-01-05T01:00:00+00:00", 10, 10, 0, "accepted")
    _add_inspection(conn, mixed, "2026-01-05T02:00:00+00:00", 10, 0, 10, "rejected")

    assert repo.recompute_batch_status(conn, no_act) == "pending"
    assert repo.recompute_batch_status(conn, acc) == "accepted"
    assert repo.recompute_batch_status(conn, rej) == "rejected"
    assert repo.recompute_batch_status(conn, part) == "partial"
    assert repo.recompute_batch_status(conn, mixed) == "partial"

    persisted = {
        row["id"]: row["status"]
        for row in conn.execute("SELECT id, status FROM batches").fetchall()
    }
    assert persisted == {
        no_act: "pending",
        acc: "accepted",
        rej: "rejected",
        part: "partial",
        mixed: "partial",
    }

    # повторный вызов при уже корректном статусе не должен падать (rowcount = 0)
    assert repo.recompute_batch_status(conn, acc) == "accepted"

    with pytest.raises(ValueError):
        repo.recompute_batch_status(conn, 999999)


# --- §7.1 / §2.2: регистрация акта ОТК ------------------------------------

def test_create_inspection_computes_result_and_persists(db):
    conn, _ = db
    p = _add_product(conn, "SKU-I", "Inspected")
    d1 = _add_defect(conn, "D-01", "Царапина")
    _add_defect(conn, "D-02", "Трещина")
    pa = _add_parameter(conn, "BORE", "Диаметр", "мм", 10.0, 9.5, 10.5, critical=1)
    pb = _add_parameter(conn, "LEN", "Длина", "мм", 50.0, 49.0, 51.0, critical=0)
    batch = _add_batch(conn, p, "I-1", "2026-04-01T08:00:00+00:00")

    draft = _draft(
        "2026-04-02T08:00:00+00:00", 10, 7, 3,
        defects=(("D-01", 2),),
        measurements=(
            ("BORE", (1.0, 2.0, 3.0, 4.0, 5.0)),
            ("LEN", (48.0, 49.0, 50.0, 51.0, 52.0)),
        ),
    )
    row = repo.create_inspection(
        conn, batch_id=batch, draft=draft,
        defects_ref={"D-01": d1}, params_ref={"BORE": pa, "LEN": pb},
    )

    assert isinstance(row, dict)
    assert row["batch_id"] == batch
    assert row["result"] == "partial"
    assert (row["sample_qty"], row["accepted_qty"], row["rejected_qty"]) == (10, 7, 3)
    assert row["inspector"] == "Иванов"
    inspection_id = row["id"]

    counts = {
        r["parameter_id"]: r["n"]
        for r in conn.execute(
            "SELECT parameter_id, COUNT(*) AS n FROM measurements "
            "WHERE inspection_id = ? GROUP BY parameter_id",
            (inspection_id,),
        )
    }
    assert counts == {pa: 5, pb: 5}

    event = conn.execute(
        "SELECT defect_id, qty, is_repeat FROM defect_events WHERE inspection_id = ?",
        (inspection_id,),
    ).fetchone()
    assert event["defect_id"] == d1
    assert event["qty"] == 2
    assert event["is_repeat"] == 0

    status = conn.execute(
        "SELECT status FROM batches WHERE id = ?", (batch,)
    ).fetchone()["status"]
    assert status == "partial"


def test_create_inspection_result_accepted_and_rejected(db):
    conn, _ = db
    p = _add_product(conn, "SKU-O", "Outcomes")
    b_acc = _add_batch(conn, p, "O-1", "2026-04-10T08:00:00+00:00")
    b_rej = _add_batch(conn, p, "O-2", "2026-04-11T08:00:00+00:00")

    acc = repo.create_inspection(
        conn, batch_id=b_acc,
        draft=_draft("2026-04-10T09:00:00+00:00", 10, 10, 0),
        defects_ref={}, params_ref={},
    )
    assert acc["result"] == "accepted"
    assert conn.execute(
        "SELECT status FROM batches WHERE id = ?", (b_acc,)
    ).fetchone()["status"] == "accepted"

    rej = repo.create_inspection(
        conn, batch_id=b_rej,
        draft=_draft("2026-04-11T09:00:00+00:00", 10, 0, 10),
        defects_ref={}, params_ref={},
    )
    assert rej["result"] == "rejected"
    assert conn.execute(
        "SELECT status FROM batches WHERE id = ?", (b_rej,)
    ).fetchone()["status"] == "rejected"


def test_create_inspection_is_repeat_rule_is_per_batch(db):
    conn, _ = db
    p = _add_product(conn, "SKU-RP", "Repeat")
    d1 = _add_defect(conn, "D-01", "Повторный")
    d2 = _add_defect(conn, "D-02", "Новый")
    pa = _add_parameter(conn, "BORE", "Диаметр", "мм", 10.0, 9.0, 11.0, critical=1)
    defects_ref = {"D-01": d1, "D-02": d2}
    params_ref = {"BORE": pa}
    batch = _add_batch(conn, p, "RP-1", "2026-05-01T08:00:00+00:00")
    other = _add_batch(conn, p, "RP-2", "2026-05-02T08:00:00+00:00")

    first = repo.create_inspection(
        conn, batch_id=batch,
        draft=_draft("2026-05-01T09:00:00+00:00", 10, 8, 2,
                     defects=(("D-01", 2),),
                     measurements=(("BORE", (1, 2, 3, 4, 5)),)),
        defects_ref=defects_ref, params_ref=params_ref,
    )
    second = repo.create_inspection(
        conn, batch_id=batch,
        draft=_draft("2026-05-02T09:00:00+00:00", 10, 7, 3,
                     defects=(("D-01", 3), ("D-02", 1)),
                     measurements=(("BORE", (2, 3, 4, 5, 6)),)),
        defects_ref=defects_ref, params_ref=params_ref,
    )
    third = repo.create_inspection(
        conn, batch_id=batch,
        draft=_draft("2026-05-03T09:00:00+00:00", 10, 8, 2,
                     defects=(("D-02", 1),),
                     measurements=(("BORE", (3, 4, 5, 6, 7)),)),
        defects_ref=defects_ref, params_ref=params_ref,
    )
    elsewhere = repo.create_inspection(
        conn, batch_id=other,
        draft=_draft("2026-05-04T09:00:00+00:00", 10, 8, 2,
                     defects=(("D-01", 2),),
                     measurements=(("BORE", (1, 1, 1, 1, 1)),)),
        defects_ref=defects_ref, params_ref=params_ref,
    )

    grouped = repo.defects_for_inspections(
        conn, [first["id"], second["id"], third["id"], elsewhere["id"]]
    )

    first_events = grouped[first["id"]]
    assert [(e["code"], e["is_repeat"]) for e in first_events] == [("D-01", 0)]

    second_events = {e["code"]: e for e in grouped[second["id"]]}
    assert second_events["D-01"]["is_repeat"] == 1  # был в предыдущем акте партии
    assert second_events["D-02"]["is_repeat"] == 0  # впервые в этой партии

    third_events = {e["code"]: e for e in grouped[third["id"]]}
    assert third_events["D-02"]["is_repeat"] == 1  # уже был во втором акте

    # тот же код в ДРУГОЙ партии не является повтором
    assert grouped[elsewhere["id"]][0]["is_repeat"] == 0


def test_inspections_check_constraint_is_enforced(db):
    conn, _ = db
    p = _add_product(conn, "SKU-C", "Check")
    batch = _add_batch(conn, p, "C-1", "2026-06-01T00:00:00+00:00")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inspections (batch_id, inspected_at, inspector, "
            "sample_qty, accepted_qty, rejected_qty, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch, "2026-06-02T00:00:00+00:00", "X", 10, 5, 3, "partial"),
        )

    ok = conn.execute(
        "INSERT INTO inspections (batch_id, inspected_at, inspector, "
        "sample_qty, accepted_qty, rejected_qty, result) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch, "2026-06-03T00:00:00+00:00", "X", 10, 6, 4, "partial"),
    )
    assert ok.lastrowid > 0


def test_create_inspection_validation_and_rollback(db):
    conn, _ = db
    p = _add_product(conn, "SKU-V", "Validate")
    d1 = _add_defect(conn, "D-01", "D")
    pa = _add_parameter(conn, "BORE", "B", "мм", 10.0, 9.0, 11.0, critical=1)
    batch = _add_batch(conn, p, "V-1", "2026-07-01T00:00:00+00:00")
    defects_ref = {"D-01": d1}
    params_ref = {"BORE": pa}

    def inspection_count():
        return conn.execute("SELECT COUNT(*) AS n FROM inspections").fetchone()["n"]

    before = inspection_count()

    with pytest.raises(ValueError):  # sample_qty <= 0
        repo.create_inspection(
            conn, batch_id=batch,
            draft=_draft("2026-07-02T00:00:00+00:00", 0, 0, 0),
            defects_ref=defects_ref, params_ref=params_ref,
        )
    with pytest.raises(ValueError):  # отрицательное accepted_qty
        repo.create_inspection(
            conn, batch_id=batch,
            draft=_draft("2026-07-02T00:00:00+00:00", 10, -1, 11),
            defects_ref=defects_ref, params_ref=params_ref,
        )
    with pytest.raises(ValueError):  # accepted + rejected != sample
        repo.create_inspection(
            conn, batch_id=batch,
            draft=_draft("2026-07-02T00:00:00+00:00", 10, 5, 4),
            defects_ref=defects_ref, params_ref=params_ref,
        )
    with pytest.raises(ValueError):  # неизвестный код дефекта
        repo.create_inspection(
            conn, batch_id=batch,
            draft=_draft("2026-07-02T00:00:00+00:00", 10, 8, 2,
                         defects=(("NOPE", 1),)),
            defects_ref=defects_ref, params_ref=params_ref,
        )
    with pytest.raises(ValueError):  # qty дефекта <= 0
        repo.create_inspection(
            conn, batch_id=batch,
            draft=_draft("2026-07-02T00:00:00+00:00", 10, 8, 2,
                         defects=(("D-01", 0),)),
            defects_ref=defects_ref, params_ref=params_ref,
        )
    with pytest.raises(ValueError):  # неизвестный код параметра
        repo.create_inspection(
            conn, batch_id=batch,
            draft=_draft("2026-07-02T00:00:00+00:00", 10, 8, 2,
                         measurements=(("NOPE", (1, 2, 3, 4, 5)),)),
            defects_ref=defects_ref, params_ref=params_ref,
        )

    assert inspection_count() == before

    # ошибка ВНУТРИ транзакции (несуществующая партия → FK) откатывается
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_inspection(
            conn, batch_id=999999,
            draft=_draft("2026-07-03T00:00:00+00:00", 10, 8, 2),
            defects_ref=defects_ref, params_ref=params_ref,
        )
    assert inspection_count() == before


# --- §7.1: группировка дефектов и измерений по актам ----------------------

def test_grouping_helpers_key_by_inspection_id(db):
    conn, _ = db
    p = _add_product(conn, "SKU-G", "Group")
    d1 = _add_defect(conn, "D-01", "D")
    pa = _add_parameter(conn, "BORE", "B", "мм", 10.0, 9.0, 11.0, critical=1)
    batch = _add_batch(conn, p, "G-1", "2026-08-01T00:00:00+00:00")
    defects_ref = {"D-01": d1}
    params_ref = {"BORE": pa}

    with_data = repo.create_inspection(
        conn, batch_id=batch,
        draft=_draft("2026-08-02T00:00:00+00:00", 10, 8, 2,
                     defects=(("D-01", 2),),
                     measurements=(("BORE", (1, 2, 3, 4, 5)),)),
        defects_ref=defects_ref, params_ref=params_ref,
    )
    without_data_batch = _add_batch(conn, p, "G-2", "2026-08-03T00:00:00+00:00")
    without_data = repo.create_inspection(
        conn, batch_id=without_data_batch,
        draft=_draft("2026-08-04T00:00:00+00:00", 10, 10, 0),
        defects_ref=defects_ref, params_ref=params_ref,
    )

    ids = [with_data["id"], without_data["id"], 999999]

    defects = repo.defects_for_inspections(conn, ids)
    assert set(defects) == set(ids)
    assert isinstance(defects[with_data["id"]], list)
    event = defects[with_data["id"]][0]
    assert event["code"] == "D-01"
    assert event["name"] == "D"
    assert event["group_name"] == "Прочее"
    assert event["severity"] == 3
    assert event["qty"] == 2
    assert defects[without_data["id"]] == []
    assert defects[999999] == []

    measurements = repo.measurements_for_inspections(conn, ids)
    assert set(measurements) == set(ids)
    assert len(measurements[with_data["id"]]) == 5
    first_measurement = measurements[with_data["id"]][0]
    assert first_measurement["code"] == "BORE"
    assert first_measurement["parameter_name"] == "B"
    assert first_measurement["value"] == 1.0
    assert measurements[without_data["id"]] == []
    assert measurements[999999] == []

    assert repo.defects_for_inspections(conn, []) == {}
    assert repo.measurements_for_inspections(conn, []) == {}


# --- §7.1: аналитические выборки ------------------------------------------

def test_load_period_facts_shape_values_and_date_filter(db):
    conn, _ = db
    ids = _seed_analytics(conn)

    facts = repo.load_period_facts(conn, since="2026-01-01", until="2026-03-31")
    assert set(facts) == {ids["p1"], ids["p2"]}
    assert all(isinstance(f, ProductFacts) for f in facts.values())

    f1 = facts[ids["p1"]]
    assert (f1.product_id, f1.sku, f1.name, f1.category) == (
        ids["p1"], "SKU-A", "Alpha", "CatA")
    assert (f1.sample_qty, f1.rejected_qty) == (20, 7)
    assert (f1.defect_qty, f1.repeat_qty) == (7, 2)
    assert (f1.in_spec, f1.with_specs) == (9, 10)
    assert (f1.batches_total, f1.batches_clean) == (3, 1)

    f2 = facts[ids["p2"]]
    assert (f2.sample_qty, f2.rejected_qty, f2.defect_qty, f2.repeat_qty) == (0, 0, 0, 0)
    assert (f2.batches_total, f2.batches_clean) == (0, 0)

    # Узкое окно: декабрьский акт и январская партия не должны попасть.
    feb = repo.load_period_facts(conn, since="2026-02-01", until="2026-02-28")
    feb1 = feb[ids["p1"]]
    assert (feb1.sample_qty, feb1.rejected_qty) == (10, 5)
    assert (feb1.defect_qty, feb1.repeat_qty) == (4, 0)
    assert (feb1.batches_total, feb1.batches_clean) == (1, 0)


def test_load_monthly_facts_shape_and_date_filter(db):
    conn, _ = db
    _seed_analytics(conn)

    months = repo.load_monthly_facts(conn, since="2026-01-01", until="2026-03-31")
    assert all(isinstance(m, MonthlyFacts) for m in months)
    assert [m.month for m in months] == ["2026-01", "2026-02", "2026-03"]

    jan, feb, mar = months
    assert (jan.batches, jan.inspections, jan.sample_qty, jan.rejected_qty) == (1, 1, 10, 2)
    assert jan.defect_rate == pytest.approx(0.2)
    assert jan.accepted_batches == 1

    assert (feb.batches, feb.inspections, feb.sample_qty, feb.rejected_qty) == (1, 1, 10, 5)
    assert feb.defect_rate == pytest.approx(0.5)
    assert feb.accepted_batches == 0

    assert (mar.batches, mar.inspections, mar.sample_qty) == (1, 0, 0)
    assert mar.defect_rate is None
    assert mar.accepted_batches == 0

    narrowed = repo.load_monthly_facts(conn, since="2026-02-01", until="2026-03-31")
    assert [m.month for m in narrowed] == ["2026-02", "2026-03"]
    assert repo.load_monthly_facts(conn, since="2030-01-01", until="2030-12-31") == []


def test_load_pareto_counts_shape_order_and_date_filter(db):
    conn, _ = db
    _seed_analytics(conn)

    counts = repo.load_pareto_counts(conn, since="2026-01-01", until="2026-03-31")
    assert all(isinstance(c, DefectCount) for c in counts)
    assert [c.code for c in counts] == ["DA", "DB"]  # qty DESC
    assert (counts[0].name, counts[0].group_name, counts[0].severity, counts[0].qty) == (
        "Defect A", "Размеры", 3, 6)
    assert (counts[1].code, counts[1].qty) == ("DB", 1)

    # Декабрьский акт с qty=100 обязан быть исключён фильтром периода.
    feb = repo.load_pareto_counts(conn, since="2026-02-01", until="2026-02-28")
    assert [c.code for c in feb] == ["DA"]
    assert feb[0].qty == 4

    assert repo.load_pareto_counts(conn, since="2030-01-01", until="2030-12-31") == []


def test_load_spc_subgroups_shape_order_and_date_filter(db):
    conn, _ = db
    ids = _seed_analytics(conn)

    subgroups = repo.load_spc_subgroups(
        conn, parameter_id=ids["pa"], since="2026-01-01", until="2026-03-31")
    assert all(isinstance(s, SpcSubgroup) for s in subgroups)
    assert [s.label for s in subgroups] == ["B1", "B2"]
    assert [s.subgroup_no for s in subgroups] == [1, 2]
    assert subgroups[0].values == (1.0, 2.0, 3.0, 4.0, 20.0)
    assert subgroups[1].values == (5.0, 6.0, 7.0, 8.0, 9.0)
    assert subgroups[0].n == 5
    assert subgroups[0].xbar == pytest.approx(6.0)

    jan_only = repo.load_spc_subgroups(
        conn, parameter_id=ids["pa"], since="2026-01-01", until="2026-01-31")
    assert [s.label for s in jan_only] == ["B1"]

    # Параметр без допусков также даёт подгруппы.
    pb_subs = repo.load_spc_subgroups(
        conn, parameter_id=ids["pb"], since="2026-01-01", until="2026-03-31")
    assert [s.label for s in pb_subs] == ["B1"]

    assert repo.load_spc_subgroups(
        conn, parameter_id=999999, since="2026-01-01", until="2026-03-31") == []


# --- §7.1: KPI главной страницы -------------------------------------------

def test_dashboard_stats_exact_keys_and_values(db):
    conn, _ = db
    _seed_analytics(conn)

    stats = repo.dashboard_stats(conn, since="2026-01-01", until="2026-03-31")

    assert set(stats) == {
        "batches_total",
        "batches_pending",
        "accepted_share",
        "defect_rate",
        "index_overall",
        "top_defect_code",
        "top_defect_name",
        "top_defect_qty",
        "parameters_critical",
        "products_active",
        "inspections_count",
    }
    assert stats["batches_total"] == 3
    assert stats["batches_pending"] == 1
    assert stats["accepted_share"] == pytest.approx(0.5)
    assert stats["defect_rate"] == pytest.approx(0.35)
    assert stats["index_overall"] is None
    assert stats["top_defect_code"] == "DA"
    assert stats["top_defect_name"] == "Defect A"
    assert stats["top_defect_qty"] == 6
    assert stats["parameters_critical"] == 1
    assert stats["products_active"] == 2
    assert stats["inspections_count"] == 2


def test_dashboard_stats_on_empty_database(db):
    conn, _ = db

    stats = repo.dashboard_stats(conn, since="2026-01-01", until="2026-12-31")

    assert stats["batches_total"] == 0
    assert stats["batches_pending"] == 0
    assert stats["accepted_share"] is None
    assert stats["defect_rate"] is None
    assert stats["top_defect_code"] is None
    assert stats["top_defect_name"] is None
    assert stats["top_defect_qty"] == 0
    assert stats["inspections_count"] == 0


# --- §7.1: веса индекса качества ------------------------------------------

def test_get_weights_inserts_baseline_once(db):
    conn, _ = db
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM quality_index_config"
    ).fetchone()["n"] == 0

    weights = repo.get_weights(conn)
    assert isinstance(weights, IndexWeights)
    assert weights.version == 1
    assert (weights.w_defect, weights.w_spec, weights.w_repeat, weights.w_clean) == (
        0.40, 0.30, 0.15, 0.15)
    assert (weights.defect_rate_cap, weights.repeat_rate_cap) == (0.10, 0.20)
    assert weights.min_sample == 10
    assert weights.updated_by == "system"

    again = repo.get_weights(conn)
    assert again.version == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM quality_index_config"
    ).fetchone()["n"] == 1


def test_update_weights_success_bumps_version(db):
    conn, _ = db
    before = repo.get_weights(conn)

    updated = repo.update_weights(
        conn, author="auditor",
        w_defect=0.5, w_spec=0.2, w_repeat=0.2, w_clean=0.1,
        defect_rate_cap=0.2, repeat_rate_cap=0.3, min_sample=5,
    )

    assert updated.version == before.version + 1
    assert updated.version == 2
    assert (updated.w_defect, updated.w_spec, updated.w_repeat, updated.w_clean) == (
        0.5, 0.2, 0.2, 0.1)
    assert updated.defect_rate_cap == 0.2
    assert updated.repeat_rate_cap == 0.3
    assert updated.min_sample == 5
    assert updated.updated_by == "auditor"
    assert updated.updated_at != ""

    assert repo.get_weights(conn).version == 2


@pytest.mark.parametrize("fields", [
    {"author": "   "},
    {"author": "x", "w_defect": -0.1, "w_spec": 0.5, "w_repeat": 0.35, "w_clean": 0.25},
    {"author": "x", "w_defect": 0.5},  # сумма весов != 1
    {"author": "x", "defect_rate_cap": 0},
    {"author": "x", "defect_rate_cap": -1},
    {"author": "x", "repeat_rate_cap": 0},
    {"author": "x", "min_sample": -1},
    {"author": "x", "bogus_field": 1},
    {"author": "x", "w_defect": "abc"},
])
def test_update_weights_validation_errors_keep_version(db, fields):
    conn, _ = db
    before = repo.get_weights(conn)

    with pytest.raises(ValueError):
        repo.update_weights(conn, **fields)

    after = repo.get_weights(conn)
    assert after.version == before.version
    assert after == before


def test_update_weights_rejects_nan_weight_and_keeps_config(db):
    """NaN-вес должен отклоняться как ValueError, не доходя до NOT NULL-колонки.

    До исправления NaN проходил проверки ``< 0`` и суммы весов и сохранялся
    драйвером SQLite как NULL → ``sqlite3.IntegrityError`` вместо контракта
    §7.1 (``ValueError``). Здесь же проверяется, что отказ не изменил строку.
    """
    conn, _ = db
    before = repo.get_weights(conn)

    with pytest.raises(ValueError, match="конечн"):
        repo.update_weights(
            conn, author="x",
            w_defect=float("nan"), w_spec=0.30, w_repeat=0.15, w_clean=0.15,
        )

    after = repo.get_weights(conn)
    assert after.version == before.version
    assert (after.w_defect, after.w_spec, after.w_repeat, after.w_clean) == (
        before.w_defect, before.w_spec, before.w_repeat, before.w_clean)
    assert after == before


def test_update_weights_rejects_inf_cap_and_keeps_config(db):
    """Бесконечный норматив должен отклоняться как ValueError.

    До исправления ``inf`` проходил проверку ``<= 0`` (ложна для +inf) и
    записывался в REAL-колонку как бесконечность — ``ValueError`` не возникал
    вовсе. Отклонённый апдейт не должен менять строку конфигурации.
    """
    conn, _ = db
    before = repo.get_weights(conn)

    with pytest.raises(ValueError, match="конечн"):
        repo.update_weights(
            conn, author="x",
            defect_rate_cap=float("inf"),
        )

    after = repo.get_weights(conn)
    assert after.version == before.version
    assert (after.defect_rate_cap, after.repeat_rate_cap) == (
        before.defect_rate_cap, before.repeat_rate_cap)
    assert after == before


# --- §9: read-only проверка поставляемой БД -------------------------------

def test_shipped_database_integrity_readonly():
    assert SHIPPED_DB.exists(), f"нет поставляемой БД: {SHIPPED_DB}"

    conn = sqlite3.connect(SHIPPED_DB.as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        assert row[0] == "ok"
    finally:
        conn.close()
