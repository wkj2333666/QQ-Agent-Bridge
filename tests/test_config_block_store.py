"""Regression tests for targeted config.yaml block persistence."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge import config_block_store  # type: ignore
from qq_agent_bridge.config import BridgeConfig, MentionModeConfig, ProfileConfig  # type: ignore
from qq_agent_bridge.mention_mode_store import write_mention_modes_to_config  # type: ignore
from qq_agent_bridge.profile_store import write_profiles_to_config  # type: ignore


def test_rewriting_identical_block_does_not_append_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    modes = MentionModeConfig(default="ask", groups={"group": "task"})
    path.write_text(
        "commands: {ask: true, task: true, mode: true}\n"
        "mention_modes:\n  default: ask\n  groups:\n    \"group\": task\n",
        encoding="utf-8",
    )

    write_mention_modes_to_config(path, modes)
    modes.groups.clear()
    write_mention_modes_to_config(path, modes)

    text = path.read_text(encoding="utf-8")
    assert text.count("mention_modes:") == 1
    assert BridgeConfig.load(path).mention_modes.groups == {}


def test_top_level_comment_inside_block_does_not_leave_stale_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "mention_modes:\n"
        "  default: ask\n"
        "# this comment does not end the YAML mapping\n"
        "  groups:\n"
        "    \"group\": task\n"
        "onebot: {port: 8765}\n",
        encoding="utf-8",
    )

    write_mention_modes_to_config(path, MentionModeConfig())

    text = path.read_text(encoding="utf-8")
    assert BridgeConfig.load(path).mention_modes.groups == {}
    assert text.count("groups:") == 1
    assert "# this comment does not end the YAML mapping" in text


def test_atomic_replace_failure_preserves_original_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    original = "owners: [owner]\nmention_modes:\n  default: ask\n  groups: {}\n"
    path.write_text(original, encoding="utf-8")

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_block_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        write_mention_modes_to_config(
            path,
            MentionModeConfig(default="ask", groups={"group": "task"}),
        )

    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".config.yaml.*.tmp"))


def test_profile_block_scalar_hash_text_does_not_leak_across_scopes(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    profiles = ProfileConfig(
        default="",
        groups={"group": "# GROUP_ONLY\n群聊身份"},
        users={"user": "私聊身份"},
    )
    path.write_text(
        "profiles:\n"
        "  default: \"\"\n"
        "  groups:\n"
        "    \"group\": |\n"
        "      # GROUP_ONLY\n"
        "      群聊身份\n"
        "  users:\n"
        "    \"user\": |\n"
        "      私聊身份\n"
        "onebot: {port: 8765}\n",
        encoding="utf-8",
    )

    write_profiles_to_config(path, profiles)

    loaded = BridgeConfig.load(path)
    assert loaded.profiles.groups["group"] == "# GROUP_ONLY\n群聊身份"
    assert loaded.profiles.users["user"] == "私聊身份"
    assert path.read_text(encoding="utf-8").count("# GROUP_ONLY") == 1
