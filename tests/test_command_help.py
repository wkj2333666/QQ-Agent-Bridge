"""Structured command help metadata and rendering tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.command_help import (  # type: ignore
    COMMAND_HELP_METADATA,
    build_command_help,
)
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


COMMAND_NAMES = (
    "ask",
    "plan",
    "search",
    "task",
    "code",
    "status",
    "stop",
    "approve",
    "shell",
    "help",
    "profile",
    "mode",
    "reset",
    "reload",
    "schedule",
    "permission",
)


def make_event(*, group: bool = True, chat_id: str = "group-42") -> ChatEvent:
    return ChatEvent(
        id="message-1",
        platform="qq",
        chat_id=chat_id if group else "user-7",
        sender_id="user-7",
        is_group=group,
        mentioned_bot=True,
        text="/help",
        timestamp=1,
    )


def test_metadata_covers_every_registered_command_with_required_sections() -> None:
    assert set(COMMAND_NAMES) <= set(COMMAND_HELP_METADATA)

    cfg = BridgeConfig(commands={name: True for name in COMMAND_NAMES})
    for name in COMMAND_NAMES:
        reply = build_command_help(name, cfg, make_event())
        assert reply
        assert "用法" in reply
        assert "权限" in reply
        assert f"/{name}" in reply


def test_group_aware_config_access_is_rendered_as_effective_permission() -> None:
    class GroupConfig:
        def command_access(self, name: str, group_id: str | None = None) -> str:
            if name == "task" and group_id == "group-42":
                return "disabled"
            return "user"

    reply = build_command_help("task", GroupConfig(), make_event())

    assert "权限：disabled" in reply
    assert "group-42" in reply
    assert "有效权限" in reply


def test_group_override_falls_back_for_legacy_config_api() -> None:
    class LegacyConfig:
        commands = {"task": True}
        command_groups = {"group-42": {"task": "owner"}}

        def command_access(self, name: str) -> str:
            return "user"

    group_reply = build_command_help("task", LegacyConfig(), make_event())
    private_reply = build_command_help("task", LegacyConfig(), make_event(group=False))

    assert "权限：owner" in group_reply
    assert "权限：user" in private_reply
    assert "私聊按全局配置" in private_reply


def test_schedule_help_includes_current_timezone_and_existing_examples() -> None:
    cfg = BridgeConfig(commands={"schedule": True})
    cfg.scheduler.timezone = "UTC"

    reply = build_command_help("schedule", cfg, make_event())

    assert "UTC" in reply
    assert "/schedule once" in reply
    assert "/schedule list" in reply


def test_unknown_command_returns_friendly_help_prompt() -> None:
    reply = build_command_help("does-not-exist", BridgeConfig(), make_event())

    assert "未知命令" in reply
    assert "/help" in reply
    assert "ask" in reply
