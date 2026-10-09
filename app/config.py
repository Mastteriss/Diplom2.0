"""Конфигурация проекта — единый источник истины для путей, констант расчёта
и данных презентационного слоя.

Константы этого модуля совместно используют три слоя: базы данных (путь к
файлу, размер страницы), анализа (точность округления, границы SPC, названия
критериев) и представления (палитра, меню, параметры CSV-выгрузок). Значения
меняются только здесь, чтобы слои не разошлись.

Формы (POST) разбираются вручную через ``urllib.parse.parse_qsl``: проект
сознательно не зависит от ``python-multipart``. Это решение фиксируется как
отдельный ADR.

Модуль не выполняет побочных эффектов при импорте.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Пути -----------------------------------------------------------------
# APP_DIR — каталог app/, PROJECT_DIR — корень проекта (его родитель).
APP_DIR: Path = Path(__file__).resolve().parent
PROJECT_DIR: Path = APP_DIR.parent

ENV_DB_PATH: str = "DIPL2_DB"
DEFAULT_DB_PATH: Path = PROJECT_DIR / "data" / "quality.sqlite3"

TEMPLATES_DIR: Path = APP_DIR / "web" / "templates"
STATIC_DIR: Path = APP_DIR / "web" / "static"


def resolve_db_path() -> Path:
    """Путь к файлу БД: значение DIPL2_DB (относительный путь — от PROJECT_DIR)
    либо DEFAULT_DB_PATH. Ограничений на размещение нет: тесты указывают
    временные каталоги."""
    raw = (os.environ.get(ENV_DB_PATH) or "").strip()
    if not raw:
        return DEFAULT_DB_PATH
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    return candidate.resolve()


def ensure_data_dir() -> Path:
    """Создаёт каталог для текущего файла БД (родитель resolve_db_path())."""
    data_dir = resolve_db_path().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# --- Размеры выборок и периода -------------------------------------------
PAGE_SIZE: int = 50
DEFAULT_MONTHS: int = 12
MIN_MONTHS: int = 1
MAX_MONTHS: int = 36

# --- Точность округления --------------------------------------------------
ROUND_DP: int = 6            # индекс качества и доли
SPC_VALUE_DP: int = 4        # значения контрольных карт
SPC_CPDP: int = 3            # Cp / Cpk
SHARE_DP: int = 4            # доли

# --- Границы контрольных карт SPC -----------------------------------------
SPC_MIN_SUBGROUPS: int = 10
SPC_N_MIN: int = 2
SPC_N_MAX: int = 10

# --- Палитра интерфейса ---------------------------------------------------
# dark   — подвал, первичные кнопки, активный пункт меню
# mid    — акцент, вторичные кнопки, подчёркивание ссылок
# light  — серия графиков №1, информационные бейджи
# bright — положительные индикаторы, бейджи «успех»
PALETTE: dict[str, str] = {
    "dark": "#22682a",
    "mid": "#349e87",
    "light": "#47c3bc",
    "bright": "#91c347",
}

# --- Графики --------------------------------------------------------------
CHART_SERIES: tuple[str, ...] = (
    "#47c3bc",
    "#91c347",
    "#349e87",
    "#22682a",
    "#8a9a94",
    "#b08968",
)
CHART_AXIS_TEXT: str = "#2b3a35"
CHART_GRID: str = "#dfe6e3"
CHART_EMPTY_TEXT: str = "Нет данных для графика"

# --- Навигация: (ключ активности, подпись, абсолютный путь) ---------------
MENU: tuple[tuple[str, str, str], ...] = (
    ("home", "Главная", "/"),
    ("products", "Продукция", "/products"),
    ("batches", "Партии", "/batches"),
    ("pareto", "Дефекты", "/analysis/pareto"),
    ("trends", "Динамика", "/analysis/trends"),
    ("spc", "Контроль процесса", "/analysis/spc"),
    ("config", "Настройки", "/config/weights"),
)

# --- CSV-выгрузки ---------------------------------------------------------
CSV_DELIMITER: str = ";"
CSV_ENCODING: str = "utf-8-sig"
CSV_BATCHES_FILENAME: str = "partii.csv"
CSV_DEFECTS_FILENAME: str = "defekty.csv"

# --- Формы ----------------------------------------------------------------
# Разбор тела формы выполняется вручную через urllib.parse.parse_qsl;
# python-multipart намеренно не используется.
FORM_CSP: str = "application/x-www-form-urlencoded"
