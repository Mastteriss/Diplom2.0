"""Маршруты справочника продукции, списка партий, карточки партии и формы
регистрации акта ОТК (GET/POST, contract.md §5 и §5.1)."""

from __future__ import annotations

import math
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import DEFAULT_MONTHS, PAGE_SIZE
from app.domain.models import InspectionDraft

from . import deps
from .context import db_conn, page_base, pagination, period_range, read_form, render

router = APIRouter()

_BATCH_STATUSES = ("pending", "accepted", "partial", "rejected")


def _to_int(raw) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


# --- Продукция --------------------------------------------------------------

@router.get("/products")
def products_page(request: Request):
    conn = db_conn()
    query = (request.query_params.get("q") or "").strip() or None
    category = (request.query_params.get("category") or "").strip() or None
    total = deps.repositories.count_products(conn, q=query, category=category)
    page, total_pages, offset, has_prev, has_next = pagination(
        request.query_params.get("page"), total
    )
    products = deps.repositories.list_products(
        conn, q=query, category=category, limit=PAGE_SIZE, offset=offset
    )
    return render(
        request,
        "products.html",
        conn=conn,
        active="products",
        page_title="Продукция",
        products=products,
        categories=deps.repositories.list_categories(conn),
        q=query or "",
        category=category or "",
        page=page,
        total=total,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next,
        page_base=page_base(request, "/products"),
    )


@router.get("/products/{product_id}")
def product_detail_page(request: Request, product_id: str):
    conn = db_conn()
    pid = _to_int(product_id)
    product = deps.repositories.get_product(conn, pid) if pid is not None else None
    if product is None:
        raise HTTPException(404, "Продукция не найдена в справочнике.")
    since, until = period_range(DEFAULT_MONTHS)
    facts = deps.repositories.load_period_facts(conn, since=since, until=until).get(pid)
    index = None
    if facts is not None:
        weights = deps.repositories.get_weights(conn)
        results = deps.quality_index.compute_all(
            {pid: facts},
            weights,
            as_of=date.today(),
            months=DEFAULT_MONTHS,
        )
        index = results[0] if results else None
    return render(
        request,
        "product_detail.html",
        conn=conn,
        active="products",
        page_title="Карточка продукции",
        product=product,
        facts=facts,
        index=index,
        recent_batches=deps.repositories.list_batches(
            conn, product_id=pid, limit=5, offset=0
        ),
        since=since,
        until=until,
    )


# --- Партии -----------------------------------------------------------------

@router.get("/batches")
def batches_page(request: Request):
    conn = db_conn()
    status = request.query_params.get("status") or None
    if status not in _BATCH_STATUSES:
        status = None
    product_id = _to_int(request.query_params.get("product_id"))
    total = deps.repositories.count_batches(conn, product_id=product_id, status=status)
    page, total_pages, offset, has_prev, has_next = pagination(
        request.query_params.get("page"), total
    )
    batches = deps.repositories.list_batches(
        conn, product_id=product_id, status=status, limit=PAGE_SIZE, offset=offset
    )
    product_total = deps.repositories.count_products(conn)
    return render(
        request,
        "batches.html",
        conn=conn,
        active="batches",
        page_title="Партии",
        batches=batches,
        products=deps.repositories.list_products(
            conn, limit=max(product_total, 1), offset=0
        ),
        statuses=_BATCH_STATUSES,
        current_status=status or "",
        current_product_id=product_id,
        page=page,
        total=total,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next,
        page_base=page_base(request, "/batches"),
    )


@router.get("/batches/{batch_id}")
def batch_detail_page(request: Request, batch_id: str):
    conn = db_conn()
    bid = _to_int(batch_id)
    batch = deps.repositories.get_batch(conn, bid) if bid is not None else None
    if batch is None:
        raise HTTPException(404, "Партия не найдена.")
    inspections = deps.repositories.list_inspections(conn, bid)
    inspection_ids = [
        row.get("id") for row in inspections if row.get("id") is not None
    ]
    defects_map = (
        deps.repositories.defects_for_inspections(conn, inspection_ids)
        if inspection_ids
        else {}
    )
    measurements_map = (
        deps.repositories.measurements_for_inspections(conn, inspection_ids)
        if inspection_ids
        else {}
    )
    return render(
        request,
        "batch_detail.html",
        conn=conn,
        active="batches",
        page_title="Партия",
        batch=batch,
        inspections=inspections,
        defects_map=defects_map,
        measurements_map=measurements_map,
    )


# --- Регистрация акта ОТК ---------------------------------------------------

@router.get("/batches/{batch_id}/inspections/new")
def inspection_form_page(request: Request, batch_id: str):
    conn = db_conn()
    bid = _to_int(batch_id)
    batch = deps.repositories.get_batch(conn, bid) if bid is not None else None
    if batch is None:
        raise HTTPException(404, "Партия не найдена.")
    return render(
        request,
        "inspection_form.html",
        conn=conn,
        active="batches",
        page_title="Регистрация акта ОТК",
        batch=batch,
        defects=deps.repositories.list_defects(conn, active_only=True),
        parameters=deps.repositories.list_parameters(conn),
        errors=[],
        values={},
        today=date.today().isoformat(),
    )


def _parse_int(raw, errors: list[str], label: str, *, allow_zero: bool = False) -> int | None:
    text = str(raw or "").strip()
    if not text:
        errors.append(f"{label}: укажите целое число.")
        return None
    try:
        value = int(text)
    except ValueError:
        errors.append(f"{label}: значение «{text}» не является целым числом.")
        return None
    if value > 2_000_000_000 or value < -2_000_000_000:
        errors.append(f"{label}: значение «{text}» выходит за допустимый диапазон.")
        return None
    if not allow_zero and value <= 0:
        errors.append(f"{label}: значение должно быть больше нуля.")
        return None
    if allow_zero and value < 0:
        errors.append(f"{label}: значение не может быть отрицательным.")
        return None
    return value


@router.post("/batches/{batch_id}/inspections/new")
def inspection_create(request: Request, batch_id: str):
    conn = db_conn()
    bid = _to_int(batch_id)
    batch = deps.repositories.get_batch(conn, bid) if bid is not None else None
    if batch is None:
        raise HTTPException(404, "Партия не найдена.")
    form = read_form(request)
    values = dict(form)
    defects = deps.repositories.list_defects(conn, active_only=True)
    parameters = deps.repositories.list_parameters(conn)
    defect_ids = {
        row.get("code"): row.get("id")
        for row in defects
        if row.get("code") and row.get("id") is not None
    }
    defect_names = {row.get("code"): row.get("name") or row.get("code") for row in defects}
    param_ids = {
        row.get("code"): row.get("id")
        for row in parameters
        if row.get("code") and row.get("id") is not None
    }
    param_names = {row.get("code"): row.get("name") or row.get("code") for row in parameters}

    errors: list[str] = []

    inspector = str(form.get("inspector") or "").strip()
    if not inspector:
        errors.append("Укажите инспектора ОТК.")
    elif len(inspector) > 100:
        errors.append("ФИО инспектора — не более 100 символов.")

    inspected_at = ""
    raw_date = str(form.get("inspected_at") or "").strip()
    if not raw_date:
        errors.append("Укажите дату осмотра.")
    else:
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError:
            errors.append("Дата осмотра задана неверно (ожидается формат ГГГГ-ММ-ДД).")
        else:
            if parsed_date > date.today() + timedelta(days=1):
                errors.append("Дата осмотра не может быть позже завтрашнего дня.")
            else:
                inspected_at = parsed_date.isoformat()

    sample_qty = _parse_int(form.get("sample_qty"), errors, "Количество выборки")
    accepted_qty = _parse_int(form.get("accepted_qty"), errors, "Принято", allow_zero=True)
    rejected_qty = _parse_int(form.get("rejected_qty"), errors, "Отклонено", allow_zero=True)
    if (
        sample_qty is not None
        and accepted_qty is not None
        and rejected_qty is not None
        and accepted_qty + rejected_qty != sample_qty
    ):
        errors.append(
            "Сумма принятых и отклонённых единиц должна равняться количеству выборки."
        )

    draft_defects: list[tuple[str, int]] = []
    for key, raw_value in form.items():
        if not key.startswith("defect_") or not str(raw_value).strip():
            continue
        code = key[len("defect_"):]
        label = f"Дефект «{defect_names.get(code, code)}»"
        qty = _parse_int(raw_value, errors, label)
        if qty is not None and qty <= 0:
            errors.append(f"{label}: количество должно быть больше нуля.")
            qty = None
        if qty is not None and code in defect_ids:
            draft_defects.append((code, qty))

    draft_measurements: list[tuple[str, tuple[float, ...]]] = []
    for key, raw_value in form.items():
        if not key.startswith("param_") or not str(raw_value).strip():
            continue
        code = key[len("param_"):]
        label = param_names.get(code, code)
        parts = [part.strip() for part in str(raw_value).split(",")]
        if len(parts) != 5:
            errors.append(
                f"Параметр «{label}»: нужно ровно 5 значений через запятую "
                f"(указано {len(parts)})."
            )
            continue
        numbers: list[float] = []
        bad = False
        for part in parts:
            try:
                value = float(part)
            except ValueError:
                errors.append(
                    f"Параметр «{label}»: значение «{part}» не является числом."
                )
                bad = True
            else:
                if not math.isfinite(value):
                    errors.append(
                        f"Параметр «{label}»: значение «{part}» должно быть "
                        f"конечным числом."
                    )
                    bad = True
                else:
                    numbers.append(value)
        if not bad and code in param_ids:
            draft_measurements.append((code, tuple(numbers)))

    if errors:
        return render(
            request,
            "inspection_form.html",
            conn=conn,
            active="batches",
            page_title="Регистрация акта ОТК",
            batch=batch,
            defects=defects,
            parameters=parameters,
            errors=errors,
            values=values,
            today=date.today().isoformat(),
            status_code=400,
        )

    draft = InspectionDraft(
        inspected_at=inspected_at,
        inspector=inspector,
        sample_qty=sample_qty,
        accepted_qty=accepted_qty,
        rejected_qty=rejected_qty,
        defects=tuple(draft_defects),
        measurements=tuple(draft_measurements),
    )
    deps.repositories.create_inspection(
        conn,
        batch_id=bid,
        draft=draft,
        defects_ref=defect_ids,
        params_ref=param_ids,
    )
    return RedirectResponse(url=f"/batches/{bid}", status_code=303)
