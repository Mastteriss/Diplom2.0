"""Маршрут «/config/weights» — просмотр и правка весов индекса качества
(contract.md §5: GET — 200, POST — 303 при успехе, 400 при ошибке валидации)."""

from __future__ import annotations

import math

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from . import deps
from .context import db_conn, read_form, render

router = APIRouter()

_WEIGHT_FIELDS = (
    ("w_defect", "Вес дефектности"),
    ("w_spec", "Вес соответствия ТЗ"),
    ("w_repeat", "Вес повторяемости"),
    ("w_clean", "Вес чистых партий"),
)
_CAP_FIELDS = (
    ("defect_rate_cap", "Порог дефектности"),
    ("repeat_rate_cap", "Порог повторяемости"),
)
_ALL_NUMERIC_FIELDS = _WEIGHT_FIELDS + _CAP_FIELDS


def _current_values(weights) -> dict[str, str]:
    values = {
        field: f"{getattr(weights, field):g}" for field, _ in _ALL_NUMERIC_FIELDS
    }
    values["min_sample"] = str(weights.min_sample)
    values["author"] = weights.updated_by or ""
    return values


@router.get("/config/weights")
def weights_page(request: Request):
    conn = db_conn()
    weights = deps.repositories.get_weights(conn)
    return render(
        request,
        "config_weights.html",
        conn=conn,
        active="config",
        page_title="Настройки",
        weights=weights,
        values=_current_values(weights),
        errors=[],
    )


@router.post("/config/weights")
def weights_update(request: Request):
    conn = db_conn()
    form = read_form(request)
    values = {
        field: str(form.get(field) or "").strip()
        for field, _ in _ALL_NUMERIC_FIELDS
    }
    values["min_sample"] = str(form.get("min_sample") or "").strip()
    values["author"] = str(form.get("author") or "").strip()

    errors: list[str] = []
    parsed: dict[str, float] = {}
    for field, label in _ALL_NUMERIC_FIELDS:
        text = values[field]
        if not text:
            errors.append(f"{label}: укажите число.")
            continue
        try:
            value = float(text)
        except ValueError:
            errors.append(f"{label}: значение «{text}» не является числом.")
        else:
            if not math.isfinite(value):
                errors.append(f"{label}: значение должно быть конечным числом.")
            else:
                parsed[field] = value

    weight_values = [parsed.get(field) for field, _ in _WEIGHT_FIELDS]
    if all(value is not None for value in weight_values):
        if any(value < 0 for value in weight_values):
            errors.append("Веса не могут быть отрицательными.")
        total = sum(weight_values)
        if abs(total - 1.0) >= 0.000001:
            errors.append(
                f"Сумма четырёх весов должна быть равна 1 (сейчас: {total:.4f})."
            )
    for field, label in _CAP_FIELDS:
        if field in parsed and parsed[field] <= 0:
            errors.append(f"{label} должен быть больше нуля.")

    min_sample = None
    text = values["min_sample"]
    if not text:
        errors.append("Минимальный размер выборки: укажите целое число.")
    else:
        try:
            min_sample = int(text)
        except ValueError:
            errors.append(
                f"Минимальный размер выборки: значение «{text}» не является целым числом."
            )
        else:
            if min_sample < 0:
                errors.append("Минимальный размер выборки не может быть отрицательным.")
                min_sample = None

    author = values["author"]
    if not author:
        errors.append("Укажите автора настроек.")

    weights = deps.repositories.get_weights(conn)
    if errors:
        return render(
            request,
            "config_weights.html",
            conn=conn,
            active="config",
            page_title="Настройки",
            weights=weights,
            values=values,
            errors=errors,
            status_code=400,
        )

    try:
        deps.repositories.update_weights(
            conn,
            author=author,
            w_defect=parsed["w_defect"],
            w_spec=parsed["w_spec"],
            w_repeat=parsed["w_repeat"],
            w_clean=parsed["w_clean"],
            defect_rate_cap=parsed["defect_rate_cap"],
            repeat_rate_cap=parsed["repeat_rate_cap"],
            min_sample=min_sample,
        )
    except ValueError:
        return render(
            request,
            "config_weights.html",
            conn=conn,
            active="config",
            page_title="Настройки",
            weights=weights,
            values=values,
            errors=["Настройки не прошли проверку базы данных."],
            status_code=400,
        )
    return RedirectResponse(url="/config/weights", status_code=303)
