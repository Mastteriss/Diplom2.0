"""Маршрут «/» — сводная страница: KPI-карточки, топ-5 дефектов, ссылки."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.config import DEFAULT_MONTHS

from . import deps
from .context import db_conn, period_range, render

router = APIRouter()


@router.get("/")
def index_page(request: Request):
    conn = db_conn()
    since, until = period_range(DEFAULT_MONTHS)
    stats = deps.repositories.dashboard_stats(conn, since=since, until=until)
    counts = deps.repositories.load_pareto_counts(conn, since=since, until=until)
    total_qty = sum(count.qty for count in counts)
    top = [
        {
            "code": defect.code,
            "name": defect.name,
            "group_name": defect.group_name,
            "qty": defect.qty,
            "share": (defect.qty / total_qty) if total_qty else None,
        }
        for defect in sorted(counts, key=lambda d: (-d.qty, d.code))[:5]
    ]
    return render(
        request,
        "index.html",
        conn=conn,
        active="home",
        page_title="Главная",
        stats=stats,
        top_defects=top,
        since=since,
        until=until,
    )
