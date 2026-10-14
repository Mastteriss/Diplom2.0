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
import shutil
import sys
import threading
import webbrowser
from pathlib import Path


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
        description="ИС анализа качества продукции — локальный сервер.",
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
    return parser


def _apply_data_dir(raw: str | None) -> None:
    """``--data-dir`` задаёт каталог БД через DIPL2_DB ДО импорта приложения."""
    if not raw:
        return
    data_dir = Path(raw).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DIPL2_DB"] = str((data_dir / "quality.sqlite3").resolve())


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


def _print_banner(host: str, port: int, db_path: Path) -> None:
    from app.config import APP_VERSION

    line = "=" * 62
    print(line)
    print(" ИС анализа качества продукции")
    print(f" Версия: {APP_VERSION}")
    print(f" Адрес:  http://{host}:{port}/")
    print(f" База:   {db_path}")
    print(" Остановить: Ctrl+C")
    print(line)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    resource_root = _resource_root()
    _ensure_import_path(resource_root)
    _apply_data_dir(args.data_dir)

    if args.version:
        from app.config import APP_VERSION

        print(APP_VERSION)
        return 0

    db_path = _seed_database(resource_root)

    # Импорт после настройки путей и DIPL2_DB: StaticFiles/Jinja2Templates
    # проверяют каталоги, а соединение читает путь при первом обращении.
    from app.main import app

    _print_banner(args.host, args.port, db_path)

    if not args.no_browser:
        url = f"http://{args.host}:{args.port}/"
        timer = threading.Timer(1.0, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    import uvicorn

    # Объект ASGI-приложения и один worker: строка импорта в сборке недоступна.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
