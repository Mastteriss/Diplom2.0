"""CSV-выгрузки (contract.md §6): разделитель «;», utf-8-sig, attachment."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Request, Response

from app.config import (
    CSV_BATCHES_FILENAME,
    CSV_DEFECTS_FILENAME,
    CSV_DELIMITER,
    CSV_ENCODING,
    SHARE_DP,
)

from . import deps
from .context import STATUS_LABELS, db_conn, parse_months, period_range

router = APIRouter()


def _csv_response(rows: list[list], filename: str) -> Response:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, dialect="excel")
    writer.writerows(rows)
    payload = buffer.getvalue().encode(CSV_ENCODING)
    return Response(
        payload,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/batches.csv")
def export_batches(request: Request):
    conn = db_conn()
    total = deps.repositories.count_batches(conn)
    batches = deps.repositories.list_batches(conn, limit=max(total, 1), offset=0)
    rows: list[list] = [
        [
            "ID",
            "SKU",
            "Продукция",
            "Линия",
            "Статус",
            "Произведено",
            "Дата",
            "Актов ОТК",
        ]
    ]
    for batch in batches:
        status = batch.get("status")
        rows.append(
            [
                batch.get("id"),
                batch.get("product_sku"),
                batch.get("product_name"),
                batch.get("line"),
                STATUS_LABELS.get(status, status or ""),
                batch.get("produced_qty"),
                batch.get("produced_at"),
                batch.get("inspections_count"),
            ]
        )
    return _csv_response(rows, CSV_BATCHES_FILENAME)


@router.get("/export/defects.csv")
def export_defects(request: Request):
    conn = db_conn()
    months = parse_months(request.query_params.get("months"))
    since, until = period_range(months)
    counts = deps.repositories.load_pareto_counts(conn, since=since, until=until)
    total_qty = sum(defect.qty for defect in counts)
    rows: list[list] = [
        ["Код", "Дефект", "Группа", "Критичность", "Количество", "Доля"]
    ]
    for defect in sorted(counts, key=lambda d: (-d.qty, d.code)):
        share = (defect.qty / total_qty) if total_qty else 0.0
        rows.append(
            [
                defect.code,
                defect.name,
                defect.group_name,
                defect.severity,
                defect.qty,
                f"{share:.{SHARE_DP}f}",
            ]
        )
    return _csv_response(rows, CSV_DEFECTS_FILENAME)
