"""Resource staging tests for QQ attachments passed to the agent runtime."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.resources import ResourceManager, format_resource_context  # type: ignore
from qq_agent_bridge.types import ChatEvent, ChatResource  # type: ignore


def make_cfg(workspace: Path) -> BridgeConfig:
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.default_workspace = str(workspace)
    cfg.resources.max_bytes = 1024
    return cfg


def make_ev(resources: tuple[ChatResource, ...], mid: str = "m/1") -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id="100",
        sender_id="200",
        is_group=True,
        mentioned_bot=True,
        text="/ask 看看附件",
        timestamp=1,
        resources=resources,
    )


def test_resource_manager_stages_downloadable_resource_under_workspace(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        assert url == "https://qq.example/cat.jpg"
        assert limit == 1024
        return b"image-bytes", "image/jpeg"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev((ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg"),))

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "image"
    assert refs[0].local_path is not None
    local = tmp_path / refs[0].local_path
    assert local.read_bytes() == b"image-bytes"
    assert local.name.endswith(".jpg")
    assert "cat.jpg" not in refs[0].local_path
    assert tmp_path in local.parents
    assert "downloads" in local.parts
    assert "qq-agent-bridge" in local.parts


def test_resource_manager_stages_qq_voice_with_duration_context(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        assert url == "https://qq.example/voice.silk"
        return b"voice-bytes", "audio/silk"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (
            ChatResource(
                kind="voice",
                url="https://qq.example/voice.silk",
                name="voice.silk",
                duration_seconds=12,
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "voice"
    assert refs[0].duration_seconds == 12
    assert refs[0].local_path is not None
    assert (tmp_path / refs[0].local_path).read_bytes() == b"voice-bytes"
    context = format_resource_context(refs)
    assert "voice:" in context
    assert "duration=12s" in context
    assert "QQ voice limit=60s" in context


def test_resource_manager_keeps_plain_url_without_downloading(tmp_path: Path) -> None:
    called = False

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal called
        called = True
        return b"", "text/plain"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev((ChatResource(kind="url", url="https://example.com/page", name="page"),))

    refs = asyncio.run(manager.prepare(ev))

    assert not called
    assert len(refs) == 1
    assert refs[0].kind == "url"
    assert refs[0].url == "https://example.com/page"
    assert refs[0].local_path is None


def test_resource_manager_sanitizes_names_and_limits_count(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        return b"x", "application/octet-stream"

    cfg = make_cfg(tmp_path)
    cfg.resources.max_items = 1
    manager = ResourceManager(cfg, fetch=fetch)
    ev = make_ev(
        (
            ChatResource(kind="file", url="https://qq.example/1", name="../../secret.txt"),
            ChatResource(kind="file", url="https://qq.example/2", name="second.txt"),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].local_path is not None
    assert Path(refs[0].local_path).name != "secret.txt"
    assert ".." not in refs[0].local_path


def test_resource_manager_does_not_pass_unstaged_attachment_urls(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        raise RuntimeError("download failed")

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev((ChatResource(kind="image", url="https://qq.example/private-image", name="cat.jpg"),))

    refs = asyncio.run(manager.prepare(ev))

    assert refs == ()


def test_resource_manager_formats_forward_chat_record_context_without_downloading(tmp_path: Path) -> None:
    called = False

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal called
        called = True
        return b"", "text/plain"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (
            ChatResource(
                kind="forward",
                file_id="forward-msg-1",
                name="群聊的聊天记录",
                raw_data={
                    "messages": [
                        {
                            "sender_id": "222",
                            "sender_name": "Alice",
                            "text": "第一条 https://example.com/a",
                        },
                        {
                            "sender_id": "333",
                            "sender_name": "Bob",
                            "text": "",
                            "resources": [
                                {
                                    "kind": "image",
                                    "name": "pic.jpg",
                                    "url": "https://qq.example/pic.jpg",
                                }
                            ],
                        },
                    ]
                },
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))
    context = format_resource_context(refs)

    assert not called
    assert len(refs) == 1
    assert refs[0].kind == "forward"
    assert "QQ批量转发：群聊的聊天记录" in context
    assert "Alice(222): 第一条 https://example.com/a" in context
    assert "Bob(333): [image] pic.jpg https://qq.example/pic.jpg" in context


def test_forward_chat_record_context_truncates_long_user_text(tmp_path: Path) -> None:
    manager = ResourceManager(make_cfg(tmp_path))
    long_text = "很长" * 2000
    ev = make_ev(
        (
            ChatResource(
                kind="forward",
                name="超长聊天记录",
                raw_data={
                    "messages": [
                        {
                            "sender_id": "222",
                            "sender_name": "Alice",
                            "text": long_text,
                        }
                    ]
                },
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))
    context = format_resource_context(refs)

    assert len(context) < 1200
    assert long_text not in context
    assert "..." in context
