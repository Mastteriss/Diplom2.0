"""Посадка справочных данных (docs/contract.md §7.1, §8).

Идемпотентно: все вставки — ``INSERT OR IGNORE`` по естественным ключам
(продукция ``sku``, параметры и дефекты ``code``, конфигурация ``id = 1``).
Партии, акты ОТК, дефектные события и измерения здесь НЕ создаются —
их порождает генератор демо-данных (scripts/) через репозитории.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# Продукция: (sku, название, категория, единица). 10 позиций, 4 категории,
# единицы шт./м/кг; SKU-1004 обязателен для посадочных примеров (§8).
_PRODUCTS: tuple[tuple[str, str, str, str], ...] = (
    ("SKU-1001", "Фланец воротниковый ФН-100 DN50", "Металлоизделия", "шт."),
    ("SKU-1002", "Труба стальная бесшовная Ø57×3,5", "Трубы и профиль", "м"),
    ("SKU-1003", "Профиль квадратный 40×40×2", "Трубы и профиль", "м"),
    ("SKU-1004", "Кронштейн опорный КР-12", "Металлоизделия", "шт."),
    ("SKU-1005", "Болт М16×80 М8.8 ГОСТ 7798", "Крепёж и метизы", "шт."),
    ("SKU-1006", "Гайка М16 М8 ГОСТ 5915", "Крепёж и метизы", "шт."),
    ("SKU-1007", "Вал приводной ВП-40", "Детали машин", "шт."),
    ("SKU-1008", "Втулка бронзовая ВЦ-25", "Детали машин", "шт."),
    ("SKU-1009", "Сетка проволочная СР-50", "Металлоизделия", "кг"),
    ("SKU-1010", "Лента стальная 20×2", "Трубы и профиль", "м"),
)

# Параметры: (code, name, unit, target, lsl, usl, is_critical).
# DIAM/BORE/LENGTH/WEIGHT — оба допуска; HARDNESS — только lsl;
# SURFACE — без допусков; критичные: DIAM, BORE, HARDNESS.
_PARAMETERS: tuple[tuple[str, str, str, float | None, float | None, float | None, int], ...] = (
    ("DIAM", "Диаметр наружный", "мм", 50.0, 49.8, 50.2, 1),
    ("BORE", "Диаметр отверстия", "мм", 12.0, 11.9, 12.1, 1),
    ("LENGTH", "Длина", "мм", 200.0, 199.0, 201.0, 0),
    ("WEIGHT", "Масса", "кг", 5.0, 4.8, 5.2, 0),
    ("HARDNESS", "Твёрдость", "HRC", 48.0, 45.0, None, 1),
    ("SURFACE", "Шероховатость поверхности", "мкм", None, None, None, 0),
)

# Дефекты: (code, name, group_name, severity) — ровно 12 кодов,
# все 8 групп contract.md §2.1 покрыты.
_DEFECTS: tuple[tuple[str, str, str, int], ...] = (
    ("SCRATCH", "Царапина на поверхности", "Внешний вид", 2),
    ("DENT", "Вмятина", "Внешний вид", 3),
    ("OVERSIZE", "Превышение наружного диаметра", "Размеры", 4),
    ("UNDERSIZE", "Недобор диаметра отверстия", "Размеры", 4),
    ("CRACK", "Трещина", "Прочность", 5),
    ("POROUS", "Пористость металла", "Материал", 4),
    ("INCLUSION", "Посторонние включения", "Материал", 3),
    ("THREAD", "Повреждение резьбы", "Функциональные", 4),
    ("LEAK", "Негерметичность соединения", "Функциональные", 5),
    ("PACK", "Повреждение упаковки", "Упаковка", 2),
    ("LABEL", "Неверная маркировка", "Сопроводительные документы", 3),
    ("MISC", "Прочий дефект", "Прочее", 1),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed_reference_data(conn: sqlite3.Connection) -> dict[str, int]:
    """Заполняет справочники: продукция, параметры, дефекты, конфиг индекса.

    Идемпотентно (``INSERT OR IGNORE`` по естественным ключам); повторный
    вызов не изменяет и не дублирует данные. Возвращает число строк в каждой
    таблице ПОСЛЕ посадки: ``{"products", "parameters", "defects", "config"}``.
    """
    stamp = _now_iso()
    for sku, name, category, unit in _PRODUCTS:
        conn.execute(
            "INSERT OR IGNORE INTO products "
            "(sku, name, category, unit, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (sku, name, category, unit, stamp),
        )
    for code, name, unit, target, lsl, usl, critical in _PARAMETERS:
        conn.execute(
            "INSERT OR IGNORE INTO parameters_ref "
            "(code, name, unit, target, lsl, usl, is_critical) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, name, unit, target, lsl, usl, critical),
        )
    for code, name, group_name, severity in _DEFECTS:
        conn.execute(
            "INSERT OR IGNORE INTO defects_ref "
            "(code, name, group_name, severity, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (code, name, group_name, severity),
        )
    conn.execute(
        "INSERT OR IGNORE INTO quality_index_config "
        "(id, version, w_defect, w_spec, w_repeat, w_clean, defect_rate_cap, "
        "repeat_rate_cap, min_sample, notes, updated_at, updated_by) "
        "VALUES (1, 1, 0.40, 0.30, 0.15, 0.15, 0.10, 0.20, 10, "
        "'Базовая конфигурация индекса качества', ?, 'system')",
        (stamp,),
    )
    counts = {
        "products": _count(conn, "products"),
        "parameters": _count(conn, "parameters_ref"),
        "defects": _count(conn, "defects_ref"),
        "config": _count(conn, "quality_index_config"),
    }
    return counts


def _count(conn: sqlite3.Connection, table: str) -> int:
    """Число строк справочной таблицы (только известные имена из seed)."""
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
