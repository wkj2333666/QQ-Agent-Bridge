"""Tests for the lightweight ``python -m tests`` runner."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace


def load_runner() -> ModuleType:
    path = Path(__file__).with_name("__main__.py")
    spec = importlib.util.spec_from_file_location("qq_bridge_test_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parameterless_tests_skip_pytest_fixture_functions() -> None:
    def test_plain() -> None:
        return None

    def test_with_fixture(caplog: object) -> None:
        del caplog

    module = SimpleNamespace(
        test_plain=test_plain,
        test_with_fixture=test_with_fixture,
        helper=lambda: None,
    )

    assert load_runner().parameterless_tests(module) == (("test_plain", test_plain),)
