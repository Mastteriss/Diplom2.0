"""Точка входа FastAPI: фабрика приложения, промежуточное чтение тела формы,
страницы ошибок, статика и маршруты без префиксов (корневые ссылки в шаблонах)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import STATIC_DIR, get_access_key
from app.db.connection import init_default_db
from app.web import (
    auth,
    pages_analysis,
    pages_batches,
    pages_config,
    pages_export,
    pages_index,
)
from app.web.context import templates

_HTTP_MESSAGES = {
    404: "Запрошенная страница не найдена.",
    405: "Такой HTTP-метод не поддерживается.",
    400: "Некорректный запрос.",
}
_ENGLISH_DETAILS = {"", "Not Found", "Method Not Allowed", "Unprocessable Entity"}

# Лимит буферизуемого тела: публичные POST/PUT/PATCH (в т.ч. POST /login)
# не должны позволять анонимному клиенту занять неограниченный объём памяти.
_MAX_BODY_BYTES = 64 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_default_db()
    yield


def _body_too_large() -> JSONResponse:
    """413 до буферизации тела — ответ уходит сразу, без чтения потока."""
    return JSONResponse(
        {"detail": f"Тело запроса слишком велико (лимит {_MAX_BODY_BYTES} байт)."},
        status_code=413,
    )


def _render_error(request: Request, status: int, message: str):
    # Контекст минимальный: страница ошибки отдаётся и без сессии (favicon,
    # отсутствующая статика, 405), поэтому путь к БД и версии схемы/весов
    # в неё не попадают.
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "page_title": f"Ошибка {status}",
            "message": message,
            "status_code": status,
        },
        status_code=status,
    )


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
        if request.method not in ("POST", "PUT", "PATCH"):
            return await call_next(request)
        # Content-Length отсекается до чтения; лимит держится и без него, и при
        # лживой длине: тело читается по частям и 413 уходит, как только объём
        # превысил cap — больше cap в память не буферизуется.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
            return _body_too_large()
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_BODY_BYTES:
                return _body_too_large()
            chunks.append(chunk)
        request.state.raw_body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        status = exc.status_code
        detail = exc.detail if isinstance(getattr(exc, "detail", None), str) else ""
        if detail in _ENGLISH_DETAILS:
            detail = _HTTP_MESSAGES.get(
                status, f"Сервер не смог обработать запрос (код {status})."
            )
        return _render_error(request, status, detail)

    @app.exception_handler(Exception)
    def server_error(request: Request, exc: Exception):
        # Текст исключения наружу не попадает — только на серверный журнал.
        return _render_error(
            request,
            500,
            "Внутренняя ошибка сервера. Повторите попытку позже.",
        )

    if get_access_key() is not None:
        app.add_middleware(auth.AccessKeyMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(auth.router)
    app.include_router(pages_index.router)
    app.include_router(pages_batches.router)
    app.include_router(pages_analysis.router)
    app.include_router(pages_config.router)
    app.include_router(pages_export.router)
    return app


app = create_app()
