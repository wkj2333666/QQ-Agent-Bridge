"""Core shared types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ResourceKind = Literal["image", "file", "url", "audio", "voice", "video", "forward"]


@dataclass(frozen=True)
class ChatResource:
    """Untrusted resource reference attached to a chat message."""

    kind: ResourceKind
    url: str | None = None
    file_id: str | None = None
    name: str | None = None
    size: int | None = None
    mime_type: str | None = None
    duration_seconds: int | None = None
    source_segment: int = 0
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatSegment:
    """Normalized OneBot message segment."""

    type: str
    text: str = ""
    qq: str | None = None
    resource: ChatResource | None = None
    raw_type: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatReply:
    """Quoted/replied-to message context."""

    message_id: str = ""
    sender_id: str | None = None
    text: str = ""
    raw_message: str = ""
    segments: tuple[ChatSegment, ...] = ()
    resources: tuple[ChatResource, ...] = ()
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatEvent:
    """Normalized chat event from any platform."""
    id: str
    platform: str
    chat_id: str
    sender_id: str
    is_group: bool
    mentioned_bot: bool
    text: str
    timestamp: int
    segments: tuple[ChatSegment, ...] = ()
    resources: tuple[ChatResource, ...] = ()
    reply: ChatReply | None = None
    raw_message: str = ""


CommandName = Literal[
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
]


@dataclass(frozen=True)
class ParsedCommand:
    name: CommandName
    args: str
    raw: str
