# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH — каталог этого .spec (packaging/); корень проекта — его родитель.
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
APP_ROOT = os.path.join(PROJECT_ROOT, "app")

# Данные кладутся с сохранением структуры app/...: frozen-пути в app.config
# указывают на <sys._MEIPASS>/app/web/templates, .../app/db/schema.sql и
# <sys._MEIPASS>/data/quality.sqlite3.
datas = [
    (os.path.join(APP_ROOT, "web", "templates"), os.path.join("app", "web", "templates")),
    (os.path.join(APP_ROOT, "web", "static"), os.path.join("app", "web", "static")),
    (os.path.join(APP_ROOT, "db", "schema.sql"), os.path.join("app", "db")),
    (os.path.join(PROJECT_ROOT, "data", "quality.sqlite3"), "data"),
]

# uvicorn подгружает loops/protocols/lifespan динамически (через auto),
# поэтому статического анализа мало — собираем подмодули явно.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("app")
    + ["app.main"]
)

a = Analysis(
    [os.path.join(SPECPATH, "run_diplom.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "tests_artifacts", "docs", "scripts"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DiplomQuality",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DiplomQuality",
)
