"""Тесты слоя B3 (веб) по docs/contract.md: §5 (маршруты), §5.1 (форма акта),
§6 (CSV) и §9 (соглашения).

Проверяется наблюдаемое поведение через HTTP: коды ответов, заголовок
``Location``, содержимое отрендеренных шаблонов, персистентное состояние
временной SQLite и байты CSV-выгрузок.

Изоляция: на каждый тест создаётся уникальная БД в ``tests_artifacts/``,
удаляется в teardown (best-effort). Поток-локальное соединение веб-слоя
подменяется (``app.web.context.get_connection``) на одно соединение с
``check_same_thread=False``: sync-обработчики выполняются в worker-потоке
AnyIO, и без подмены такое соединение осталось бы открытым и блокировало файл.

Поставляемая БД ``data/quality.sqlite3`` не трогается.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import APP_VERSION, PROJECT_DIR
from app.db import repositories
from app.db.connection import init_db
from app.domain.models import InspectionDraft
from app.main import app
from app.web.context import base_context

ARTIFACTS_DIR: Path = PROJECT_DIR / "tests_artifacts"

# Точные заголовки CSV (§6).
BATCHES_HEADER = [
    "ID", "SKU", "Продукция", "Линия", "Статус", "Произведено", "Дата", "Актов ОТК",
]
DEFECTS_HEADER = ["Код", "Дефект", "Группа", "Критичность", "Количество", "Доля"]

# Справочники временной БД.
DEFECTS_REF = {"D1": 1, "D2": 2, "D3": 3}
PARAMS_REF = {"CRIT": 1, "SEC": 2}


# --- Фикстуры и подготовка временной БД ------------------------------------


def _open_shared_connection(path: Path) -> sqlite3.Connection:
    """Соединение, пригодное для использования из worker-потоков AnyIO."""
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    return conn


def _remove_db(path: Path) -> None:
    """Best-effort удаление файла БД и его WAL/SHM спутников."""
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass


def _seed(conn: sqlite3.Connection) -> None:
    """Детерминированный набор: 2 продукта, по партии на статус, акты с
    дефектами и 5 измерениями на (акт, параметр), распределение дефектов."""
    today = date.today().isoformat()
    stamp = f"{today}T00:00:00+00:00"

    for row in (
        (1, "CRIT", "Критичный параметр", "мм", 10.0, 9.0, 11.0, 1),
        (2, "SEC", "Второй параметр", "мм", 20.0, 19.0, 21.0, 0),
    ):
        conn.execute(
            "INSERT INTO parameters_ref "
            "(id, code, name, unit, target, lsl, usl, is_critical) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    for row in (
        (1, "SKU-A", "Изделие Альфа", "Категория А", "шт.", 1, stamp),
        (2, "SKU-B", "Изделие Бета", "Категория Б", "кг", 1, stamp),
    ):
        conn.execute(
            "INSERT INTO products "
            "(id, sku, name, category, unit, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    for row in (
        (1, "D1", "Царапина", "Внешний вид", 2, 1),
        (2, "D2", "Вмятина", "Размеры", 3, 1),
        (3, "D3", "Трещина", "Прочность", 5, 1),
    ):
        conn.execute(
            "INSERT INTO defects_ref "
            "(id, code, name, group_name, severity, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.execute(
        "INSERT INTO quality_index_config "
        "(id, version, w_defect, w_spec, w_repeat, w_clean, defect_rate_cap, "
        "repeat_rate_cap, min_sample, notes, updated_at, updated_by) "
        "VALUES (1, 1, 0.40, 0.30, 0.15, 0.15, 0.10, 0.20, 10, '', ?, 'system')",
        (stamp,),
    )
    for row in (
        (1, 1, "B-PEND", "Линия-1", 100, today, "pending"),
        (2, 1, "B-ACC", "Линия-1", 100, today, "pending"),
        (3, 1, "B-PART", "Линия-1", 100, today, "pending"),
        (4, 2, "B-REJ", "Линия-2", 100, today, "pending"),
    ):
        conn.execute(
            "INSERT INTO batches "
            "(id, product_id, batch_no, line, produced_qty, produced_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )

    # Партия 2 → accepted: дефектов нет, измерения по CRIT и SEC.
    repositories.create_inspection(
        conn,
        batch_id=2,
        draft=InspectionDraft(
            inspected_at=today,
            inspector="Иванов И.И.",
            sample_qty=10,
            accepted_qty=10,
            rejected_qty=0,
            defects=(),
            measurements=(
                ("CRIT", (9.8, 9.9, 10.0, 10.1, 10.2)),
                ("SEC", (19.8, 19.9, 20.0, 20.1, 20.2)),
            ),
        ),
        defects_ref=DEFECTS_REF,
        params_ref=PARAMS_REF,
    )
    # Партия 3 → partial: дефекты D1=5, D2=2.
    repositories.create_inspection(
        conn,
        batch_id=3,
        draft=InspectionDraft(
            inspected_at=today,
            inspector="Петров П.П.",
            sample_qty=10,
            accepted_qty=7,
            rejected_qty=3,
            defects=(("D1", 5), ("D2", 2)),
            measurements=(("CRIT", (9.7, 9.6, 9.9, 10.2, 10.3)),),
        ),
        defects_ref=DEFECTS_REF,
        params_ref=PARAMS_REF,
    )
    # Партия 4 → rejected: дефекты D3=4, D1=6.
    repositories.create_inspection(
        conn,
        batch_id=4,
        draft=InspectionDraft(
            inspected_at=today,
            inspector="Сидоров С.С.",
            sample_qty=10,
            accepted_qty=0,
            rejected_qty=10,
            defects=(("D3", 4), ("D1", 6)),
            measurements=(("CRIT", (8.5, 8.4, 8.6, 8.7, 8.3)),),
        ),
        defects_ref=DEFECTS_REF,
        params_ref=PARAMS_REF,
    )


@contextmanager
def _temp_web(monkeypatch, seed):
    """Свежая временная БД + TestClient с подменённым соединением веб-слоя."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    db_path = ARTIFACTS_DIR / f"test_web_{uuid.uuid4().hex}.sqlite3"
    monkeypatch.setenv("DIPL2_DB", str(db_path))
    init_db(db_path)
    conn = _open_shared_connection(db_path)
    try:
        if seed is not None:
            seed(conn)
        monkeypatch.setattr("app.web.context.get_connection", lambda: conn)
        client = TestClient(app)
        with client:
            yield client, conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _remove_db(db_path)


@pytest.fixture()
def web(monkeypatch):
    with _temp_web(monkeypatch, _seed) as pair:
        yield pair


@pytest.fixture()
def web_empty(monkeypatch):
    """Пустая схема без данных — проверка пустых коллекций (граница)."""
    with _temp_web(monkeypatch, None) as pair:
        yield pair


def _valid_form(**overrides) -> dict[str, str]:
    form = {
        "inspector": "Иванов И.И.",
        "inspected_at": date.today().isoformat(),
        "sample_qty": "10",
        "accepted_qty": "10",
        "rejected_qty": "0",
    }
    form.update(overrides)
    return form


# --- §5: главная, продукция -------------------------------------------------


def test_index_ok_and_shows_top_defect(web):
    client, _ = web
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Главная" in resp.text
    # Верхний дефект распределения — D1 (11 из 17).
    assert "Царапина" in resp.text


def test_products_filters_and_page_clamp(web):
    client, _ = web

    resp = client.get("/products")
    assert resp.status_code == 200
    assert "SKU-A" in resp.text and "SKU-B" in resp.text

    resp = client.get("/products", params={"q": "Альфа"})
    assert resp.status_code == 200
    assert "SKU-A" in resp.text
    assert "SKU-B" not in resp.text

    resp = client.get("/products", params={"category": "Категория Б"})
    assert resp.status_code == 200
    assert "SKU-B" in resp.text
    assert "SKU-A" not in resp.text

    for page in ("0", "9999"):
        resp = client.get("/products", params={"page": page})
        assert resp.status_code == 200
        assert "Страница 1 из 1" in resp.text


def test_product_detail_ok_and_404(web):
    client, _ = web
    resp = client.get("/products/1")
    assert resp.status_code == 200
    assert "Изделие Альфа" in resp.text

    resp = client.get("/products/9999")
    assert resp.status_code == 404
    assert "Продукция не найдена в справочнике." in resp.text

    resp = client.get("/products/not-an-int")
    assert resp.status_code == 404


# --- §5: партии и акт ОТК ---------------------------------------------------


def test_batches_filters_and_page_clamp(web):
    client, _ = web

    resp = client.get("/batches")
    assert resp.status_code == 200
    for batch_no in ("B-PEND", "B-ACC", "B-PART", "B-REJ"):
        assert batch_no in resp.text

    resp = client.get("/batches", params={"status": "accepted"})
    assert resp.status_code == 200
    assert "B-ACC" in resp.text
    assert "B-REJ" not in resp.text

    resp = client.get("/batches", params={"status": "rejected"})
    assert resp.status_code == 200
    assert "B-REJ" in resp.text
    assert "B-ACC" not in resp.text

    resp = client.get("/batches", params={"product_id": "2"})
    assert resp.status_code == 200
    assert "B-REJ" in resp.text
    assert "B-ACC" not in resp.text

    for page in ("0", "9999"):
        resp = client.get("/batches", params={"page": page})
        assert resp.status_code == 200
        assert "Страница 1 из 1" in resp.text


def test_batch_detail_ok_and_404(web):
    client, _ = web
    resp = client.get("/batches/2")
    assert resp.status_code == 200
    assert "B-ACC" in resp.text
    assert "Регистрация акта ОТК" in resp.text
    assert "Иванов И.И." in resp.text

    resp = client.get("/batches/9999")
    assert resp.status_code == 404
    assert "Партия не найдена." in resp.text


def test_inspection_form_get_ok_and_404(web):
    client, _ = web
    resp = client.get("/batches/1/inspections/new")
    assert resp.status_code == 200
    assert "Регистрация акта ОТК" in resp.text
    assert 'name="param_CRIT"' in resp.text
    assert 'name="defect_D1"' in resp.text

    resp = client.get("/batches/9999/inspections/new")
    assert resp.status_code == 404


def test_inspection_post_valid_redirects_and_persists(web):
    client, conn = web
    form = _valid_form()
    form["defect_D1"] = "2"
    form["param_CRIT"] = "9.9, 10.0, 10.1, 9.8, 10.2"
    form["param_SEC"] = ""   # пустое поле параметра игнорируется (§5.1)
    form["defect_D2"] = ""   # пустое поле дефекта игнорируется (§5.1)

    resp = client.post(
        "/batches/1/inspections/new", data=form, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/batches/1"

    row = conn.execute(
        "SELECT * FROM inspections WHERE batch_id = 1"
    ).fetchone()
    assert row is not None
    assert row["inspector"] == "Иванов И.И."
    assert row["sample_qty"] == 10
    assert row["result"] == "accepted"

    events = conn.execute(
        "SELECT defect_id, qty, is_repeat FROM defect_events "
        "WHERE inspection_id = ?",
        (row["id"],),
    ).fetchall()
    assert [(e["defect_id"], e["qty"], e["is_repeat"]) for e in events] == [(1, 2, 0)]

    measurement_counts = conn.execute(
        "SELECT parameter_id, COUNT(*) AS n FROM measurements "
        "WHERE inspection_id = ? GROUP BY parameter_id",
        (row["id"],),
    ).fetchall()
    assert [(m["parameter_id"], m["n"]) for m in measurement_counts] == [(1, 5)]

    status = conn.execute(
        "SELECT status FROM batches WHERE id = 1"
    ).fetchone()["status"]
    assert status == "accepted"


def test_inspection_post_tomorrow_allowed_and_inspector_100_ok(web):
    client, _ = web
    form = _valid_form(
        inspector="И" * 100,
        inspected_at=(date.today() + timedelta(days=1)).isoformat(),
    )
    resp = client.post(
        "/batches/1/inspections/new", data=form, follow_redirects=False
    )
    assert resp.status_code == 303


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"inspector": ""}, "Укажите инспектора ОТК."),
        ({"inspector": "И" * 101}, "не более 100 символов"),
        (
            {"inspected_at": (date.today() + timedelta(days=2)).isoformat()},
            "не может быть позже завтрашнего дня",
        ),
        (
            {"accepted_qty": "5", "rejected_qty": "3"},
            "Сумма принятых и отклонённых единиц",
        ),
        ({"param_CRIT": "1, 2, 3"}, "ровно 5 значений"),
        ({"param_CRIT": "1,2,3,4,5,6"}, "ровно 5 значений"),
        ({"sample_qty": "0"}, "должно быть больше нуля"),
        ({"accepted_qty": "-1", "rejected_qty": "11"}, "не может быть отрицательным"),
    ],
)
def test_inspection_post_invalid_rerenders_form(web, overrides, fragment):
    client, conn = web
    before = conn.execute("SELECT COUNT(*) AS n FROM inspections").fetchone()["n"]

    resp = client.post(
        "/batches/1/inspections/new",
        data=_valid_form(**overrides),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert fragment in resp.text
    assert "Форма заполнена с ошибками:" in resp.text
    assert 'action="/batches/1/inspections/new"' in resp.text

    after = conn.execute("SELECT COUNT(*) AS n FROM inspections").fetchone()["n"]
    assert after == before


@pytest.mark.parametrize("non_finite", ["nan", "inf"])
def test_inspection_post_param_crit_non_finite_returns_400(web, non_finite):
    """§5.1: пять нечисловых (non-finite) значений параметра — ошибка формы,
    а не падение БД. До правки такие значения доходили до ``create_inspection``
    и давали ``IntegrityError`` → 500 (иногда с неявной записью ``inf``)."""
    client, conn = web
    before = conn.execute("SELECT COUNT(*) AS n FROM inspections").fetchone()["n"]

    resp = client.post(
        "/batches/1/inspections/new",
        data=_valid_form(param_CRIT=",".join([non_finite] * 5)),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert not resp.text.lstrip().startswith("{")
    assert "Форма заполнена с ошибками:" in resp.text
    assert "должно быть конечным числом." in resp.text
    assert 'action="/batches/1/inspections/new"' in resp.text

    after = conn.execute("SELECT COUNT(*) AS n FROM inspections").fetchone()["n"]
    assert after == before


def test_inspection_post_missing_batch_404(web):
    client, _ = web
    resp = client.post(
        "/batches/9999/inspections/new",
        data=_valid_form(),
        follow_redirects=False,
    )
    assert resp.status_code == 404


# --- §5: аналитика ----------------------------------------------------------


def test_analysis_pages_ok_and_months_clamp(web):
    client, _ = web

    for path in ("/analysis/pareto", "/analysis/trends", "/analysis/spc"):
        assert client.get(path).status_code == 200

        low = client.get(path, params={"months": "0"})
        assert low.status_code == 200
        assert 'max="36" value="1"' in low.text

        high = client.get(path, params={"months": "9999"})
        assert high.status_code == 200
        assert 'max="36" value="36"' in high.text


def test_pareto_renders_seeded_distribution(web):
    client, _ = web
    resp = client.get("/analysis/pareto")
    assert resp.status_code == 200
    assert "<code>D1</code>" in resp.text
    assert "Царапина" in resp.text
    assert ">17<" in resp.text  # всего дефектов


def test_trends_includes_current_month(web):
    client, _ = web
    resp = client.get("/analysis/trends")
    assert resp.status_code == 200
    assert date.today().strftime("%Y-%m") in resp.text


def test_spc_defaults_to_first_critical_and_unknown_falls_back(web):
    client, _ = web

    default = client.get("/analysis/spc")
    assert default.status_code == 200
    assert 'name="param" value="CRIT"' in default.text
    assert "Недостаточно подгрупп" in default.text  # 3 подгруппы < 10

    explicit = client.get("/analysis/spc", params={"param": "SEC"})
    assert explicit.status_code == 200
    assert 'name="param" value="SEC"' in explicit.text

    # Неизвестный код: страница падает на первый критичный параметр (код).
    unknown = client.get("/analysis/spc", params={"param": "NOPE"})
    assert unknown.status_code == 200
    assert 'name="param" value="CRIT"' in unknown.text
    assert 'name="param" value="NOPE"' not in unknown.text


# --- §5: настройки весов ----------------------------------------------------


def test_config_weights_get_ok(web):
    client, _ = web
    resp = client.get("/config/weights")
    assert resp.status_code == 200
    assert "Текущие веса критериев" in resp.text


def test_config_weights_post_valid(web):
    client, conn = web
    form = {
        "w_defect": "0.25",
        "w_spec": "0.25",
        "w_repeat": "0.25",
        "w_clean": "0.25",
        "defect_rate_cap": "0.10",
        "repeat_rate_cap": "0.20",
        "min_sample": "5",
        "author": "Петров П.П.",
    }
    resp = client.post("/config/weights", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/config/weights"

    row = conn.execute(
        "SELECT * FROM quality_index_config WHERE id = 1"
    ).fetchone()
    assert row["version"] == 2
    assert row["w_defect"] == pytest.approx(0.25)
    assert row["repeat_rate_cap"] == pytest.approx(0.20)
    assert row["min_sample"] == 5
    assert row["updated_by"] == "Петров П.П."

    page = client.get("/config/weights")
    assert 'value="5"' in page.text
    assert 'value="Петров П.П."' in page.text


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        (
            {"w_defect": "0.5", "w_spec": "0.5", "w_repeat": "0.5", "w_clean": "0.5"},
            "Сумма четырёх весов",
        ),
        ({"author": ""}, "Укажите автора настроек."),
    ],
)
def test_config_weights_post_invalid(web, overrides, fragment):
    client, conn = web
    form = {
        "w_defect": "0.25",
        "w_spec": "0.25",
        "w_repeat": "0.25",
        "w_clean": "0.25",
        "defect_rate_cap": "0.10",
        "repeat_rate_cap": "0.20",
        "min_sample": "5",
        "author": "Петров П.П.",
    }
    form.update(overrides)
    before = conn.execute(
        "SELECT version FROM quality_index_config WHERE id = 1"
    ).fetchone()["version"]

    resp = client.post("/config/weights", data=form, follow_redirects=False)
    assert resp.status_code == 400
    assert fragment in resp.text

    after = conn.execute(
        "SELECT version FROM quality_index_config WHERE id = 1"
    ).fetchone()["version"]
    assert after == before


@pytest.mark.parametrize(
    "field, non_finite",
    [("w_defect", "nan"), ("defect_rate_cap", "inf")],
)
def test_config_weights_post_non_finite_returns_400(web, field, non_finite):
    """§5: non-finite числовое поле — ошибка валидации формы (400) и
    неизменная версия конфигурации. До правки ``nan``/``inf`` проходили
    валидацию и доходили до SQLite → ``IntegrityError`` → 500."""
    client, conn = web
    form = {
        "w_defect": "0.25",
        "w_spec": "0.25",
        "w_repeat": "0.25",
        "w_clean": "0.25",
        "defect_rate_cap": "0.10",
        "repeat_rate_cap": "0.20",
        "min_sample": "5",
        "author": "Петров П.П.",
    }
    form[field] = non_finite
    before = conn.execute(
        "SELECT version FROM quality_index_config WHERE id = 1"
    ).fetchone()["version"]

    resp = client.post("/config/weights", data=form, follow_redirects=False)
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert not resp.text.lstrip().startswith("{")
    assert "Форма заполнена с ошибками:" in resp.text
    assert "значение должно быть конечным числом." in resp.text
    assert 'action="/config/weights"' in resp.text

    after = conn.execute(
        "SELECT version FROM quality_index_config WHERE id = 1"
    ).fetchone()["version"]
    assert after == before


# --- §6: CSV-выгрузки -------------------------------------------------------


def test_export_batches_csv(web):
    client, _ = web
    resp = client.get("/export/batches.csv")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert "partii.csv" in disposition
    assert resp.content.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in resp.content  # csv.writer(dialect="excel") → CRLF (§6)

    lines = resp.content.decode("utf-8-sig").splitlines()
    assert lines[0].split(";") == BATCHES_HEADER
    assert lines[0].count(";") == len(BATCHES_HEADER) - 1

    rows = [line.split(";") for line in lines[1:]]
    assert len(rows) == 4
    by_id = {int(row[0]): row for row in rows}
    assert by_id[1][4] == "Ожидает"
    assert by_id[2][4] == "Принята"
    assert by_id[2][1] == "SKU-A"
    assert by_id[2][7] == "1"
    assert by_id[3][4] == "Принята частично"
    assert by_id[4][4] == "Отклонена"


def test_export_defects_csv(web):
    client, _ = web
    resp = client.get("/export/defects.csv")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert "defekty.csv" in disposition
    assert resp.content.startswith(b"\xef\xbb\xbf")

    lines = resp.content.decode("utf-8-sig").splitlines()
    assert lines[0].split(";") == DEFECTS_HEADER

    rows = [line.split(";") for line in lines[1:]]
    assert len(rows) == 3
    by_code = {row[0]: row for row in rows}
    assert by_code["D1"][1] == "Царапина"
    assert by_code["D1"][2] == "Внешний вид"
    assert by_code["D1"][3] == "2"
    assert by_code["D1"][4] == "11"
    assert by_code["D1"][5] == "0.6471"  # SHARE_DP = 4
    for row in rows:
        assert re.fullmatch(r"\d+\.\d{4}", row[5])


# --- §5/§9: страница ошибки -------------------------------------------------


def test_unknown_route_renders_html_error(web):
    client, _ = web
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Запрошенная страница не найдена." in resp.text
    assert not resp.text.lstrip().startswith("{")


# --- Граница: пустая БД (пустые коллекции) ----------------------------------


def test_empty_database_pages_and_exports(web_empty):
    client, _ = web_empty

    for path in (
        "/",
        "/products",
        "/batches",
        "/analysis/pareto",
        "/analysis/trends",
        "/analysis/spc",
        "/config/weights",
    ):
        assert client.get(path).status_code == 200

    assert "Продукция не найдена." in client.get("/products").text
    assert "Партии по заданным условиям не найдены." in client.get("/batches").text
    assert (
        "Нет справочника контролируемых параметров."
        in client.get("/analysis/spc").text
    )

    batches = client.get("/export/batches.csv")
    assert batches.status_code == 200
    assert batches.content.decode("utf-8-sig").splitlines() == [
        ";".join(BATCHES_HEADER)
    ]

    defects = client.get("/export/defects.csv")
    assert defects.status_code == 200
    assert defects.content.decode("utf-8-sig").splitlines() == [
        ";".join(DEFECTS_HEADER)
    ]


# --- Шапка сайта: содержимое бренда и активный пункт меню (регрессия) --------
#
# Рестайлинг шапки (Avito-кнопки, отказ от подзаголовка) идёт параллельно,
# поэтому проверяются только текст и HTML-атрибуты в регионе ``<header>``:
# ни имён CSS-классов, ни цветов, ни app.css.

BRAND_TITLE = "ИС анализа качества продукции"
BRAND_SUBTITLE = "Анализ качества выпускаемой продукции"
NAV_LABELS = (
    "Главная",
    "Продукция",
    "Партии",
    "Дефекты",
    "Динамика",
    "Контроль процесса",
    "Настройки",
)


def _header_region(text: str) -> str:
    """Срез HTML от ``<header`` до ``</header>`` включительно.

    Имена элементов фиксированы контрактом шапки; ограничение регионом
    нужно, чтобы проверки не цеплялись за слова в теле страницы.
    """
    start = text.find("<header")
    end = text.find("</header>")
    assert start != -1, "страница не содержит <header>"
    assert end != -1 and end > start, "страница не содержит </header>"
    return text[start : end + len("</header>")]


def _footer_region(text: str) -> str:
    """Срез HTML от ``<footer`` до ``</footer>`` включительно."""
    start = text.find("<footer")
    end = text.find("</footer>")
    assert start != -1, "страница не содержит <footer>"
    assert end != -1 and end > start, "страница не содержит </footer>"
    return text[start : end + len("</footer>")]


def _nav_link(header: str, label: str) -> str:
    """Полный фрагмент ``<a …>…</a>`` с указанной подписью в шапке."""
    links = re.findall(r"<a\b[^>]*>.*?</a>", header, flags=re.S)
    matched = [link for link in links if label in link]
    assert len(matched) == 1, (
        f"ожидалась ровно одна ссылка «{label}» в шапке, найдено {len(matched)}"
    )
    return matched[0]


def test_index_header_brand_nav_and_active_link(web):
    """Шапка «/»: название бренда без подзаголовка, все 7 пунктов меню
    и ровно один активный пункт — «Главная» (href="/")."""
    client, _ = web
    resp = client.get("/")
    assert resp.status_code == 200

    header = _header_region(resp.text)

    # Бренд: заголовок на месте, подзаголовок из шапки убран.
    assert BRAND_TITLE in header
    assert BRAND_SUBTITLE not in header

    # Полный набор пунктов навигации.
    for label in NAV_LABELS:
        assert label in header, f"нет пункта меню «{label}» в шапке"

    # Активен ровно один пункт, и это «Главная» со ссылкой на "/".
    assert header.count('aria-current="page"') == 1
    home = _nav_link(header, "Главная")
    assert 'aria-current="page"' in home
    assert 'href="/"' in home


def test_products_header_active_link(web):
    """Шапка «/products»: единственный активный пункт — «Продукция»."""
    client, _ = web
    resp = client.get("/products")
    assert resp.status_code == 200

    header = _header_region(resp.text)
    assert header.count('aria-current="page"') == 1

    products = _nav_link(header, "Продукция")
    assert 'aria-current="page"' in products
    assert 'href="/products"' in products


# --- Подвал: версия приложения (регрессия) ----------------------------------


def test_base_context_includes_app_version(web):
    """``base_context`` кладёт ``app_version`` из ``APP_VERSION`` (шов шаблонов)."""
    _, conn = web
    ctx = base_context(conn, active="home", page_title="Главная")
    assert ctx["app_version"] == APP_VERSION


def test_index_footer_shows_app_version(web):
    """Отрендеренный подвал «/» содержит строку ``Версия <APP_VERSION>``."""
    client, _ = web
    resp = client.get("/")
    assert resp.status_code == 200

    footer = _footer_region(resp.text)
    assert f"Версия {APP_VERSION}" in footer

