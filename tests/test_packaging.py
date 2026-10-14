"""Тесты упаковки (PyInstaller) и версии приложения.

Проверяется поведение ``app/config.py``, объявленное координатором: единый
``APP_VERSION``, frozen-aware ``RESOURCE_ROOT`` (``sys._MEIPASS``), производные
``TEMPLATES_DIR``/``SCHEMA_PATH``, frozen-aware ``DEFAULT_DB_PATH``
(``%LOCALAPPDATA%\\DiplomQuality\\data``) и сохранение семантики ``DIPL2_DB``
в ``resolve_db_path()``.

Константы ``app.config`` вычисляются на этапе импорта, поэтому frozen-ветка
проверяется отдельным процессом (``reload`` ненадёжен): дочерний интерпретатор
выставляет ``sys.frozen`` и ``sys._MEIPASS`` ДО импорта ``app.config``.
Пути сравниваются через ``os.path.normcase``, чтобы не зависеть от разделителей
и регистра Windows.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from app.config import (
    APP_DIR,
    APP_VERSION,
    DEFAULT_DB_PATH,
    PROJECT_DIR,
    SCHEMA_PATH,
    TEMPLATES_DIR,
    is_frozen,
    resolve_db_path,
)

# Строка, а не константа: имя переменной окружения — часть контракта, её смена
# должна ронять тест, а не молча подстраиваться.
ENV_DB = "DIPL2_DB"


# --- Версия ----------------------------------------------------------------


def test_app_version_is_semver_string():
    """APP_VERSION — непустая строка вида ``X.Y.Z`` (единый источник истины)."""
    assert isinstance(APP_VERSION, str)
    assert APP_VERSION  # непустая
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION), APP_VERSION


# --- Пути вне сборки -------------------------------------------------------


def test_non_frozen_resource_and_db_defaults():
    """Вне сборки RESOURCE_ROOT = PROJECT_DIR, а БД лежит в ``data/`` проекта.

    Проверяются точные значения: производные каталоги обязаны совпадать с
    прежним (не-frozen) поведением, иначе сборка необратимо сломает запуск из
    репозитория.
    """
    assert is_frozen() is False, "тестовый процесс не должен быть frozen"

    assert DEFAULT_DB_PATH == PROJECT_DIR / "data" / "quality.sqlite3"
    assert TEMPLATES_DIR == APP_DIR / "web" / "templates"
    assert SCHEMA_PATH == APP_DIR / "db" / "schema.sql"


# --- resolve_db_path: приоритет DIPL2_DB и границы -------------------------


def test_resolve_db_path_absolute_env(tmp_path, monkeypatch):
    """Абсолютный DIPL2_DB возвращается как есть (после ``resolve()``)."""
    target = tmp_path / "nested" / "custom.sqlite3"
    monkeypatch.setenv(ENV_DB, str(target))

    assert resolve_db_path() == target.resolve()


def test_resolve_db_path_defaults_when_env_unset(monkeypatch):
    """Граница: без DIPL2_DB возвращается DEFAULT_DB_PATH."""
    monkeypatch.delenv(ENV_DB, raising=False)

    assert resolve_db_path() == DEFAULT_DB_PATH


def test_resolve_db_path_relative_against_project_dir(monkeypatch):
    """Относительный DIPL2_DB (вне сборки) разрешается от PROJECT_DIR."""
    monkeypatch.setenv(ENV_DB, "relative.sqlite3")

    assert resolve_db_path() == (PROJECT_DIR / "relative.sqlite3").resolve()


# --- Замороженная сборка (отдельный процесс) -------------------------------


def test_frozen_constants_via_subprocess(tmp_path):
    """Frozen-ветка: _MEIPASS для ресурсов, LOCALAPPDATA для данных.

    Константы вычисляются при импорте ``app.config``, поэтому единственный
    детерминированный способ проверить ветку — дочерний процесс, который
    выставляет ``sys.frozen``/``sys._MEIPASS`` ДО импорта.
    """
    meipass = tmp_path / "meipass"
    localappdata = tmp_path / "LADIR"
    meipass.mkdir()
    localappdata.mkdir()

    code = (
        "import sys;"
        "sys.frozen = True;"
        f"sys._MEIPASS = {str(meipass)!r};"
        "import app.config as c;"
        "print(c.DEFAULT_DB_PATH);"
        "print(c.TEMPLATES_DIR);"
        "print(c.SCHEMA_PATH);"
    )
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(localappdata)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 3, proc.stdout

    default_db, templates, schema = (Path(line) for line in lines)
    norm = os.path.normcase

    # Данные пишутся в записываемый %LOCALAPPDATA%, а не внутрь _MEIPASS.
    assert norm(str(default_db)) == norm(
        str(localappdata / "DiplomQuality" / "data" / "quality.sqlite3")
    )
    # Ресурсы читаются из распакованного _MEIPASS с сохранением структуры.
    assert norm(str(templates)).startswith(norm(str(meipass)))
    assert norm(str(templates)).endswith(
        norm(str(Path("app") / "web" / "templates"))
    )
    assert norm(str(schema)).endswith(
        norm(str(Path("app") / "db" / "schema.sql"))
    )
