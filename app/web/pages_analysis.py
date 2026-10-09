"""Маршруты аналитических экранов: Парето, динамика, контроль процесса (SPC)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.domain.models import ParameterSpec

from . import deps
from .context import db_conn, month_sequence, parse_months, period_range, render

router = APIRouter()


@router.get("/analysis/pareto")
def pareto_page(request: Request):
    conn = db_conn()
    months = parse_months(request.query_params.get("months"))
    since, until = period_range(months)
    counts = deps.repositories.load_pareto_counts(conn, since=since, until=until)
    result = deps.pareto.compute_pareto(counts, months=months)
    return render(
        request,
        "analysis_pareto.html",
        conn=conn,
        active="pareto",
        page_title="Дефекты (Парето)",
        result=result,
        months=months,
        since=since,
        until=until,
    )


@router.get("/analysis/trends")
def trends_page(request: Request):
    conn = db_conn()
    months = parse_months(request.query_params.get("months"))
    since, until = period_range(months)
    facts = deps.repositories.load_monthly_facts(conn, since=since, until=until)
    points = deps.trends.compute_trends(month_sequence(since, until), facts)
    return render(
        request,
        "analysis_trends.html",
        conn=conn,
        active="trends",
        page_title="Динамика",
        points=points,
        months=months,
        since=since,
        until=until,
    )


def _parameter_spec(row: dict) -> ParameterSpec:
    return ParameterSpec(
        id=int(row.get("id") or 0),
        code=str(row.get("code") or ""),
        name=str(row.get("name") or ""),
        unit=str(row.get("unit") or ""),
        target=row.get("target"),
        lsl=row.get("lsl"),
        usl=row.get("usl"),
        is_critical=bool(row.get("is_critical")),
    )


@router.get("/analysis/spc")
def spc_page(request: Request):
    conn = db_conn()
    months = parse_months(request.query_params.get("months"))
    since, until = period_range(months)
    parameters = deps.repositories.list_parameters(conn, critical_first=True)
    default_code = None
    for row in parameters:
        if row.get("is_critical"):
            default_code = row.get("code")
            break
    if default_code is None and parameters:
        default_code = parameters[0].get("code")

    requested = (request.query_params.get("param") or "").strip() or None
    current = None
    if requested:
        current = deps.repositories.get_parameter_by_code(conn, requested)
    if current is None and default_code:
        current = deps.repositories.get_parameter_by_code(conn, default_code)

    result = None
    if current is not None:
        spec = _parameter_spec(current)
        subgroups = deps.repositories.load_spc_subgroups(
            conn, parameter_id=spec.id, since=since, until=until
        )
        result = deps.spc.compute_spc(
            subgroups, spec, period=f"с {since} по {until}"
        )

    return render(
        request,
        "analysis_spc.html",
        conn=conn,
        active="spc",
        page_title="Контроль процесса",
        parameters=parameters,
        current_code=(current or {}).get("code", ""),
        result=result,
        months=months,
        since=since,
        until=until,
    )
