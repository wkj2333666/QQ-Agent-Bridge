"""Tests for persistent global and per-group command access settings."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.command_access_store import (  # type: ignore
    write_command_access_to_config,
)


def test_writes_sorted_global_access_and_group_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("owners: [owner]\nbot: {enabled: true}\n", encoding="utf-8")

    write_command_access_to_config(
        path,
        {
            "task": "user",
            "shell": False,
            "ask": True,
            "code": "owner",
            "groups": True,
        },
        {
            "200": {"task": "disabled", "ask": "owner"},
            "100": {"search": "user"},
        },
    )

    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)

    assert loaded["owners"] == ["owner"]
    assert loaded["bot"] == {"enabled": True}
    assert loaded["commands"] == {
        "ask": True,
        "code": "owner",
        "shell": False,
        "task": "user",
        "groups": {
            "100": {"search": "user"},
            "200": {"ask": "owner", "task": "disabled"},
        },
    }
    assert text.index("  ask: true") < text.index("  code: owner")
    assert text.index("  code: owner") < text.index("  shell: false")
    assert text.index("  shell: false") < text.index("  task: user")
    assert text.index('    "100":') < text.index('    "200":')
    assert '    "100":' in text
    assert '    100:' not in text
    assert "  groups: true" not in text


def test_writes_empty_groups_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    write_command_access_to_config(path, {"ask": True, "shell": "disabled"}, {})

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "commands": {"ask": True, "shell": "disabled", "groups": {}},
    }
    assert "  groups: {}\n" in path.read_text(encoding="utf-8")


def test_replaces_existing_commands_without_duplicate_or_stale_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "owners: [owner]\n"
        "commands:\n"
        "  ask: true\n"
        "  stale: owner\n"
        "  groups:\n"
        '    "old":\n'
        "      task: disabled\n"
        "onebot: {port: 8765}\n",
        encoding="utf-8",
    )

    write_command_access_to_config(
        path,
        {"task": "user"},
        {"new": {"ask": "owner"}},
    )

    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)

    assert text.count("commands:") == 1
    assert loaded["owners"] == ["owner"]
    assert loaded["onebot"] == {"port": 8765}
    assert loaded["commands"] == {
        "task": "user",
        "groups": {"new": {"ask": "owner"}},
    }
