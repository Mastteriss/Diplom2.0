"""Подключение к SQLite: поток-локальное соединение, PRAGMA-настройки и
инициализация схемы.

Схема применяется идемпотентно (все объекты создаются через IF NOT EXISTS),
отдельного механизма миграций в v2 нет — ``SCHEMA_VERSION`` фиксирован.
Файлы базы не удаляются и не перемещаются этим модулем.
"""

from __future__ import annotations

import datetime
import sqlite3
import threading
from pathlib import Path

from app.config import PROJECT_DIR, ensure_data_dir, resolve_db_path

SCHEMA_PATH: Path = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION: int = 1

_local = threading.local()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _absolute_path(path: Path | str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    return candidate.resolve()


def connect(path: Path | str) -> sqlite3.Connection:
    """Открывает соединение SQLite с рабочими PRAGMA.

    Включает WAL, synchronous=NORMAL, foreign_keys=ON, busy_timeout=5000,
    row_factory=sqlite3.Row и autocommit (isolation_level=None). Каталог
    файла создаётся при необходимости.
    """
    target = _absolute_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    return conn


def get_connection() -> sqlite3.Connection:
    """Соединение текущего потока по пути из ``resolve_db_path()``.

    Кэшируется по потоку; при смене пути соединение переоткрывается.
    """
    path = resolve_db_path()
    conn = getattr(_local, "conn", None)
    if conn is not None:
        if getattr(_local, "path", None) == path:
            return conn
        conn.close()
    conn = connect(path)
    _local.conn = conn
    _local.path = path
    return conn


def close_connection() -> None:
    """Закрывает соединение текущего потока (файл БД не удаляется)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    _local.conn = None
    _local.path = None
    conn.close()


def init_db(path: Path | str) -> None:
    """Создаёт схему по ``schema.sql`` и записывает строку версии.

    Идемпотентна: повторный вызов ничего не меняет и не дублирует
    ``schema_version``.
    """
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (SCHEMA_VERSION, "baseline", _now_iso()),
        )
    finally:
        conn.close()


def init_default_db() -> None:
    """Инициализирует базу по пути по умолчанию из конфигурации."""
    ensure_data_dir()
    init_db(resolve_db_path())


def schema_version(conn: sqlite3.Connection) -> int:
    """Максимальная версия в ``schema_version``; 0, если таблицы ещё нет."""
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["version"]) if row else 0
