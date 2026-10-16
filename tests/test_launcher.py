"""Тесты лаунчера упаковки (packaging/run_diplom.py).

Проверяется нормализация ключа доступа и запрет туннеля без валидного ключа.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_run_diplom():
    """Загружает packaging/run_diplom.py как модуль (не пакет)."""
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "packaging" / "run_diplom.py"
    spec = importlib.util.spec_from_file_location("run_diplom", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить модуль: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_diplom"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def run_diplom_module(monkeypatch):
    module = _load_run_diplom()
    # Очищаем переменную окружения перед каждым тестом
    monkeypatch.delenv("DIPL2_ACCESS_KEY", raising=False)
    yield module
    monkeypatch.delenv("DIPL2_ACCESS_KEY", raising=False)


def test_apply_access_key_trims_explicit(monkeypatch, run_diplom_module):
    """--access-key с пробелами обрезается, show=True."""
    module = run_diplom_module
    args = argparse.Namespace(access_key="  secret  ", remote=False)
    key, show = module._apply_access_key(args)
    assert key == "secret"
    assert show is True
    assert os.environ.get("DIPL2_ACCESS_KEY") == "secret"
    monkeypatch.delenv("DIPL2_ACCESS_KEY", raising=False)


def test_apply_access_key_whitespace_only_absent_no_remote(monkeypatch, run_diplom_module):
    """Пробельный --access-key считается отсутствующим при remote=False."""
    module = run_diplom_module
    args = argparse.Namespace(access_key="   ", remote=False)
    key, show = module._apply_access_key(args)
    assert key is None
    assert show is False
    assert "DIPL2_ACCESS_KEY" not in os.environ


def test_apply_access_key_whitespace_only_generates_with_remote(monkeypatch, run_diplom_module):
    """Пробельный --access-key при remote=True генерирует ключ."""
    module = run_diplom_module
    args = argparse.Namespace(access_key="   ", remote=True)
    key, show = module._apply_access_key(args)
    assert key is not None
    assert len(key) > 0
    assert show is True
    assert os.environ.get("DIPL2_ACCESS_KEY") == key


def test_apply_access_key_inherits_env_no_flags(monkeypatch, run_diplom_module):
    """Наследует DIPL2_ACCESS_KEY из окружения, show=False."""
    module = run_diplom_module
    monkeypatch.setenv("DIPL2_ACCESS_KEY", "env-key")
    args = argparse.Namespace(access_key=None, remote=False)
    key, show = module._apply_access_key(args)
    assert key == "env-key"
    assert show is False
    assert os.environ.get("DIPL2_ACCESS_KEY") == "env-key"


def test_apply_access_key_sets_env_trimmed(monkeypatch, run_diplom_module):
    """Устанавливает DIPL2_ACCESS_KEY в обрезанном виде."""
    module = run_diplom_module
    args = argparse.Namespace(access_key="  trimmed  ", remote=False)
    key, show = module._apply_access_key(args)
    assert key == "trimmed"
    assert os.environ["DIPL2_ACCESS_KEY"] == "trimmed"


def test_main_tunnel_without_key_refuses(monkeypatch, run_diplom_module):
    """--tunnel без ключа печатает отказ и возвращает 2."""
    module = run_diplom_module
    monkeypatch.delenv("DIPL2_ACCESS_KEY", raising=False)
    result = module.main(["--tunnel", "--no-browser", "--port", "8193"])
    assert result == 2


def test_main_tunnel_with_whitespace_key_refuses(monkeypatch, run_diplom_module):
    """--tunnel с пробельным ключом тоже возвращает 2."""
    module = run_diplom_module
    monkeypatch.delenv("DIPL2_ACCESS_KEY", raising=False)
    result = module.main(["--tunnel", "--access-key", "   ", "--no-browser", "--port", "8194"])
    assert result == 2
