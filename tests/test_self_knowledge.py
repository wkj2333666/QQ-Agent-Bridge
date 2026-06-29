"""Self-knowledge replies for QQ-facing bot identity."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.self_knowledge import (  # type: ignore
    build_help_reply,
    build_prompt_self_knowledge,
    maybe_self_reply,
)
from qq_agent_bridge.types import ChatEvent  # type: ignore


def make_ev(text: str = "你是谁", *, is_group: bool = True, sender: str = "reader") -> ChatEvent:
    return ChatEvent(
        id="m1",
        platform="qq",
        chat_id="group" if is_group else sender,
        sender_id=sender,
        is_group=is_group,
        mentioned_bot=True,
        text=text,
        timestamp=1,
    )


def make_cfg(memory_enabled: bool = True) -> BridgeConfig:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["reader"],
        allowed_groups=["group"],
        commands={
            "ask": True,
            "plan": True,
            "search": True,
            "status": True,
            "help": True,
            "reset": True,
            "code": True,
            "approve": True,
            "stop": True,
            "reload": True,
        },
    )
    cfg.memory.enabled = memory_enabled
    return cfg


def test_self_identity_reply_is_public_safe() -> None:
    reply = maybe_self_reply("你是谁", make_cfg(), make_ev())

    assert reply is not None
    assert "QQ" in reply
    assert "助手" in reply
    for forbidden in ("Cursor", "cursor", "OpenAI", "Claude", "NapCat", "OneBot", "/home/", "token"):
        assert forbidden not in reply


def test_configured_profile_disables_default_identity_quick_reply(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
allowed_users:
  - "reader"
allowed_groups:
  - "group"
profiles:
  default: "你是默认设定"
  groups:
    group: "你是群里的项目管家"
  users:
    reader: "你是私聊里的学习搭子"
""",
        encoding="utf-8",
    )
    cfg = BridgeConfig.load(config_path)

    assert maybe_self_reply("你是谁", cfg, make_ev(is_group=True, sender="reader")) is None
    assert maybe_self_reply("你是谁", cfg, make_ev(is_group=False, sender="reader")) is None


def test_configured_profile_self_knowledge_does_not_restate_default_identity(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
allowed_users:
  - "reader"
allowed_groups:
  - "group"
profiles:
  default: "默认身份 SECRET_DEFAULT"
  groups:
    group: "群身份 SELECTED_GROUP"
    other: "其他群身份 SECRET_OTHER"
  users:
    reader: "私聊身份 SECRET_USER"
commands:
  ask: true
  help: true
""",
        encoding="utf-8",
    )
    cfg = BridgeConfig.load(config_path)

    self_knowledge = build_prompt_self_knowledge(cfg, make_ev(is_group=True, sender="reader"))

    assert "轻量助手" not in self_knowledge
    assert "按“身份与口吻”里的设定介绍自己" in self_knowledge
    assert "SECRET_DEFAULT" not in self_knowledge
    assert "SECRET_OTHER" not in self_knowledge
    assert "SECRET_USER" not in self_knowledge


def test_usage_reply_reflects_group_and_private_context() -> None:
    cfg = make_cfg()

    group_reply = maybe_self_reply("怎么用", cfg, make_ev(is_group=True))
    private_reply = maybe_self_reply("怎么用", cfg, make_ev(is_group=False))

    assert group_reply is not None and "群里 @我" in group_reply
    assert "先发图片/文件" in group_reply
    assert private_reply is not None and "私聊直接问" in private_reply


def test_memory_reply_reflects_config() -> None:
    enabled = maybe_self_reply("有记忆吗", make_cfg(memory_enabled=True), make_ev())
    disabled = maybe_self_reply("有记忆吗", make_cfg(memory_enabled=False), make_ev())

    assert enabled is not None and "短期记忆" in enabled
    assert disabled is not None and "没有开启记忆" in disabled


def test_help_reply_hides_owner_commands_from_non_owner() -> None:
    cfg = make_cfg()

    reader_help = build_help_reply(cfg, make_ev(sender="reader"))
    owner_help = build_help_reply(cfg, make_ev(sender="owner"))

    assert "/ask" in reader_help
    assert "/search" in reader_help
    assert "/reset" not in reader_help
    assert "/code" not in reader_help
    assert "/reload" not in reader_help
    assert "/reset" in owner_help
    assert "/code" in owner_help
    assert "/reload" in owner_help


def test_help_reply_mentions_proactive_chat_when_enabled() -> None:
    cfg = make_cfg()
    cfg.proactive.enabled = True

    reply = build_help_reply(cfg, make_ev(is_group=True))

    assert "偶尔插一句" in reply
    assert "未 @ 的命令不会被执行" in reply
