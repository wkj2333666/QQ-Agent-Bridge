"""Short-lived group attachment cache tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.attachment_cache import AttachmentCache  # type: ignore
from qq_agent_bridge.types import ChatEvent, ChatResource  # type: ignore


def make_ev(
    sender: str = "reader",
    group: str = "group",
    mid: str = "m1",
    timestamp: int = 1,
    resources: tuple[ChatResource, ...] = (),
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=group,
        sender_id=sender,
        is_group=True,
        mentioned_bot=False,
        text="",
        timestamp=timestamp,
        resources=resources,
    )


def test_cache_returns_same_sender_group_resources_once() -> None:
    now = 1000.0
    cache = AttachmentCache(ttl_seconds=600, max_items=4, now=lambda: now)
    image = ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg")

    cache.remember(make_ev(resources=(image,)))

    assert cache.pop("group", "reader") == (image,)
    assert cache.pop("group", "reader") == ()


def test_cache_is_scoped_by_group_and_sender() -> None:
    cache = AttachmentCache(ttl_seconds=600, max_items=4, now=lambda: 1000.0)
    image = ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg")

    cache.remember(make_ev(resources=(image,)))

    assert cache.pop("group", "other") == ()
    assert cache.pop("other-group", "reader") == ()
    assert cache.pop("group", "reader") == (image,)


def test_cache_expires_old_resources() -> None:
    current = 1000.0

    def now() -> float:
        return current

    cache = AttachmentCache(ttl_seconds=10, max_items=4, now=now)
    image = ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg")
    cache.remember(make_ev(resources=(image,)))

    current = 1011.0

    assert cache.pop("group", "reader") == ()
