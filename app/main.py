"""Точка входа FastAPI: фабрика приложения, промежуточное чтение тела формы,
страницы ошибок, статика и маршруты без префиксов (корневые ссылки в шаблонах)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import STATIC_DIR
from app.db.connection import init_default_db
from app.web import (
    pages_analysis,
    pages_batches,
    pages_config,
    pages_export,
    pages_index,
)
from app.web.context import db_conn, render

_HTTP_MESSAGES = {
    404: "Запрошенная страница не найдена.",
    405: "Такой HTTP-метод не поддерживается.",
    400: "Некорректный запрос.",
}
_ENGLISH_DETAILS = {"", "Not Found", "Method Not Allowed", "Unprocessable Entity"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_default_db()
    yield


def _safe_conn():
    try:
        return db_conn()
    except Exception:
        return None


def create_app() -> FastAPI:
    app = FastAPI(
        title="ИС анализа качества продукции",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def buffer_form_body(request: Request, call_next):
        # Тело формы буферизуется здесь: обработчики синхронные и разбирают
        # его вручную через urllib.parse.parse_qsl (без python-multipart).
        if request.method in ("POST", "PUT", "PATCH"):
            request.state.raw_body = await request.body()
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        status = exc.status_code
        detail = exc.detail if isinstance(getattr(exc, "detail", None), str) else ""
        if detail in _ENGLISH_DETAILS:
            detail = _HTTP_MESSAGES.get(
                status, f"Сервер не смог обработать запрос (код {status})."
            )
        return render(
            request,
            "error.html",
            conn=_safe_conn(),
            active=None,
            page_title=f"Ошибка {status}",
            message=detail,
            status_code=status,
        )

    @app.exception_handler(Exception)
    def server_error(request: Request, exc: Exception):
        # Текст исключения наружу не попадает — только на серверный журнал.
        return render(
            request,
            "error.html",
            conn=None,
            active=None,
            page_title="Ошибка 500",
            message="Внутренняя ошибка сервера. Повторите попытку позже.",
            status_code=500,
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(pages_index.router)
    app.include_router(pages_batches.router)
    app.include_router(pages_analysis.router)
    app.include_router(pages_config.router)
    app.include_router(pages_export.router)
    return app


app = create_app()
