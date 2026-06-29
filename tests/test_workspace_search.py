"""Bounded workspace search tests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.workspace_search import WorkspaceSearch  # type: ignore


def make_cfg(workspace: Path) -> BridgeConfig:
    cfg = BridgeConfig(
        workspaces={str(workspace): True},
        commands={"search": True},
        max_output_chars=4000,
    )
    cfg.agent.default_workspace = str(workspace)
    return cfg


def test_search_command_uses_literal_query_after_separator(tmp_path: Path) -> None:
    search = WorkspaceSearch(make_cfg(tmp_path))

    argv = search._build_rg_args("--glob *.py")  # noqa: SLF001 - command safety regression

    assert "--fixed-strings" in argv
    assert "--" in argv
    separator = argv.index("--")
    assert argv[separator + 1] == "--glob *.py"


def test_search_returns_matches_but_skips_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle visible\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("needle hidden-token\n", encoding="utf-8")
    (tmp_path / ".env").write_text("needle secret\n", encoding="utf-8")

    search = WorkspaceSearch(make_cfg(tmp_path))

    result = asyncio.run(search.search("needle"))

    assert "src/app.py:1: needle visible" in result
    assert "config.yaml" not in result
    assert ".env" not in result


def test_empty_search_query_returns_usage(tmp_path: Path) -> None:
    search = WorkspaceSearch(make_cfg(tmp_path))

    result = asyncio.run(search.search("   "))

    assert "用法" in result
    assert "/search" in result
