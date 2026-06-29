"""Tiny stdlib test runner for this dependency-light project."""
from __future__ import annotations

import importlib


def main() -> None:
    modules = ("tests.test_policy", "tests.test_onebot", "tests.test_redact")
    for module_name in modules:
        mod = importlib.import_module(module_name)
        for name in sorted(n for n in dir(mod) if n.startswith("test_")):
            getattr(mod, name)()
            print(f"{module_name}.{name} OK")


if __name__ == "__main__":
    main()
