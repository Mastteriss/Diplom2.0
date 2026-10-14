"""Шов рендеринга веб-слоя: единый Jinja2Templates, базовый контекст страниц,
пагинация, расчёт периода и разбор формы.

Все страницы рендерятся через :func:`render`; графики B2 регистрируются как
глобальные функции шаблонов и вызываются прямо из разметки (SVG приходит
готовой строкой и подставляется через ``|safe``).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode

from fastapi import Request
from starlette.templating import Jinja2Templates

from app.config import (
    APP_VERSION,
    DEFAULT_MONTHS,
    MAX_MONTHS,
    MIN_MONTHS,
    MENU,
    PAGE_SIZE,
    TEMPLATES_DIR,
    resolve_db_path,
)
from app.db.connection import get_connection
from app.db.connection import schema_version as _schema_version

from . import deps

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Четыре графика B2 доступны шаблонам напрямую.
templates.env.globals.update(
    pareto_chart=deps.charts.pareto_chart,
    trend_chart=deps.charts.trend_chart,
    spc_chart=deps.charts.spc_chart,
    weight_bar=deps.charts.weight_bar,
)

# Подписи статусов партий и результатов актов ОТК (перечисления contract.md §2.1).
STATUS_LABELS: dict[str, str] = {
    "pending": "Ожидает",
    "accepted": "Принята",
    "partial": "Принята частично",
    "rejected": "Отклонена",
}
templates.env.globals["status_labels"] = STATUS_LABELS


def db_conn() -> sqlite3.Connection:
    """Поток-локальное соединение текущего процесса."""
    return get_connection()


def base_context(conn, *, active, page_title) -> dict:
    """Базовый контекст любой страницы: заголовок, меню, версии, путь к БД."""
    try:
        schema_v = _schema_version(conn) if conn is not None else 0
    except sqlite3.Error:
        schema_v = 0
    try:
        weights = deps.repositories.get_weights(conn) if conn is not None else None
        weights_v = weights.version if weights is not None else None
    except sqlite3.Error:
        weights_v = None
    return {
        "page_title": page_title,
        "active": active,
        "menu": MENU,
        "app_version": APP_VERSION,
        "schema_version": schema_v,
        "weights_version": weights_v,
        "db_path": str(resolve_db_path()),
    }


def render(request, name, *, conn, active, page_title, status_code=200, **context):
    """Рендерит страницу через общий шов (contract.md §9)."""
    ctx = base_context(conn, active=active, page_title=page_title)
    ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def pagination(page, total, page_size: int = PAGE_SIZE):
    """Клампит номер страницы: (страница, страниц всего, сдвиг, назад, вперёд)."""
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    total = int(total or 0)
    if total < 0:
        total = 0
    total_pages = max(1, -(-total // page_size))
    current = max(1, min(current, total_pages))
    offset = (current - 1) * page_size
    return current, total_pages, offset, current > 1, current < total_pages


def parse_months(raw) -> int:
    """Число месяцев из query-параметра: по умолчанию/при ошибке — DEFAULT_MONTHS,
    иначе кламп в MIN_MONTHS..MAX_MONTHS."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MONTHS
    return max(MIN_MONTHS, min(value, MAX_MONTHS))


def _add_months(day: date, delta: int) -> date:
    index = day.year * 12 + (day.month - 1) + delta
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def period_range(months: int) -> tuple[str, str]:
    """Календарный период «months месяцев, включая текущий» → (с, по) в ISO."""
    today = date.today()
    first = today.replace(day=1)
    since = _add_months(first, -(months - 1))
    until = _add_months(first, 1) - timedelta(days=1)
    return since.isoformat(), until.isoformat()


def month_sequence(since: str, until: str) -> list[str]:
    """Список месяцев «YYYY-MM» от since до until включительно."""
    current = date.fromisoformat(since).replace(day=1)
    stop = date.fromisoformat(until).replace(day=1)
    months: list[str] = []
    while current <= stop:
        months.append(f"{current.year:04d}-{current.month:02d}")
        current = _add_months(current, 1)
    return months


def read_form(request: Request) -> dict[str, str]:
    """Тело POST-формы (application/x-www-form-urlencoded) разбирается вручную
    через urllib.parse.parse_qsl — python-multipart не используется (§5.1)."""
    raw = getattr(request.state, "raw_body", None)
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    try:
        pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=2000)
    except ValueError:
        return {}
    result: dict[str, str] = {}
    for key, value in pairs:
        result[key] = value
    return result


def page_base(request: Request, path: str) -> str:
    """Основа ссылки постраничной навигации: путь с текущими query-параметрами
    (без ``page``), всегда заканчивается разделителем ``?`` или ``&``."""
    items = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "page"
    ]
    if not items:
        return f"{path}?"
    return f"{path}?{urlencode(items)}&"
