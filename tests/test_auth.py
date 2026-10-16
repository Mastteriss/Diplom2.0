"""Тесты удалённого доступа в режиме access key (docs/contract.md §10).

Проверяется наблюдаемое поведение через HTTP: коды ответов, заголовки
``Location`` и ``Set-Cookie``, содержимое рендеримых форм. Режим включается
переменной окружения ``DIPL2_ACCESS_KEY``, поэтому каждая фикстура строит
СВЕЖЕЕ приложение (``create_app()``) уже с выставленной переменной:
``AccessKeyMiddleware`` добавляется при сборке приложения, а значение ключа
читается на каждом запросе — фикстура выставляет переменную до сборки и не
трогает её после.

Изоляция БД повторяет приём ``tests/test_web.py`` (conftest.py в проекте нет,
файлы тестов самодостаточны): уникальный файл в ``tests_artifacts/``,
подмена ``app.web.context.get_connection`` на одно соединение с
``check_same_thread=False``, удаление best-effort в teardown. Поставляемая
``data/quality.sqlite3`` не трогается. Переменные окружения возвращает
``monkeypatch``, поэтому тесты независимы и не зависят от порядка.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ACCESS_KEY_ENV, PROJECT_DIR, get_access_key
from app.db.connection import init_db
from app.main import create_app
from app.web import auth

ARTIFACTS_DIR: Path = PROJECT_DIR / "tests_artifacts"

SECRET_KEY = "SECRET42"
SESSION_COOKIE_NAME = "dipl2_session"       # contract.md §10.2
SESSION_MESSAGE_TEXT = b"diplom-quality-session-v1"  # contract.md §10.2

# Валидный токен ключа и «почти валидный» — отличается одним последним
# символом: проверка, что константное сравнение не пропускает похожий HMAC.
_VALID_TOKEN = auth.session_token(SECRET_KEY)
_NEAR_MISS_TOKEN = _VALID_TOKEN[:-1] + ("0" if _VALID_TOKEN[-1] != "0" else "1")


# --- Инфраструктура: временная БД и приложение с нужным режимом доступа ----


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


@contextmanager
def _temp_auth_app(monkeypatch, access_key: str | None):
    """Свежая временная БД + приложение, собранное в заданном режиме доступа.

    ``access_key is None`` — переменная удалена (защита выключена), иначе
    установлена до ``create_app()``, чтобы middleware успел добавиться.
    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    db_path = ARTIFACTS_DIR / f"test_auth_{uuid.uuid4().hex}.sqlite3"
    monkeypatch.setenv("DIPL2_DB", str(db_path))
    if access_key is None:
        monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(ACCESS_KEY_ENV, access_key)
    init_db(db_path)
    conn = _open_shared_connection(db_path)
    try:
        monkeypatch.setattr("app.web.context.get_connection", lambda: conn)
        client = TestClient(create_app())
        with client:
            yield client, conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _remove_db(db_path)


@pytest.fixture()
def auth_on(monkeypatch):
    """Защита включена: приложение собрано с ``DIPL2_ACCESS_KEY=SECRET42``."""
    with _temp_auth_app(monkeypatch, SECRET_KEY) as pair:
        yield pair


# --- §10.5: защита выключена ----------------------------------------------


@pytest.mark.parametrize(
    "env_value",
    [None, "", "   "],
    ids=["unset", "empty", "whitespace"],
)
def test_access_protection_disabled_when_env_blank(monkeypatch, env_value):
    """§10.5: отсутствующая/пустая переменная — поведение прежнее, без редиректов."""
    with _temp_auth_app(monkeypatch, env_value) as (client, _):
        assert get_access_key() is None

        home = client.get("/")
        assert home.status_code == 200
        assert "Главная" in home.text

        login = client.get("/login", follow_redirects=False)
        assert login.status_code == 303
        assert login.headers["location"] == "/"

        # Данные-маршруты открыты без сессии.
        assert client.get("/products").status_code == 200

        # POST /login без включённой защиты ведёт на главную, не 401.
        submit = client.post(
            "/login", data={"access_key": "anything"}, follow_redirects=False
        )
        assert submit.status_code == 303
        assert submit.headers["location"] == "/"


# --- §10.1/§10.3: страницы входа -------------------------------------------


def test_root_redirects_to_login_without_session(auth_on):
    """§10.1: без валидной сессии ``GET /`` — 303 на ``/login``, не главная."""
    client, _ = auth_on
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert "Главная" not in resp.text  # страница не отрендерена


def test_login_page_renders_access_key_form(auth_on):
    """§10.3: ``GET /login`` — 200 с формой поля ``access_key``."""
    client, _ = auth_on
    resp = client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'name="access_key"' in resp.text
    assert 'action="/login"' in resp.text
    assert "Ключ доступа" in resp.text


@pytest.mark.parametrize(
    "payload",
    [{"access_key": "wrong-key"}, {"access_key": ""}, {}],
    ids=["wrong-key", "empty-key", "missing-field"],
)
def test_login_rejects_wrong_key_with_401(auth_on, payload):
    """§10.3: неверный/пустой ключ — 401, повторный рендер формы, без cookie."""
    client, _ = auth_on
    resp = client.post("/login", data=payload, follow_redirects=False)
    assert resp.status_code == 401
    assert "Неверный ключ доступа." in resp.text
    assert 'name="access_key"' in resp.text  # форма отрендерена заново
    assert not any(
        "dipl2_session" in header
        for header in resp.headers.get_list("set-cookie")
    ), "неверный ключ не должен выдавать сессию"


def test_login_accepts_key_and_sets_session_cookie(auth_on):
    """§10.2/§10.3: верный ключ — 303 на ``/`` и Set-Cookie сессии с флагами."""
    client, _ = auth_on
    resp = client.post(
        "/login", data={"access_key": SECRET_KEY}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}={_VALID_TOKEN}" in set_cookie
    for flag in ("HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=lax"):
        assert flag in set_cookie, f"в Set-Cookie нет флага {flag}: {set_cookie}"


def test_home_page_renders_with_valid_session(auth_on):
    """Сессия после ``POST /login`` открывает главную страницу."""
    client, _ = auth_on
    login = client.post(
        "/login", data={"access_key": SECRET_KEY}, follow_redirects=False
    )
    assert login.status_code == 303

    home = client.get("/")  # cookie уже в jar клиента
    assert home.status_code == 200
    assert "Главная" in home.text


def test_valid_session_cookie_header_is_accepted(auth_on):
    """Позитивный контроль для заголовка ``Cookie``: валидный токен пускает.

    Без этого теста проверка подделанных cookie была бы ложно-успешной, если
    бы клиент перестал отправлять переданный заголовок.
    """
    client, _ = auth_on
    resp = client.get(
        "/",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={_VALID_TOKEN}"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Главная" in resp.text


# --- §10.1: закрытые маршруты и статика -----------------------------------


@pytest.mark.parametrize(
    "path",
    ["/products", "/batches", "/export/batches.csv"],
    ids=["products", "batches", "export"],
)
def test_protected_routes_redirect_without_session(auth_on, path):
    """§10.1: любой не-публичный маршрут без сессии — 303 на ``/login``."""
    client, _ = auth_on
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_static_and_favicon_are_open_without_session(auth_on):
    """§10.1: статика и favicon доступны без сессии (не редиректятся на /login)."""
    client, _ = auth_on

    css = client.get("/static/app.css", follow_redirects=False)
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    favicon = client.get("/favicon.ico", follow_redirects=False)
    assert favicon.status_code != 303
    assert favicon.headers.get("location") != "/login"


def test_logout_redirects_and_clears_session_cookie(auth_on):
    """§10.3: ``GET /logout`` — 303 на ``/login`` и удаление cookie (Max-Age=0)."""
    client, _ = auth_on
    login = client.post(
        "/login", data={"access_key": SECRET_KEY}, follow_redirects=False
    )
    assert login.status_code == 303
    assert client.cookies.get(SESSION_COOKIE_NAME) is not None

    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    assert "Max-Age=0" in set_cookie, f"cookie не очищен: {set_cookie}"
    # Клиент забрал удаление: следующий запрос без сессии уходит на /login.
    assert client.cookies.get(SESSION_COOKIE_NAME) is None
    assert client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "forged",
    [
        "",
        "deadbeef" * 8,
        auth.session_token("OTHER-KEY"),
        _NEAR_MISS_TOKEN,
    ],
    ids=["empty", "garbage", "other-key", "near-miss"],
)
def test_forged_session_cookie_is_rejected(auth_on, forged):
    """§10.1/§10.4: подделанный cookie не даёт доступа — 303 на ``/login``."""
    client, _ = auth_on
    resp = client.get(
        "/",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={forged}"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- §10.2/§10.5: токен и чтение ключа ------------------------------------


def test_session_token_is_deterministic_and_matches_contract():
    """§10.2: токен детерминирован и равен HMAC-SHA256(ключ, сообщение) в hex."""
    assert auth.session_token("x") == auth.session_token("x")
    assert auth.session_token("x") != auth.session_token("y")
    assert auth.session_token("") != auth.session_token("x")  # граница: пустой ключ

    for value in ("x", "y", "", SECRET_KEY):
        expected = hmac.new(
            value.encode("utf-8"), SESSION_MESSAGE_TEXT, hashlib.sha256
        ).hexdigest()
        token = auth.session_token(value)
        assert token == expected
        assert re.fullmatch(r"[0-9a-f]{64}", token)


def test_get_access_key_blank_means_disabled(monkeypatch):
    """§10.5: пустая переменная выключает защиту; крайние пробелы срезаются."""
    monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
    assert get_access_key() is None

    for blank in ("", "   ", "\t\n"):
        monkeypatch.setenv(ACCESS_KEY_ENV, blank)
        assert get_access_key() is None

    monkeypatch.setenv(ACCESS_KEY_ENV, "  SECRET42  ")
    assert get_access_key() == "SECRET42"


# --- Hardening tests --------------------------------------------------------


def test_login_oversized_body_returns_413_and_small_wrong_still_401(auth_on):
    """При ключе в окружении POST /login с телом > _MAX_BODY_BYTES → 413.
    Обычный маленький неверный ключ по-прежнему → 401.
    """
    from app.main import _MAX_BODY_BYTES

    client, _ = auth_on
    # Строим oversized body: "access_key=" + "x"*70000
    oversized = "access_key=" + "x" * 70000
    assert len(oversized.encode("utf-8")) > _MAX_BODY_BYTES

    resp = client.post("/login", data=oversized, follow_redirects=False)
    assert resp.status_code == 413

    # Нормальный маленький неверный ключ по-прежнему даёт 401
    resp_small = client.post(
        "/login", data={"access_key": "wrong"}, follow_redirects=False
    )
    assert resp_small.status_code == 401


def test_error_pages_do_not_leak_sensitive_strings(auth_on):
    """Страницы ошибок не содержат чувствительных строк и не редиректят на /login."""
    client, _ = auth_on

    # GET /favicon.ico без сессии — должен быть реальный 404, не редирект на /login
    favicon = client.get("/favicon.ico", follow_redirects=False)
    assert favicon.status_code == 404
    assert favicon.headers.get("location") != "/login"
    body = favicon.text.lower()
    assert "quality.sqlite3" not in body
    assert "schema" not in body
    assert "weights" not in body

    # GET /static/missing_file.txt без сессии — 404, не редирект на /login
    missing = client.get("/static/missing_file.txt", follow_redirects=False)
    assert missing.status_code == 404
    assert missing.headers.get("location") != "/login"
    body_m = missing.text.lower()
    assert "quality.sqlite3" not in body_m
    assert "schema" not in body_m
    assert "weights" not in body_m
