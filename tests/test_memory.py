"""Conversation memory tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.memory import ConversationMemory, GroupAmbientMemory, should_include_ambient_for_task  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


def make_ev(
    chat_id: str = "group-1",
    sender_id: str = "user-1",
    is_group: bool = True,
) -> ChatEvent:
    return ChatEvent(
        id="m1",
        platform="qq",
        chat_id=chat_id,
        sender_id=sender_id,
        is_group=is_group,
        mentioned_bot=True,
        text="hello",
        timestamp=1,
    )


def test_memory_keys_private_and_group_separately() -> None:
    mem = ConversationMemory(max_messages=4, max_chars=1000)
    group = make_ev(chat_id="g1", sender_id="u1", is_group=True)
    private = make_ev(chat_id="u1", sender_id="u1", is_group=False)

    assert mem.key_for(group) == "group:g1"
    assert mem.key_for(private) == "private:u1"


def test_append_exchange_formats_group_sender_and_assistant() -> None:
    mem = ConversationMemory(max_messages=4, max_chars=1000)
    ev = make_ev(chat_id="g1", sender_id="u1", is_group=True)

    mem.append_exchange(ev, "你好", "你好呀")

    history = mem.format_history(ev)
    assert "u1: 你好" in history
    assert "助手: 你好呀" in history


def test_memory_trims_by_message_count() -> None:
    mem = ConversationMemory(max_messages=2, max_chars=1000)
    ev = make_ev()

    mem.append_exchange(ev, "first", "reply first")
    mem.append_exchange(ev, "second", "reply second")

    history = mem.format_history(ev)
    assert "first" not in history
    assert "second" in history
    assert "reply second" in history


def test_memory_trims_by_character_budget() -> None:
    mem = ConversationMemory(max_messages=10, max_chars=20)
    ev = make_ev()

    mem.append_exchange(ev, "short", "ok")
    mem.append_exchange(ev, "very very long message", "long reply")

    history = mem.format_history(ev)
    assert len(history) <= 20
    assert "long reply" in history


def test_reset_clears_current_conversation_only() -> None:
    mem = ConversationMemory(max_messages=4, max_chars=1000)
    ev1 = make_ev(chat_id="g1")
    ev2 = make_ev(chat_id="g2")

    mem.append_exchange(ev1, "one", "reply one")
    mem.append_exchange(ev2, "two", "reply two")
    mem.reset(ev1)

    assert mem.format_history(ev1) == ""
    assert "reply two" in mem.format_history(ev2)


def test_ambient_memory_records_group_background_separately() -> None:
    mem = GroupAmbientMemory(max_messages=3, max_chars=1000, max_message_chars=40, max_age_seconds=600)
    ev = make_ev(chat_id="g1", sender_id="u1", is_group=True)
    ev = ChatEvent(**{**ev.__dict__, "mentioned_bot": False, "text": "  这个接口有点慢  "})

    assert mem.remember(ev)

    context = mem.format_context(ev)
    assert "u1: 这个接口有点慢" in context


def test_ambient_memory_filters_commands_short_text_and_duplicates() -> None:
    mem = GroupAmbientMemory(
        max_messages=3,
        max_chars=1000,
        max_message_chars=40,
        max_age_seconds=600,
        min_chars=3,
        ignored_prefixes=("/", "／"),
    )
    command = ChatEvent(**{**make_ev().__dict__, "mentioned_bot": False, "text": "/task hello"})
    short = ChatEvent(**{**make_ev().__dict__, "id": "m2", "mentioned_bot": False, "text": "哈"})
    normal = ChatEvent(**{**make_ev().__dict__, "id": "m3", "mentioned_bot": False, "text": "可以先看日志"})

    assert not mem.remember(command)
    assert not mem.remember(short)
    assert mem.remember(normal)
    assert not mem.remember(normal)

    context = mem.format_context(normal)
    assert "/task" not in context
    assert "哈" not in context
    assert context.count("可以先看日志") == 1


def test_ambient_memory_is_group_isolated_and_resettable() -> None:
    mem = GroupAmbientMemory(max_messages=3, max_chars=1000, max_message_chars=40, max_age_seconds=600)
    ev1 = ChatEvent(**{**make_ev(chat_id="g1").__dict__, "mentioned_bot": False, "text": "g1 背景"})
    ev2 = ChatEvent(**{**make_ev(chat_id="g2").__dict__, "id": "m2", "mentioned_bot": False, "text": "g2 背景"})

    mem.remember(ev1)
    mem.remember(ev2)
    mem.reset(ev1)

    assert mem.format_context(ev1) == ""
    assert "g2 背景" in mem.format_context(ev2)


def test_task_ambient_reference_detection() -> None:
    assert should_include_ambient_for_task("根据刚才聊天整理一下")
    assert should_include_ambient_for_task("总结上面他们说的")
    assert not should_include_ambient_for_task("生成一个报告")
    assert not should_include_ambient_for_task("不要根据聊天记录，重新写一版")
