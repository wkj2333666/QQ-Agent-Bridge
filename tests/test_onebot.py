"""OneBot event normalization tests."""
from __future__ import annotations

from collections.abc import Awaitable
import logging
import sys
from pathlib import Path
import asyncio
import json

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.onebot import _extract_text, _is_mentioned, _normalize_event  # type: ignore
from qq_agent_bridge.onebot import OneBotAdapter  # type: ignore
from qq_agent_bridge.types import ChatResource, trusted_reply_sender_id


def test_mentioned() -> None:
    assert _is_mentioned([{"type": "at", "data": {"qq": "123"}}], "123")
    assert not _is_mentioned("@示例机器人 /task hello", "123", "示例机器人")
    assert not _is_mentioned("plain", "123")


def test_normalize_private() -> None:
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 42,
        "user_id": 1000000001,
        "self_id": 111,
        "time": 1,
        "message": "hello",
    }
    ev = _normalize_event(raw, "111")
    assert ev is not None
    assert not ev.is_group
    assert ev.text == "hello"
    assert ev.sender_id == "1000000001"


def test_normalize_rejects_wrong_self_id() -> None:
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 42,
        "user_id": 1000000001,
        "self_id": 111,
        "time": 1,
        "message": "hello",
    }
    assert _normalize_event(raw, "222") is None


def test_normalize_rejects_non_numeric_ids() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 42,
        "user_id": "1000000001",
        "group_id": "abc",
        "self_id": 111,
        "time": 1,
        "message": "hello",
    }
    assert _normalize_event(raw, "111") is None


def test_normalize_image_maps_all_napcat_receive_fields() -> None:
    """Per NapCat docs, received image segments carry url, file, file_id,
    path, file_size, file_unique, and mime_type.  file is the basename/UUID;
    file_id is the separate server-assigned ID.  The size field is file_size."""
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {
                "type": "image",
                "data": {
                    "file": "ABCDEF1234567890.jpg",
                    "url": "https://gchat.qpic.cn/download?appid=1407&fileid=x",
                    "file_id": "EhI1ABCDEF1234567890",
                    "path": "/home/napcat/data/image/ABCDEF.jpg",
                    "file_size": 1048576,
                    "file_unique": "a1b2c3d4e5f6",
                    "mime_type": "image/jpeg",
                },
            }
        ],
    }
    ev = _normalize_event(raw, "111")
    assert ev is not None
    assert len(ev.resources) == 1
    r = ev.resources[0]
    assert r.kind == "image"
    assert r.url == "https://gchat.qpic.cn/download?appid=1407&fileid=x"
    # file_size is the correct field name per NapCat docs
    assert r.size == 1048576
    assert r.mime_type == "image/jpeg"


def test_normalize_record_voice_maps_all_napcat_receive_fields() -> None:
    """Per NapCat docs, received record segments have url, file, file_id,
    file_size, and duration on top of the send-side file field."""
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {
                "type": "record",
                "data": {
                    "file": "voice_abcdef.amr",
                    "url": "https://gchat.qpic.cn/voice/download?fileid=xxx",
                    "file_id": "EhIVoiceFileId",
                    "path": "/home/napcat/data/record/voice_abcdef.amr",
                    "file_size": 32768,
                    "file_unique": "voice_unique_123",
                    "duration": 11,
                },
            }
        ],
    }
    ev = _normalize_event(raw, "111")
    assert ev is not None
    assert len(ev.resources) == 1
    r = ev.resources[0]
    assert r.kind == "voice"
    assert r.url == "https://gchat.qpic.cn/voice/download?fileid=xxx"
    assert r.file_id == "EhIVoiceFileId"
    assert r.size == 32768
    assert r.duration_seconds == 11


def test_normalize_video_maps_all_napcat_receive_fields() -> None:
    """Per NapCat docs, received video segments carry url, file, file_id,
    file_size, file_unique."""
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {
                "type": "video",
                "data": {
                    "file": "video_abcdef.mp4",
                    "url": "https://gchat.qpic.cn/video/download?fileid=vid",
                    "file_id": "EhIVideoFileId",
                    "path": "/home/napcat/data/video/video_abcdef.mp4",
                    "file_size": 5242880,
                    "file_unique": "video_unique_456",
                },
            }
        ],
    }
    ev = _normalize_event(raw, "111")
    assert ev is not None
    assert len(ev.resources) == 1
    r = ev.resources[0]
    assert r.kind == "video"
    assert r.url == "https://gchat.qpic.cn/video/download?fileid=vid"
    assert r.file_id == "EhIVideoFileId"
    assert r.size == 5242880


def test_normalize_mixed_message_text_plus_images_and_voice() -> None:
    """Verify all resources are extracted from a mixed message with text,
    multiple images, and a voice — NapCat sends them as a segment array."""
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {"type": "text", "data": {"text": "看看这些照片和语音"}},
            {
                "type": "image",
                "data": {"file": "img1.jpg", "url": "https://example.com/1.jpg"},
            },
            {
                "type": "image",
                "data": {"file": "img2.jpg", "url": "https://example.com/2.jpg"},
            },
            {
                "type": "record",
                "data": {
                    "file": "voice.amr",
                    "url": "https://example.com/voice.amr",
                    "duration": 5,
                },
            },
        ],
    }
    ev = _normalize_event(raw, "111")
    assert ev is not None
    assert ev.text == "看看这些照片和语音"
    kinds = [r.kind for r in ev.resources]
    assert kinds == ["image", "image", "voice"]


def test_normalize_image_without_url_still_creates_resource() -> None:
    """NapCat may not always provide url. The resource must still be created
    so the pipeline can decide how to handle it — not silently dropped at
    the normalization stage."""
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {
                "type": "image",
                "data": {"file": "no_url_image.jpg"},
            }
        ],
    }
    ev = _normalize_event(raw, "111")
    assert ev is not None
    assert len(ev.resources) == 1
    r = ev.resources[0]
    assert r.kind == "image"
    assert r.url is None
    # file_id falls back to file when file_id field is absent
    assert r.file_id == "no_url_image.jpg"


def test_normalize_preserves_image_file_and_url_resources() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 42,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {"text": " 看看这个 https://example.com/a?b=1 "}},
            {
                "type": "image",
                "data": {
                    "file": "cat.jpg",
                    "url": "https://qq.example/image/cat.jpg",
                },
            },
            {
                "type": "file",
                "data": {
                    "file": "notes.pdf",
                    "name": "notes.pdf",
                    "url": "https://qq.example/file/notes.pdf",
                },
            },
        ],
    }

    ev = _normalize_event(raw, "111")

    assert ev is not None
    assert ev.text == "@111  看看这个 https://example.com/a?b=1"
    assert [(r.kind, r.name, r.url) for r in ev.resources] == [
        ("url", "https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("image", "cat.jpg", "https://qq.example/image/cat.jpg"),
        ("file", "notes.pdf", "https://qq.example/file/notes.pdf"),
    ]


def test_normalize_record_segment_as_qq_voice_resource() -> None:
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 49,
        "user_id": 1000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {
                "type": "record",
                "data": {
                    "file": "voice.silk",
                    "url": "https://qq.example/record/voice.silk",
                    "duration": 12,
                    "mime_type": "audio/silk",
                },
            }
        ],
    }

    ev = _normalize_event(raw, "111")

    assert ev is not None
    assert len(ev.resources) == 1
    voice = ev.resources[0]
    assert voice.kind == "voice"
    assert voice.name == "voice.silk"
    assert voice.url == "https://qq.example/record/voice.silk"
    assert voice.duration_seconds == 12
    assert ev.segments[0].type == "voice"


def test_normalize_rejects_text_mention_name_fallback() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 42,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": "@示例机器人 /task hello",
    }

    ev = _normalize_event(raw, "111", "示例机器人")

    assert ev is not None
    assert not ev.mentioned_bot
    assert ev.text == "@示例机器人 /task hello"


def test_normalize_cq_at_string_to_parseable_text() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 43,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": "[CQ:at,qq=111] ask [CQ:at,qq=222] status",
    }

    ev = _normalize_event(raw, "111")

    assert ev is not None
    assert ev.mentioned_bot
    assert ev.text == "@111 ask @222 status"
    assert [(segment.type, segment.qq) for segment in ev.segments] == [
        ("mention", "111"),
        ("text", None),
        ("mention", "222"),
        ("text", None),
    ]


def test_normalize_structured_reply_segment() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 44,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": [
            {"type": "reply", "data": {"id": "43", "qq": "222", "text": "原消息内容"}},
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {"text": " 总结一下"}},
        ],
    }

    ev = _normalize_event(raw, "111")

    assert ev is not None
    assert ev.reply is not None
    assert ev.reply.message_id == "43"
    assert ev.reply.sender_id == "222"
    assert ev.reply.text == "原消息内容"
    assert ev.text == "@111  总结一下"
    assert ev.segments[0].type == "reply"


def test_normalize_cq_reply_string_keeps_reply_id_out_of_user_text() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 45,
        "user_id": 1000000001,
        "group_id": 2000000001,
        "self_id": 111,
        "time": 1,
        "message": "[CQ:reply,id=43][CQ:at,qq=111] 总结一下",
    }

    ev = _normalize_event(raw, "111")

    assert ev is not None
    assert ev.reply is not None
    assert ev.reply.message_id == "43"
    assert ev.text == "@111 总结一下"


def test_normalize_napcat_display_reply_string() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 47,
        "user_id": 1000000004,
        "group_id": 2000000002,
        "self_id": 1000000001,
        "time": 1,
        "message": (
            "[回复消息 [示例用户(1000000002)] 测试一下引用 @X (1000000004)  ] "
            "[CQ:at,qq=1000000002] 这句话是什么意思 [CQ:at,qq=1000000001]"
        ),
    }

    ev = _normalize_event(raw, "1000000001")

    assert ev is not None
    assert ev.mentioned_bot
    assert ev.reply is not None
    assert ev.reply.sender_id == "1000000002"
    assert ev.reply.text == "测试一下引用 @X (1000000004)"
    assert "回复消息" not in ev.text
    assert "这句话是什么意思" in ev.text


def test_normalize_napcat_display_reply_inside_text_segment() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 48,
        "user_id": 1000000004,
        "group_id": 2000000002,
        "self_id": 1000000001,
        "time": 1,
        "message": [
            {
                "type": "text",
                "data": {
                    "text": "[回复消息 [示例用户(1000000002)] 测试一下引用  ] "
                },
            },
            {"type": "at", "data": {"qq": "1000000002"}},
            {"type": "text", "data": {"text": " 这句话是什么意思 "}},
            {"type": "at", "data": {"qq": "1000000001"}},
        ],
    }

    ev = _normalize_event(raw, "1000000001")

    assert ev is not None
    assert ev.reply is not None
    assert ev.reply.sender_id == "1000000002"
    assert ev.reply.text == "测试一下引用"
    assert "回复消息" not in ev.text
    assert "这句话是什么意思" in ev.text


def test_normalize_napcat_forward_json_segment_preserves_chat_record_context() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 60,
        "user_id": 1000000004,
        "group_id": 2000000002,
        "self_id": 1000000001,
        "time": 1,
        "message": [
            {"type": "at", "data": {"qq": "1000000001"}},
            {"type": "text", "data": {"text": " 总结这个聊天记录 "}},
            {
                "type": "json",
                "data": {
                    "data": json.dumps(
                        {
                            "app": "com.tencent.multimsg",
                            "prompt": "[聊天记录]",
                            "meta": {
                                "detail": {
                                    "resid": "forward-resid-1",
                                    "summary": "群聊的聊天记录",
                                    "news": [
                                        {"text": "Alice: 第一条 https://example.com/a"},
                                        {"text": "Bob: 第二条"},
                                    ],
                                }
                            },
                        },
                        ensure_ascii=False,
                    )
                },
            },
        ],
    }

    ev = _normalize_event(raw, "1000000001")

    assert ev is not None
    assert ev.mentioned_bot
    assert ev.text == "@1000000001  总结这个聊天记录"
    assert ev.segments[-1].type == "forward"
    assert len(ev.resources) == 1
    forward = ev.resources[0]
    assert forward.kind == "forward"
    assert forward.file_id == "forward-resid-1"
    assert forward.name == "群聊的聊天记录"
    assert forward.raw_data["messages"] == [
        {"text": "Alice: 第一条 https://example.com/a"},
        {"text": "Bob: 第二条"},
    ]


def test_normalize_forward_segment_marks_downloadable_merged_record_without_fetching() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 61,
        "user_id": 1000000004,
        "group_id": 2000000002,
        "self_id": 1000000001,
        "time": 1,
        "message": [
            {"type": "at", "data": {"qq": "1000000001"}},
            {"type": "text", "data": {"text": " 看下 "}},
            {
                "type": "forward",
                "data": {
                    "id": "forward-msg-1",
                    "summary": "3条转发消息",
                },
            },
        ],
    }

    ev = _normalize_event(raw, "1000000001")

    assert ev is not None
    assert ev.text == "@1000000001  看下"
    assert ev.resources[0].kind == "forward"
    assert ev.resources[0].file_id == "forward-msg-1"
    assert ev.resources[0].raw_data["messages"] == []


class FakeConn:
    def __init__(self) -> None:
        self.frames: list[str] = []

    async def send(self, data: str) -> None:
        self.frames.append(data)


async def start_onebot_send(
    adapter: OneBotAdapter,
    conn: FakeConn,
    send: Awaitable[None],
) -> tuple[asyncio.Task[None], dict[str, object]]:
    task = asyncio.create_task(send)
    for _ in range(10):
        if conn.frames:
            break
        await asyncio.sleep(0)
    assert conn.frames
    return task, json.loads(conn.frames[-1])


async def complete_successful_send(
    adapter: OneBotAdapter,
    conn: FakeConn,
    send: Awaitable[None],
) -> dict[str, object]:
    task, frame = await start_onebot_send(adapter, conn, send)
    assert not task.done()
    assert adapter._complete_action_response(  # type: ignore[attr-defined]
        {"echo": frame["echo"], "status": "ok", "retcode": 0, "data": {}}
    )
    await task
    return frame


def test_send_waits_for_matching_success_ack() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send("123", True, "hello", "send-ack"),
        )

        assert frame["action"] == "send_group_msg"
        assert frame["echo"] == "send-ack"
        assert adapter._pending_actions == {}  # type: ignore[attr-defined]
        assert adapter._pending_action_connections == {}  # type: ignore[attr-defined]

    asyncio.run(go())


def test_send_fails_without_gateway() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")

        with pytest.raises(ConnectionError):
            await adapter.send("123", True, "hello", "no-gateway")

    asyncio.run(go())


def test_send_reraises_transport_error_without_logging_exception_text(caplog: object) -> None:
    class FailingConn(FakeConn):
        async def send(self, data: str) -> None:
            raise RuntimeError("sensitive transport detail")

    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        adapter._conns.add(FailingConn())  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="sensitive transport detail"):
            await adapter.send("123", True, "hello", "transport-error")
        assert adapter._pending_actions == {}  # type: ignore[attr-defined]
        assert adapter._pending_action_connections == {}  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="qq_agent_bridge.onebot"):  # type: ignore[attr-defined]
        asyncio.run(go())

    assert "sensitive transport detail" not in caplog.text  # type: ignore[attr-defined]
    assert "RuntimeError" in caplog.text  # type: ignore[attr-defined]


def test_send_fails_on_ack_timeout(monkeypatch: object) -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        monkeypatch.setattr(  # type: ignore[attr-defined]
            OneBotAdapter,
            "SEND_ACTION_TIMEOUT_SECONDS",
            0.01,
        )

        with pytest.raises(asyncio.TimeoutError):
            await adapter.send("123", True, "hello", "timeout")
        assert adapter._pending_actions == {}  # type: ignore[attr-defined]
        assert adapter._pending_action_connections == {}  # type: ignore[attr-defined]

    asyncio.run(go())


def test_only_selected_connection_can_complete_action_response() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        first = FakeConn()
        second = FakeConn()
        adapter._conns.update((first, second))  # type: ignore[arg-type]

        task = asyncio.create_task(adapter.send("123", True, "hello", "bound-ack"))
        for _ in range(10):
            if first.frames or second.frames:
                break
            await asyncio.sleep(0)
        selected = first if first.frames else second
        wrong = second if selected is first else first
        frame = json.loads(selected.frames[0])
        response = {"echo": frame["echo"], "status": "ok", "retcode": 0, "data": {}}

        assert not adapter._complete_action_response(response, wrong)  # type: ignore[arg-type,attr-defined]
        assert not task.done()
        assert "bound-ack" in adapter._pending_actions  # type: ignore[attr-defined]
        assert adapter._complete_action_response(response, selected)  # type: ignore[arg-type,attr-defined]
        await task
        assert adapter._pending_actions == {}  # type: ignore[attr-defined]
        assert adapter._pending_action_connections == {}  # type: ignore[attr-defined]

    asyncio.run(go())


def test_disconnect_promptly_fails_pending_send_and_cleans_future() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        first = FakeConn()
        second = FakeConn()
        adapter._conns.update((first, second))  # type: ignore[arg-type]

        task = asyncio.create_task(adapter.send("123", True, "hello", "disconnect"))
        for _ in range(10):
            if first.frames or second.frames:
                break
            await asyncio.sleep(0)
        selected = first if first.frames else second
        pending = adapter._pending_actions["disconnect"]  # type: ignore[attr-defined]

        adapter._remove_connection(selected)  # type: ignore[arg-type,attr-defined]

        assert pending.done()
        assert adapter._pending_actions == {}  # type: ignore[attr-defined]
        assert adapter._pending_action_connections == {}  # type: ignore[attr-defined]
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, timeout=0.05)
        assert selected not in adapter._conns  # type: ignore[operator,attr-defined]

    asyncio.run(go())


def test_send_cancellation_cleans_pending_future() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        task, _frame = await start_onebot_send(
            adapter,
            conn,
            adapter.send("123", True, "hello", "cancelled-send"),
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter._pending_actions == {}  # type: ignore[attr-defined]
        assert adapter._pending_action_connections == {}  # type: ignore[attr-defined]

    asyncio.run(go())


def test_send_fails_on_non_ok_status() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        task, frame = await start_onebot_send(
            adapter,
            conn,
            adapter.send("123", True, "hello", "bad-status"),
        )
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {"echo": frame["echo"], "status": "failed", "retcode": 0, "data": {}}
        )

        with pytest.raises(RuntimeError):
            await task

    asyncio.run(go())


def test_send_fails_on_nonzero_retcode() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        task, frame = await start_onebot_send(
            adapter,
            conn,
            adapter.send("123", True, "hello", "bad-retcode"),
        )
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {"echo": frame["echo"], "status": "ok", "retcode": 100, "data": {}}
        )

        with pytest.raises(RuntimeError):
            await task

    asyncio.run(go())


def test_resolve_record_url_uses_file_id_and_returns_response_file() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        resource = ChatResource(kind="voice", file_id="voice.silk", url="https://qq.example/input.silk")

        task = asyncio.create_task(adapter.resolve_record_url(resource))
        await asyncio.sleep(0)

        frame = json.loads(conn.frames[0])
        assert frame["action"] == "get_record"
        assert frame["params"] == {"file": "voice.silk", "out_format": "wav"}
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {"file": "https://qq.example/voice.wav"},
            }
        )

        assert await task == "https://qq.example/voice.wav"

    asyncio.run(go())


def test_resolve_record_url_uses_url_when_file_id_is_missing() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        resource = ChatResource(kind="voice", url="https://qq.example/input.silk")

        task = asyncio.create_task(adapter.resolve_record_url(resource))
        await asyncio.sleep(0)

        frame = json.loads(conn.frames[0])
        assert frame["params"] == {"file": "https://qq.example/input.silk", "out_format": "wav"}
        adapter._complete_action_response(
            {"echo": frame["echo"], "status": "ok", "retcode": 0, "data": {"url": "out.wav"}}
        )

        assert await task == "out.wav"

    asyncio.run(go())


def test_resolve_record_url_prefers_url_then_file_then_path() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        resource = ChatResource(kind="voice", file_id="voice.silk")

        task = asyncio.create_task(adapter.resolve_record_url(resource))
        await asyncio.sleep(0)
        frame = json.loads(conn.frames[0])
        adapter._complete_action_response(
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {"url": "converted-url", "file": "converted-file", "path": "converted.wav"},
            }
        )

        assert await task == "converted-url"

    asyncio.run(go())


def test_resolve_record_url_returns_none_without_a_non_empty_string() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        resource = ChatResource(kind="voice", file_id="voice.silk")

        task = asyncio.create_task(adapter.resolve_record_url(resource))
        await asyncio.sleep(0)
        frame = json.loads(conn.frames[0])
        adapter._complete_action_response(
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {"file": "", "url": None, "path": 456},
            }
        )

        assert await task is None

    asyncio.run(go())


def test_send_image_uses_onebot_image_segment(tmp_path: Path) -> None:
    async def go() -> None:
        image = tmp_path / "plot.png"
        image.write_bytes(b"png")
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send_image("123", True, image, "img-echo"),
        )
        assert frame["action"] == "send_group_msg"
        assert frame["params"]["group_id"] == 123
        assert frame["params"]["message"][0]["type"] == "image"
        assert frame["params"]["message"][0]["data"]["file"] == image.resolve().as_uri()
        assert frame["echo"] == "img-echo"

    asyncio.run(go())


def test_adapter_fetches_json_forward_even_when_preview_messages_exist() -> None:
    async def go() -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 63,
            "user_id": 1000000004,
            "group_id": 2000000002,
            "self_id": 1000000001,
            "time": 1,
            "message": [
                {"type": "at", "data": {"qq": "1000000001"}},
                {
                    "type": "json",
                    "data": {
                        "data": json.dumps(
                            {
                                "app": "com.tencent.multimsg",
                                "prompt": "[聊天记录]",
                                "meta": {
                                    "detail": {
                                        "resid": "forward-json-1",
                                        "summary": "群聊的聊天记录",
                                        "news": [{"text": "preview only"}],
                                    }
                                },
                            },
                            ensure_ascii=False,
                        )
                    },
                },
            ],
        }
        ev = _normalize_event(raw, "1000000001")
        assert ev is not None
        assert ev.resources[0].raw_data["messages"] == [{"text": "preview only"}]

        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "1000000001")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        task = asyncio.create_task(adapter._enrich_forward(ev))  # type: ignore[attr-defined]
        await asyncio.sleep(0)

        frame = json.loads(conn.frames[0])
        assert frame["action"] == "get_forward_msg"
        assert frame["params"] == {"message_id": "forward-json-1", "id": "forward-json-1"}
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {
                    "messages": [
                        {
                            "sender": {"user_id": 222, "nickname": "Alice"},
                            "message": [{"type": "text", "data": {"text": "完整内容"}}],
                        }
                    ]
                },
            }
        )

        enriched = await task
        forward = enriched.resources[0]
        assert forward.raw_data["source"] == "onebot-forward-fetched"
        assert forward.raw_data["messages"] == [
            {"sender_id": "222", "sender_name": "Alice", "text": "完整内容"}
        ]

    asyncio.run(go())


def test_send_file_uses_upload_action(tmp_path: Path) -> None:
    async def go() -> None:
        report = tmp_path / "report.pdf"
        report.write_bytes(b"pdf")
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send_file("123", True, report, "file-echo"),
        )
        assert frame["action"] == "upload_group_file"
        assert frame["params"]["group_id"] == 123
        assert frame["params"]["file"] == report.resolve().as_uri()
        assert frame["params"]["name"] == "report.pdf"
        assert frame["echo"] == "file-echo"

    asyncio.run(go())


def test_send_voice_uses_onebot_record_segment(tmp_path: Path) -> None:
    async def go() -> None:
        voice = tmp_path / "reply.silk"
        voice.write_bytes(b"silk")
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send_voice("123", False, voice, "voice-echo"),
        )
        assert frame["action"] == "send_private_msg"
        assert frame["params"]["user_id"] == 123
        assert frame["params"]["message"][0]["type"] == "record"
        assert frame["params"]["message"][0]["data"]["file"] == voice.resolve().as_uri()
        assert frame["echo"] == "voice-echo"

    asyncio.run(go())


def test_send_at_uses_onebot_at_segment() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send_at("123", "456", "这句接得上", "at-echo"),
        )
        assert frame["action"] == "send_group_msg"
        assert frame["params"]["group_id"] == 123
        assert frame["params"]["message"] == [
            {"type": "at", "data": {"qq": 456}},
            {"type": "text", "data": {"text": " 这句接得上"}},
        ]
        assert frame["echo"] == "at-echo"

    asyncio.run(go())


def test_send_text_can_quote_message() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send("123", True, "这句接得上", "quote-echo", reply_to="43"),
        )
        assert frame["action"] == "send_group_msg"
        assert frame["params"]["group_id"] == 123
        assert frame["params"]["message"] == [
            {"type": "reply", "data": {"id": 43}},
            {"type": "text", "data": {"text": "这句接得上"}},
        ]
        assert frame["echo"] == "quote-echo"

    asyncio.run(go())


def test_send_ats_uses_multiple_onebot_at_segments() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send_ats("123", ("456", "789"), "都出来冒个泡", "ats-echo"),
        )
        assert frame["action"] == "send_group_msg"
        assert frame["params"]["group_id"] == 123
        assert frame["params"]["message"] == [
            {"type": "at", "data": {"qq": 456}},
            {"type": "at", "data": {"qq": 789}},
            {"type": "text", "data": {"text": " 都出来冒个泡"}},
        ]
        assert frame["echo"] == "ats-echo"

    asyncio.run(go())


def test_send_ats_can_quote_message_before_mentions() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        frame = await complete_successful_send(
            adapter,
            conn,
            adapter.send_ats(
                "123",
                ("456", "789"),
                "都出来冒个泡",
                "ats-quote",
                reply_to="43",
            ),
        )
        assert frame["action"] == "send_group_msg"
        assert frame["params"]["group_id"] == 123
        assert frame["params"]["message"] == [
            {"type": "reply", "data": {"id": 43}},
            {"type": "at", "data": {"qq": 456}},
            {"type": "at", "data": {"qq": 789}},
            {"type": "text", "data": {"text": " 都出来冒个泡"}},
        ]
        assert frame["echo"] == "ats-quote"

    asyncio.run(go())


def test_adapter_enriches_reply_by_fetching_quoted_message() -> None:
    async def go() -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 46,
            "user_id": 1000000001,
            "group_id": 2000000001,
            "self_id": 111,
            "time": 1,
            "message": [
                {"type": "reply", "data": {"id": "43"}},
                {"type": "at", "data": {"qq": "111"}},
                {"type": "text", "data": {"text": " 看看"}},
            ],
        }
        ev = _normalize_event(raw, "111")
        assert ev is not None
        assert ev.reply is not None
        assert ev.reply.text == ""

        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        task = asyncio.create_task(adapter._enrich_reply(ev))  # type: ignore[attr-defined]
        await asyncio.sleep(0)

        frame = json.loads(conn.frames[0])
        assert frame["action"] == "get_msg"
        assert frame["params"]["message_id"] == 43
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 43,
                    "sender": {"user_id": 222},
                    "message": [{"type": "text", "data": {"text": "被引用的原文"}}],
                    "raw_message": "被引用的原文",
                },
            }
        )

        enriched = await task
        assert enriched.reply is not None
        assert enriched.reply.message_id == "43"
        assert enriched.reply.sender_id == "222"
        assert enriched.reply.text == "被引用的原文"
        assert trusted_reply_sender_id(enriched.reply) == "222"

    asyncio.run(go())


def test_adapter_enriches_reply_voice_by_fetching_when_preview_text_present() -> None:
    async def go() -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 47,
            "user_id": 1000000001,
            "group_id": 2000000001,
            "self_id": 111,
            "time": 1,
            "message": [
                {"type": "reply", "data": {"id": "44", "text": "[语音]"}},
                {"type": "at", "data": {"qq": "111"}},
                {"type": "text", "data": {"text": " 这条语音说了啥"}},
            ],
        }
        ev = _normalize_event(raw, "111")
        assert ev is not None
        assert ev.reply is not None
        assert ev.reply.text == "[语音]"
        assert ev.resources == ()

        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        task = asyncio.create_task(adapter._enrich_reply(ev))  # type: ignore[attr-defined]
        await asyncio.sleep(0)

        frame = json.loads(conn.frames[0])
        assert frame["action"] == "get_msg"
        assert frame["params"]["message_id"] == 44
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 44,
                    "sender": {"user_id": 222},
                    "message": [
                        {
                            "type": "record",
                            "data": {
                                "file": "voice.silk",
                                "url": "https://qq.example/record/voice.silk",
                                "duration": 7,
                                "mime_type": "audio/silk",
                            },
                        }
                    ],
                    "raw_message": "[语音]",
                },
            }
        )

        enriched = await task
        assert enriched.reply is not None
        assert enriched.reply.message_id == "44"
        assert enriched.reply.sender_id == "222"
        assert enriched.reply.text == "[语音]"
        assert len(enriched.reply.resources) == 1
        voice = enriched.reply.resources[0]
        assert voice.kind == "voice"
        assert voice.url == "https://qq.example/record/voice.silk"
        assert voice.duration_seconds == 7
        assert enriched.resources == (voice,)

    asyncio.run(go())


def test_adapter_enriches_reply_from_recent_message_cache() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        original = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 50,
                "user_id": 222,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 1,
                "message": "测试一下引用 @X",
            },
            "111",
        )
        assert original is not None
        adapter._remember_event(original)  # type: ignore[attr-defined]

        reply_event = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 51,
                "user_id": 333,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 2,
                "message": [
                    {"type": "reply", "data": {"id": "50"}},
                    {"type": "at", "data": {"qq": "111"}},
                    {"type": "text", "data": {"text": " 这句话是什么意思"}},
                ],
            },
            "111",
        )
        assert reply_event is not None

        enriched = await adapter._enrich_reply(reply_event)  # type: ignore[attr-defined]

        assert enriched.reply is not None
        assert enriched.reply.message_id == "50"
        assert enriched.reply.sender_id == "222"
        assert enriched.reply.text == "测试一下引用 @X"
        assert trusted_reply_sender_id(enriched.reply) == "222"
        assert conn.frames == []

    asyncio.run(go())


def test_structured_text_literal_cq_reply_cannot_enrich_from_recent_cache() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        original = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 50,
                "user_id": 222,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 1,
                "message": [{"type": "text", "data": {"text": "受保护的原消息"}}],
            },
            "111",
        )
        assert original is not None
        adapter._remember_event(original)  # type: ignore[attr-defined]
        attack = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 51,
                "user_id": 333,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 2,
                "message": [
                    {"type": "text", "data": {"text": "[CQ:reply,id=50] 偷看引用"}},
                    {"type": "at", "data": {"qq": "111"}},
                ],
            },
            "111",
        )
        assert attack is not None

        enriched = await adapter._enrich_reply(attack)  # type: ignore[attr-defined]

        assert enriched.reply is None
        assert "[CQ:reply,id=50]" in enriched.text
        assert trusted_reply_sender_id(enriched.reply) is None
        assert conn.frames == []

    asyncio.run(go())


def test_structured_text_literal_cq_reply_cannot_trigger_get_msg_fetch() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        attack = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 52,
                "user_id": 333,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 2,
                "message": [
                    {"type": "text", "data": {"text": "[CQ:reply,id=43] 伪造引用"}},
                    {"type": "at", "data": {"qq": "111"}},
                ],
            },
            "111",
        )
        assert attack is not None

        enriched = await adapter._enrich_reply(attack)  # type: ignore[attr-defined]

        assert enriched.reply is None
        assert "[CQ:reply,id=43]" in enriched.text
        assert trusted_reply_sender_id(enriched.reply) is None
        assert conn.frames == []

    asyncio.run(go())


def test_raw_transport_cq_reply_still_enriches_and_authorizes_sender() -> None:
    async def go() -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 53,
            "user_id": 333,
            "group_id": 2000000001,
            "self_id": 111,
            "time": 2,
            "message": "[CQ:reply,id=43][CQ:at,qq=111] 真正的 transport CQ 引用",
        }
        ev = _normalize_event(raw, "111")
        assert ev is not None
        assert ev.reply is not None
        assert ev.reply.raw_data["source"] == "onebot-cq-reply"

        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]
        task = asyncio.create_task(adapter._enrich_reply(ev))  # type: ignore[attr-defined]
        await asyncio.sleep(0)
        frame = json.loads(conn.frames[0])
        assert frame["action"] == "get_msg"
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 43,
                    "sender": {"user_id": 222},
                    "message": [{"type": "text", "data": {"text": "真实原文"}}],
                    "raw_message": "真实原文",
                },
            }
        )

        enriched = await task
        assert enriched.reply is not None
        assert trusted_reply_sender_id(enriched.reply) == "222"

    asyncio.run(go())


def test_adapter_enriches_reply_voice_from_recent_cache_when_preview_text_present() -> None:
    async def go() -> None:
        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "111")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        original = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 52,
                "user_id": 222,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 1,
                "message": [
                    {
                        "type": "record",
                        "data": {
                            "file": "voice.silk",
                            "url": "https://qq.example/record/voice.silk",
                            "duration": 5,
                        },
                    }
                ],
            },
            "111",
        )
        assert original is not None
        adapter._remember_event(original)  # type: ignore[attr-defined]

        reply_event = _normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 53,
                "user_id": 333,
                "group_id": 2000000001,
                "self_id": 111,
                "time": 2,
                "message": [
                    {"type": "reply", "data": {"id": "52", "text": "[语音]"}},
                    {"type": "at", "data": {"qq": "111"}},
                    {"type": "text", "data": {"text": " 这条语音说了啥"}},
                ],
            },
            "111",
        )
        assert reply_event is not None

        enriched = await adapter._enrich_reply(reply_event)  # type: ignore[attr-defined]

        assert enriched.reply is not None
        assert enriched.reply.message_id == "52"
        assert len(enriched.reply.resources) == 1
        assert enriched.reply.resources[0].kind == "voice"
        assert enriched.resources == enriched.reply.resources
        assert conn.frames == []

    asyncio.run(go())


def test_adapter_enriches_forward_segment_by_fetching_merged_content() -> None:
    async def go() -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 62,
            "user_id": 1000000004,
            "group_id": 2000000002,
            "self_id": 1000000001,
            "time": 1,
            "message": [
                {"type": "at", "data": {"qq": "1000000001"}},
                {"type": "text", "data": {"text": " 总结 "}},
                {"type": "forward", "data": {"id": "forward-msg-2", "summary": "2条转发消息"}},
            ],
        }
        ev = _normalize_event(raw, "1000000001")
        assert ev is not None
        assert ev.resources[0].raw_data["messages"] == []

        adapter = OneBotAdapter("127.0.0.1", 1, "/onebot", "", "1000000001")
        conn = FakeConn()
        adapter._conns.add(conn)  # type: ignore[arg-type]

        task = asyncio.create_task(adapter._enrich_forward(ev))  # type: ignore[attr-defined]
        await asyncio.sleep(0)

        frame = json.loads(conn.frames[0])
        assert frame["action"] == "get_forward_msg"
        assert frame["params"] == {"message_id": "forward-msg-2", "id": "forward-msg-2"}
        adapter._complete_action_response(  # type: ignore[attr-defined]
            {
                "echo": frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {
                    "messages": [
                        {
                            "sender": {"user_id": 222, "nickname": "Alice"},
                            "message": [
                                {"type": "text", "data": {"text": "第一条 https://example.com/a"}}
                            ],
                        },
                        {
                            "sender": {"user_id": 333, "nickname": "Bob"},
                            "message": [
                                {
                                    "type": "image",
                                    "data": {
                                        "file": "pic.jpg",
                                        "url": "https://qq.example/pic.jpg",
                                    },
                                }
                            ],
                        },
                    ]
                },
            }
        )

        enriched = await task
        forward = enriched.resources[0]
        assert forward.kind == "forward"
        assert forward.raw_data["messages"] == [
            {
                "sender_id": "222",
                "sender_name": "Alice",
                "text": "第一条 https://example.com/a",
                "resources": [
                    {
                        "kind": "url",
                        "name": "https://example.com/a",
                        "url": "https://example.com/a",
                    }
                ],
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

    asyncio.run(go())
