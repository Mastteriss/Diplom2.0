"""Защита удалённого доступа по общему секрету (contract.md §11).

Режим включается переменной окружения ``DIPL2_ACCESS_KEY``: если она непустая,
любой запрос, кроме страницы входа, выхода, статики и favicon, требует валидной
сессии. Сессия — cookie с HMAC-SHA256 от ключа; сравнение — константное время.
Модуль использует только стандартную библиотеку (``hmac``, ``hashlib``).
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_access_key

from .context import read_form, templates

SESSION_COOKIE: str = "dipl2_session"
SESSION_MESSAGE: bytes = b"diplom-quality-session-v1"
SESSION_MAX_AGE: int = 2592000  # 30 суток

router = APIRouter()


def session_token(key: str) -> str:
    """Значение cookie сессии: HMAC-SHA256(ключ, SESSION_MESSAGE) в hex."""
    return hmac.new(key.encode("utf-8"), SESSION_MESSAGE, hashlib.sha256).hexdigest()


def is_authorized(request: Request) -> bool:
    """True, если защита выключена либо cookie сессии совпадает с токеном ключа."""
    key = get_access_key()
    if key is None:
        return True
    cookie = request.cookies.get(SESSION_COOKIE) or ""
    return hmac.compare_digest(
        cookie.encode("utf-8"), session_token(key).encode("utf-8")
    )


def _is_public(request: Request) -> bool:
    """Пути, доступные без сессии: вход, выход, статика и favicon."""
    path = request.url.path
    method = request.method
    if path == "/login":
        return method in ("GET", "HEAD", "POST")
    if path == "/logout":
        return method == "GET"
    if path == "/favicon.ico":
        return True
    return path == "/static" or path.startswith("/static/")


class AccessKeyMiddleware(BaseHTTPMiddleware):
    """Пускает публичные пути, иначе требует валидную сессию."""

    async def dispatch(self, request: Request, call_next):
        if _is_public(request) or is_authorized(request):
            return await call_next(request)
        return RedirectResponse("/login", status_code=303)


def _render_login(request: Request, *, error: str | None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"page_title": "Вход", "error": error},
        status_code=status_code,
    )


@router.get("/login")
def login_page(request: Request):
    if get_access_key() is None:
        return RedirectResponse("/", status_code=303)
    return _render_login(request, error=None)


@router.post("/login")
def login_submit(request: Request):
    key = get_access_key()
    if key is None:
        return RedirectResponse("/", status_code=303)
    submitted = str(read_form(request).get("access_key") or "")
    if not submitted or not hmac.compare_digest(
        submitted.encode("utf-8"), key.encode("utf-8")
    ):
        return _render_login(request, error="Неверный ключ доступа.", status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_token(key),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
