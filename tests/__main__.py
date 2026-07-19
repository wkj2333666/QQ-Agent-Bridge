"""Tiny stdlib test runner for this dependency-light project."""
from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from typing import Any


def parameterless_tests(module: Any) -> tuple[tuple[str, Callable[[], None]], ...]:
    tests: list[tuple[str, Callable[[], None]]] = []
    for name in sorted(candidate for candidate in dir(module) if candidate.startswith("test_")):
        function = getattr(module, name)
        if not callable(function):
            continue
        try:
            if inspect.signature(function).parameters:
                continue
        except (TypeError, ValueError):
            continue
        tests.append((name, function))
    return tuple(tests)


def main() -> None:
    modules = ("tests.test_policy", "tests.test_onebot", "tests.test_redact")
    for module_name in modules:
        mod = importlib.import_module(module_name)
        for name, function in parameterless_tests(mod):
            function()
            print(f"{module_name}.{name} OK")


if __name__ == "__main__":
    main()
