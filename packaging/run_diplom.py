"""Точка входа замороженной сборки: подготовка окружения, посадка БД из
встроенного образца и запуск uvicorn с объектом ASGI-приложения.

Пути берутся из ``app.config`` (frozen-aware): шаблоны, статика и schema.sql —
из корня ресурсов PyInstaller, файл БД — из записываемого каталога пользователя.
uvicorn запускается с объектом приложения (не со строкой импорта), поэтому
запуск не зависит от исходников вне сборки.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

# Публичный адрес быстрого туннеля Cloudflare в выводе cloudflared.
_TUNNEL_URL_RE = re.compile(r"https://\S+\.trycloudflare\.com")


def _resource_root() -> Path:
    """Корень ресурсов: ``sys._MEIPASS`` в сборке, иначе корень проекта."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        return Path(base) if base else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _ensure_import_path(resource_root: Path) -> None:
    """Вне сборки добавляет корень проекта в sys.path (запуск из packaging/)."""
    if getattr(sys, "frozen", False):
        return
    root = str(resource_root)
    if root not in sys.path:
        sys.path.insert(0, root)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DiplomQuality",
        description=(
            "ИС анализа качества продукции — локальный сервер. "
            "Режимы удалённого доступа: --remote (защищённый вход по ключу) "
            "и --tunnel (публичный туннель Cloudflare)."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="адрес прослушивания")
    parser.add_argument("--port", type=int, default=8000, help="порт прослушивания")
    parser.add_argument(
        "--no-browser", action="store_true", help="не открывать браузер"
    )
    parser.add_argument(
        "--version", action="store_true", help="напечатать версию и выйти"
    )
    parser.add_argument(
        "--data-dir", default=None, help="каталог файла базы данных"
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="защищённый удалённый режим: вход по ключу доступа",
    )
    parser.add_argument(
        "--access-key",
        default=None,
        help="ключ доступа (иначе сгенерируется при --remote)",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="запустить быстрый туннель Cloudflare (требует ключ доступа)",
    )
    return parser


def _apply_data_dir(raw: str | None) -> None:
    """``--data-dir`` задаёт каталог БД через DIPL2_DB ДО импорта приложения."""
    if not raw:
        return
    data_dir = Path(raw).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DIPL2_DB"] = str((data_dir / "quality.sqlite3").resolve())


def _apply_access_key(args: argparse.Namespace) -> tuple[str | None, bool]:
    """Определяет ключ доступа и выставляет DIPL2_ACCESS_KEY до импорта app.

    Приоритет: явный ``--access-key``, затем сгенерированный по ``--remote``,
    затем уже заданный в окружении ``DIPL2_ACCESS_KEY``. Ключ срезается от
    крайних пробелов (как ``get_access_key()``), пробельный ``--access-key``
    считается незаданным. Возвращает пару (ключ, показывать_ли_в_баннере):
    унаследованный из окружения ключ не печатается — пользователь задал его сам.
    """
    explicit = (args.access_key or "").strip() or None
    if explicit:
        key, show = explicit, True
    elif args.remote:
        key, show = secrets.token_urlsafe(24), True
    else:
        key = (os.environ.get("DIPL2_ACCESS_KEY") or "").strip() or None
        show = False
    if key:
        os.environ["DIPL2_ACCESS_KEY"] = key
    return key, show


def _seed_database(resource_root: Path) -> Path:
    """Создаёт каталог БД и копирует встроенный образец, если файла ещё нет.

    Если образца нет — файл создаст ``init_default_db()`` (пустая схема без
    демо-данных). Возвращает итоговый путь к файлу БД.
    """
    from app.config import ensure_data_dir, resolve_db_path

    db_path = resolve_db_path()
    ensure_data_dir()
    if not db_path.exists():
        seed = resource_root / "data" / "quality.sqlite3"
        if seed.exists():
            shutil.copy2(seed, db_path)
    return db_path


def _print_banner(
    host: str,
    port: int,
    db_path: Path,
    *,
    access_key: str | None = None,
    protected: bool = False,
) -> None:
    from app.config import APP_VERSION

    line = "=" * 62
    print(line)
    print(" ИС анализа качества продукции")
    print(f" Версия: {APP_VERSION}")
    print(f" Адрес:  http://{host}:{port}/")
    print(f" База:   {db_path}")
    if protected:
        print(" Режим:  защищённый (вход по ключу)")
        if access_key:
            print(f" Ключ доступа: {access_key}")
        print(" Введите ключ на странице /login")
    print(" Остановить: Ctrl+C")
    print(line)


def _start_tunnel(port: int) -> subprocess.Popen | None:
    """Запускает быстрый туннель Cloudflare и печатает публичный адрес.

    Без ``cloudflared`` в PATH печатает инструкцию и возвращает None (без
    аварии). Вывод читается в демон-потоке, поэтому сервер не блокируется;
    дочерний процесс завершает вызывающая сторона.
    """
    exe = shutil.which("cloudflared")
    if not exe:
        print("cloudflared не найден в PATH — туннель не запущен.")
        print("Установите: scoop install cloudflared")
        print(
            "Или скачайте: "
            "https://developers.cloudflare.com/cloudflare-one/connections/"
            "connect-networks/downloads/"
        )
        return None

    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def _pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                print(line)
            match = _TUNNEL_URL_RE.search(line)
            if match:
                print(f"Публичный адрес: {match.group(0)}")

    threading.Thread(target=_pump, daemon=True).start()
    return proc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    resource_root = _resource_root()
    _ensure_import_path(resource_root)
    _apply_data_dir(args.data_dir)

    if args.version:
        from app.config import APP_VERSION

        print(APP_VERSION)
        return 0

    access_key, show_key = _apply_access_key(args)
    if args.tunnel and not access_key:
        print("Отказ: публичный туннель без ключа доступа недопустим.")
        print(
            "Задайте --remote (сгенерировать ключ), --access-key KEY "
            "или переменную DIPL2_ACCESS_KEY."
        )
        return 2

    db_path = _seed_database(resource_root)

    # Импорт после настройки путей, DIPL2_DB и DIPL2_ACCESS_KEY:
    # StaticFiles/Jinja2Templates проверяют каталоги, соединение читает путь
    # при первом обращении, а режим доступа — переменную окружения.
    from app.main import app

    _print_banner(
        args.host,
        args.port,
        db_path,
        access_key=access_key if show_key else None,
        protected=access_key is not None,
    )

    if not args.no_browser:
        url = f"http://{args.host}:{args.port}/"
        timer = threading.Timer(1.0, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    import uvicorn

    tunnel = _start_tunnel(args.port) if args.tunnel else None
    try:
        # Объект ASGI-приложения и один worker: строка импорта в сборке
        # недоступна.
        uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)
    finally:
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
