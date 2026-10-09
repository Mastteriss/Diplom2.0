"""Тесты генератора демо-данных (docs/contract.md §2, §2.1, §8).

Проверяем ИМЕННО генератор ``scripts/generate_demo_data.py`` как чёрный ящик:
каждый сценарий запускает его отдельным процессом с явным временным ``--db``
под ``tests_artifacts/`` и читает результат стандартным ``sqlite3`` в режиме
``mode=ro``. Поставляемая БД ``data/quality.sqlite3`` никогда не используется.

Стратегия:
* детерминированные маленькие параметры (``--products``, ``--batches-per-product``,
  ``--months``, фиксированные ``--seed``/``--anchor``);
* временные БД уникальны, удаляются на teardown best-effort (падение очистки
  не должно валить тест — §9 контракта).
"""

from __future__ import annotations

import ast
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
GENERATOR_SOURCE = PROJECT_DIR / "scripts" / "generate_demo_data.py"
ARTIFACTS_DIR = PROJECT_DIR / "tests_artifacts"
PYTHON = sys.executable

ANCHOR = "2026-09-30"
# Последние 12 месяцев относительно ANCHOR — то же окно, что печатает сводка
# генератора и задаёт §8 п.2 («за период»).
PERIOD_SINCE = "2025-10-01"

# §8: значения по умолчанию. Явно фиксируем их, чтобы «референс-пресет» был
# воспроизводим независимо от будущих правок DEFAULT_*.
REFERENCE_ARGS = (
    "--products", "10",
    "--batches-per-product", "18",
    "--months", "24",
    "--seed", "20260101",
    "--anchor", ANCHOR,
    "--force-reference",
)

PLANTED_SKU = "SKU-1004"
PLANTED_PARAM = "BORE"
PLANTED_SIGMA = 0.05              # внутренняя таблица sigma генератора (§8)
PLANTED_VALUE = 12.30            # target(12.0) + 6 * 0.05
PLANTED_BATCH = "B0018-SKU-1004"  # последняя партия при batches-per-product=18

EXPECTED_GROUPS = {
    "Внешний вид",
    "Размеры",
    "Прочность",
    "Материал",
    "Функциональные",
    "Упаковка",
    "Сопроводительные документы",
    "Прочее",
}
EXPECTED_LINES = {"Линия-1", "Линия-2", "Линия-3"}
EXPECTED_PARAMS = {"DIAM", "BORE", "LENGTH", "WEIGHT", "HARDNESS", "SURFACE"}

# Стабильный числовой дамп: timestamps (created_at/updated_at) исключены —
# это wall-clock, а не данные, зависящие от seed.
_DUMP_QUERIES: dict[str, str] = {
    "products": (
        "SELECT sku, name, category, unit, is_active FROM products ORDER BY sku, id"
    ),
    "parameters": (
        "SELECT code, name, unit, target, lsl, usl, is_critical "
        "FROM parameters_ref ORDER BY code"
    ),
    "defects": (
        "SELECT code, name, group_name, severity, is_active "
        "FROM defects_ref ORDER BY code"
    ),
    "batches": (
        "SELECT p.sku, b.batch_no, b.line, b.produced_qty, b.produced_at, b.status "
        "FROM batches b JOIN products p ON p.id = b.product_id "
        "ORDER BY p.sku, b.batch_no"
    ),
    "inspections": (
        "SELECT p.sku, b.batch_no, i.inspected_at, i.inspector, i.sample_qty, "
        "i.accepted_qty, i.rejected_qty, i.result "
        "FROM inspections i JOIN batches b ON b.id = i.batch_id "
        "JOIN products p ON p.id = b.product_id "
        "ORDER BY p.sku, b.batch_no, i.inspected_at, i.id"
    ),
    "defect_events": (
        "SELECT p.sku, b.batch_no, i.inspected_at, d.code, de.qty, de.is_repeat, de.note "
        "FROM defect_events de JOIN inspections i ON i.id = de.inspection_id "
        "JOIN batches b ON b.id = i.batch_id JOIN products p ON p.id = b.product_id "
        "JOIN defects_ref d ON d.id = de.defect_id "
        "ORDER BY p.sku, b.batch_no, i.inspected_at, d.code"
    ),
    "measurements": (
        "SELECT p.sku, b.batch_no, i.inspected_at, pr.code, m.value "
        "FROM measurements m JOIN inspections i ON i.id = m.inspection_id "
        "JOIN batches b ON b.id = i.batch_id JOIN products p ON p.id = b.product_id "
        "JOIN parameters_ref pr ON pr.id = m.parameter_id "
        "ORDER BY p.sku, b.batch_no, i.inspected_at, pr.code, m.id"
    ),
    "config": (
        "SELECT version, w_defect, w_spec, w_repeat, w_clean, defect_rate_cap, "
        "repeat_rate_cap, min_sample, notes, updated_by "
        "FROM quality_index_config WHERE id = 1"
    ),
}

_COUNT_TABLES = (
    "products",
    "parameters_ref",
    "defects_ref",
    "quality_index_config",
    "batches",
    "inspections",
    "defect_events",
    "measurements",
)


# --- Инфраструктура --------------------------------------------------------

def _remove_db(path: Path) -> None:
    """Удаляет БД и её боковые файлы; неудача очистки не валит тест (§9)."""
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def run_generator(
    db_path: Path, args: tuple[str, ...] | list[str] = (), *, check: bool = True
) -> subprocess.CompletedProcess:
    """Запускает генератор отдельным процессом с явным ``--db``."""
    cmd = [PYTHON, "-m", "scripts.generate_demo_data", "--db", str(db_path), *args]
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check:
        assert proc.returncode == 0, (
            f"генератор упал (код {proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


@contextmanager
def open_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """Открывает готовую БД только для чтения (без записи и без боковых файлов)."""
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Упорядоченный стабильный дамп всех числовых таблиц."""
    return {
        name: [tuple(row) for row in conn.execute(sql)]
        for name, sql in _DUMP_QUERIES.items()
    }


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _COUNT_TABLES
    }


@pytest.fixture
def db_maker():
    """Выдаёт уникальные пути под tests_artifacts/ и чистит их после теста."""
    created: list[Path] = []

    def make(tag: str) -> Path:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACTS_DIR / f"{tag}-{uuid.uuid4().hex}.sqlite3"
        created.append(path)
        return path

    yield make
    for path in created:
        _remove_db(path)


@pytest.fixture(scope="module")
def reference_db() -> Iterator[Path]:
    """Референс-пресет §8, сгенерированный один раз (только чтение)."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"reference-{uuid.uuid4().hex}.sqlite3"
    run_generator(path, REFERENCE_ARGS)
    yield path
    _remove_db(path)


# --- §8 CLI ----------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_args, needle",
    [
        (["--products", "0"], "значение должно быть ≥ 1"),
        (["--batches-per-product", "0"], "значение должно быть ≥ 1"),
        (["--months", "0"], "значение должно быть ≥ 1"),
        (["--anchor", "not-a-date"], "некорректная дата"),
        (["--seed", "abc"], "не целое число"),
    ],
)
def test_cli_rejects_invalid_arguments(db_maker, bad_args, needle):
    """§8 CLI: неположительные размеры и битые seed/anchor → код 2, БД не создаётся."""
    path = db_maker("cli-bad")
    proc = run_generator(path, list(bad_args), check=False)
    assert proc.returncode == 2
    combined = proc.stdout + proc.stderr
    assert needle in combined, combined
    assert not path.exists()
    assert not Path(f"{path}-wal").exists()


def test_cli_defaults_match_contract():
    """§8 CLI: значения по умолчанию ровно как в контракте (БД не трогаем)."""
    code = (
        "from scripts.generate_demo_data import build_parser;"
        "ns = build_parser().parse_args([]);"
        "print('DEFAULTS=' + repr((ns.products, ns.batches_per_product, ns.months,"
        " ns.seed, ns.anchor, ns.db, ns.force_reference)))"
    )
    proc = subprocess.run(
        [PYTHON, "-c", code],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert (
        "DEFAULTS=(10, 18, 24, 20260101, '2026-09-30', 'data/quality.sqlite3', False)"
        in proc.stdout
    ), proc.stdout


def test_minimal_arguments_succeed(db_maker):
    """Граница «одна позиция/одна партия»: успех, единственная партия — pending."""
    path = db_maker("minimal")
    run_generator(
        path,
        ["--products", "1", "--batches-per-product", "1", "--months", "1",
         "--seed", "0", "--anchor", "2026-01-15"],
    )
    with open_ro(path) as conn:
        assert _counts(conn)["products"] == 10  # референс всегда 10 позиций
        assert _counts(conn)["batches"] == 1
        assert conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 0
        row = conn.execute("SELECT status FROM batches").fetchone()
        assert row["status"] == "pending"


# --- §8 п.1 Детерминизм ----------------------------------------------------

def test_same_seed_produces_identical_numeric_data(db_maker):
    """§8: повторный запуск с тем же seed даёт одинаковые числовые данные."""
    common = ["--products", "4", "--batches-per-product", "5", "--months", "4",
              "--anchor", "2026-06-15"]
    first = db_maker("det-a")
    second = db_maker("det-b")
    run_generator(first, [*common, "--seed", "12345"])
    run_generator(second, [*common, "--seed", "12345"])
    with open_ro(first) as ca, open_ro(second) as cb:
        dump_a = _dump(ca)
        dump_b = _dump(cb)
    assert dump_a["batches"], "генератор не создал партий"
    assert dump_a == dump_b


def test_different_seeds_produce_different_data(db_maker):
    """§8: разные seed дают разные числовые данные."""
    common = ["--products", "4", "--batches-per-product", "5", "--months", "4",
              "--anchor", "2026-06-15"]
    first = db_maker("seed-a")
    second = db_maker("seed-b")
    run_generator(first, [*common, "--seed", "12345"])
    run_generator(second, [*common, "--seed", "54321"])
    with open_ro(first) as ca, open_ro(second) as cb:
        assert _dump(ca) != _dump(cb)


# --- §8 п.6 / §2.1 Референс ------------------------------------------------

def test_reference_preset_seeding(reference_db):
    """§8 п.6 + §2.1: 10 продуктов, 6 параметров, 12 дефектов в 8 группах, 3 линии."""
    with open_ro(reference_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM parameters_ref").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM defects_ref").fetchone()[0] == 12

        codes = {r["code"] for r in conn.execute("SELECT code FROM parameters_ref")}
        assert codes == EXPECTED_PARAMS
        critical = {
            r["code"] for r in conn.execute(
                "SELECT code FROM parameters_ref WHERE is_critical = 1"
            )
        }
        assert critical == {"DIAM", "BORE", "HARDNESS"}

        groups = {
            r["group_name"] for r in conn.execute("SELECT group_name FROM defects_ref")
        }
        assert groups == EXPECTED_GROUPS

        skus = {r["sku"] for r in conn.execute("SELECT sku FROM products")}
        assert PLANTED_SKU in skus

        lines = {r["line"] for r in conn.execute("SELECT line FROM batches")}
        assert lines == EXPECTED_LINES


# --- §8 п.1 Посадка SPC ----------------------------------------------------

def test_planted_spc_anomaly_triggers_beyond_limit(reference_db):
    """§8 п.1: SKU-1004/BORE — 2 последовательные подгруппы target+6σ → beyond_limit."""
    with open_ro(reference_db) as conn:
        target = conn.execute(
            "SELECT target FROM parameters_ref WHERE code = ?", (PLANTED_PARAM,)
        ).fetchone()["target"]
        assert target == pytest.approx(12.0)
        planted_value = round(float(target) + 6.0 * PLANTED_SIGMA, 4)
        assert planted_value == PLANTED_VALUE

        rows = conn.execute(
            """
            SELECT i.id AS inspection_id, i.inspected_at,
                   MIN(m.value) AS lo, MAX(m.value) AS hi, COUNT(*) AS n
            FROM measurements m
            JOIN inspections i ON i.id = m.inspection_id
            JOIN batches b ON b.id = i.batch_id
            JOIN products p ON p.id = b.product_id
            JOIN parameters_ref pr ON pr.id = m.parameter_id
            WHERE p.sku = ? AND pr.code = ?
            GROUP BY i.id
            ORDER BY i.inspected_at, i.id
            """,
            (PLANTED_SKU, PLANTED_PARAM),
        ).fetchall()

    planted = [
        r for r in rows
        if r["lo"] == planted_value and r["hi"] == planted_value and r["n"] == 5
    ]
    assert len(planted) == 2, [dict(r) for r in rows]
    planted_ids = {r["inspection_id"] for r in planted}
    positions = [i for i, r in enumerate(rows) if r["inspection_id"] in planted_ids]
    assert positions[1] - positions[0] == 1, positions
    assert [r["inspected_at"] for r in planted] == ["2026-09-27", "2026-09-29"]
    others = [r["inspected_at"] for r in rows if r["inspection_id"] not in planted_ids]
    assert max(others) <= "2026-09-26"  # прочие акты не позже anchor − 4

    # Прогоняем реальный SPC (B2) по всем подгруппам BORE и убеждаемся, что
    # посаженные подгруппы действительно ловят нарушение beyond_limit.
    from app.analysis.spc import compute_spc
    from app.db.repositories import get_parameter_by_code, load_spc_subgroups
    from app.domain.models import ParameterSpec

    with open_ro(reference_db) as conn:
        prm = get_parameter_by_code(conn, PLANTED_PARAM)
        spec = ParameterSpec(
            id=prm["id"], code=prm["code"], name=prm["name"], unit=prm["unit"],
            target=prm["target"], lsl=prm["lsl"], usl=prm["usl"],
            is_critical=bool(prm["is_critical"]),
        )
        subgroups = load_spc_subgroups(
            conn, parameter_id=prm["id"], since=PERIOD_SINCE, until=ANCHOR
        )
        result = compute_spc(subgroups, spec, period=f"{PERIOD_SINCE}..{ANCHOR}")

    assert result.enough is True
    assert result.ucl is not None and result.ucl < PLANTED_VALUE
    planted_points = [pt for pt in result.points if pt.xbar == pytest.approx(PLANTED_VALUE)]
    assert len(planted_points) == 2
    assert len({pt.label for pt in planted_points}) == 1
    assert next(iter({pt.label for pt in planted_points})) == PLANTED_BATCH
    for point in planted_points:
        assert "beyond_limit" in point.violations


# --- §8 п.2 Тяжёлая голова Парето ------------------------------------------

def test_top_defect_share_at_least_35_percent(reference_db):
    """§8 п.2: один код дефекта даёт ≥ 35 % количества за период."""
    with open_ro(reference_db) as conn:
        rows = conn.execute(
            """
            SELECT d.code AS code, SUM(de.qty) AS qty
            FROM defect_events de
            JOIN inspections i ON i.id = de.inspection_id
            JOIN defects_ref d ON d.id = de.defect_id
            WHERE substr(i.inspected_at, 1, 10) BETWEEN ? AND ?
            GROUP BY d.id
            ORDER BY qty DESC, d.code
            """,
            (PERIOD_SINCE, ANCHOR),
        ).fetchall()
    total = sum(int(r["qty"]) for r in rows)
    assert total > 0
    top = rows[0]
    assert top["code"] == "SCRATCH"
    assert int(top["qty"]) / total >= 0.35


# --- §8 п.3 Статусы --------------------------------------------------------

def test_all_four_batch_statuses_and_pending_without_inspections(reference_db):
    """§8 п.3: все 4 статуса; pending-партия без актов."""
    with open_ro(reference_db) as conn:
        statuses = {
            r["status"] for r in conn.execute("SELECT DISTINCT status FROM batches")
        }
        assert statuses == {"pending", "accepted", "partial", "rejected"}

        pending_total = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE status = 'pending'"
        ).fetchone()[0]
        pending_without = conn.execute(
            "SELECT COUNT(*) FROM batches b WHERE b.status = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM inspections i WHERE i.batch_id = b.id)"
        ).fetchone()[0]
        assert pending_total >= 1
        assert pending_without == pending_total  # pending ⇔ нет актов (§2.2)


# --- §8 п.4 Повторные дефекты ----------------------------------------------

def test_at_least_30_repeat_defect_events(reference_db):
    """§8 п.4: ≥ 30 записей defect_events с is_repeat = 1."""
    with open_ro(reference_db) as conn:
        repeats = conn.execute(
            "SELECT COUNT(*) FROM defect_events WHERE is_repeat = 1"
        ).fetchone()[0]
    assert repeats >= 30


# --- §8 п.5 Измерения ------------------------------------------------------

def test_exactly_five_measurements_per_inspection_parameter(reference_db):
    """§8 п.5: ровно 5 измерений на каждую пару (акт, параметр) для всех параметров."""
    with open_ro(reference_db) as conn:
        n_inspections = conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
        n_parameters = conn.execute("SELECT COUNT(*) FROM parameters_ref").fetchone()[0]
        n_measurements = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        distribution = conn.execute(
            "SELECT cnt FROM ("
            "SELECT COUNT(*) AS cnt FROM measurements "
            "GROUP BY inspection_id, parameter_id)"
        ).fetchall()
        n_pairs = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT DISTINCT inspection_id, parameter_id FROM measurements)"
        ).fetchone()[0]

    assert n_inspections > 0
    assert n_parameters == 6
    assert n_measurements == 5 * n_inspections * n_parameters
    assert distribution, "нет ни одной пары (акт, параметр)"
    assert all(int(r["cnt"]) == 5 for r in distribution)
    assert n_pairs == n_inspections * n_parameters


# --- §8 add-only -----------------------------------------------------------

def test_rerun_is_add_only_and_does_not_duplicate(db_maker):
    """§8: повторный запуск не дублирует справочники и не удаляет строки."""
    path = db_maker("add-only")
    args = ["--products", "6", "--batches-per-product", "6", "--months", "6",
            "--seed", "42", "--anchor", "2026-05-20"]
    run_generator(path, args)
    with open_ro(path) as conn:
        before = _counts(conn)
        batch_ids_before = {r["id"] for r in conn.execute("SELECT id FROM batches")}

    run_generator(path, args)

    with open_ro(path) as conn:
        after = _counts(conn)
        batch_ids_after = {r["id"] for r in conn.execute("SELECT id FROM batches")}

    assert before["products"] == 10
    assert after["products"] == 10  # не задвоились
    assert after["parameters_ref"] == 6
    assert after["defects_ref"] == 12
    assert after == before  # ни добавления, ни удаления
    assert batch_ids_before <= batch_ids_after


# --- §8 запрет импорта app.analysis ----------------------------------------

def test_generator_source_does_not_import_app_analysis():
    """§8: в исходнике генератора нет импорта app.analysis (AST-проверка)."""
    tree = ast.parse(GENERATOR_SOURCE.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [
        module for module in imported
        if module == "app.analysis" or module.startswith("app.analysis.")
    ]
    assert offenders == [], offenders


def test_generator_runtime_does_not_load_app_analysis(db_maker):
    """§8: после main() в sys.modules нет app.analysis (проверка в подпроцессе)."""
    path = db_maker("no-analysis")
    code = (
        "import sys;"
        "import scripts.generate_demo_data as g;"
        f"g.main(['--db', {str(path)!r}, '--products', '1',"
        " '--batches-per-product', '1', '--months', '1',"
        " '--seed', '1', '--anchor', '2026-01-15']);"
        "print('ANALYSIS_IMPORTED=' + str('app.analysis' in sys.modules))"
    )
    proc = subprocess.run(
        [PYTHON, "-c", code],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ANALYSIS_IMPORTED=False" in proc.stdout, proc.stdout


# --- §8 очистка -wal/-shm --------------------------------------------------

def test_generator_removes_wal_and_shm_sidecars(db_maker):
    """§8: после завершения генератора не остаётся файлов -wal/-shm."""
    path = db_maker("sidecars")
    run_generator(
        path,
        ["--products", "2", "--batches-per-product", "2", "--months", "2",
         "--seed", "7", "--anchor", "2026-02-10"],
    )
    assert path.exists()
    assert not Path(f"{path}-wal").exists(), "остался -wal"
    assert not Path(f"{path}-shm").exists(), "остался -shm"
