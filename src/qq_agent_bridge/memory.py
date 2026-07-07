"""In-memory rolling conversation windows."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .types import ChatEvent


@dataclass(frozen=True)
class MemoryMessage:
    speaker: str
    text: str
    message_id: str = ""


class ConversationMemory:
    def __init__(self, max_messages: int = 12, max_chars: int = 6000) -> None:
        self.max_messages = max(0, max_messages)
        self.max_chars = max(0, max_chars)
        self._messages: dict[str, list[MemoryMessage]] = {}

    def key_for(self, ev: ChatEvent) -> str:
        if ev.is_group:
            return f"group:{ev.chat_id}"
        return f"private:{ev.sender_id}"

    def append_exchange(self, ev: ChatEvent, user_text: str, assistant_text: str) -> None:
        user_label = ev.sender_id if ev.is_group else "用户"
        self._append(ev, user_label, user_text, ev.id)
        self._append(ev, "助手", assistant_text, f"{ev.id}:assistant" if ev.id else "")

    def append_user_message(self, ev: ChatEvent, text: str | None = None) -> None:
        user_label = ev.sender_id if ev.is_group else "用户"
        self._append(ev, user_label, ev.text if text is None else text, ev.id)

    def append_assistant_message(self, ev: ChatEvent, text: str, message_id: str = "") -> None:
        self._append(ev, "助手", text, message_id or (f"{ev.id}:assistant" if ev.id else ""))

    def _append(self, ev: ChatEvent, speaker: str, text: str, message_id: str = "") -> None:
        clean = text.strip()
        if not clean:
            return
        key = self.key_for(ev)
        messages = self._messages.setdefault(key, [])
        if message_id and any(
            msg.message_id == message_id and msg.speaker == speaker and msg.text == clean for msg in messages
        ):
            return
        messages.append(MemoryMessage(speaker, clean, message_id))
        self._trim(key)

    def format_history(self, ev: ChatEvent) -> str:
        key = self.key_for(ev)
        lines = [f"{msg.speaker}: {msg.text}" for msg in self._messages.get(key, []) if msg.text]
        text = "\n".join(lines)
        if self.max_chars and len(text) > self.max_chars:
            return text[-self.max_chars :]
        return text

    def reset(self, ev: ChatEvent) -> None:
        self._messages.pop(self.key_for(ev), None)

    def _trim(self, key: str) -> None:
        messages = self._messages.get(key)
        if messages is None:
            return
        if self.max_messages == 0:
            messages.clear()
            return
        overflow = len(messages) - self.max_messages
        if overflow > 0:
            del messages[:overflow]
        while self.max_chars and len(self.format_history_for_key(key)) > self.max_chars and messages:
            del messages[0]

    def format_history_for_key(self, key: str) -> str:
        lines = [f"{msg.speaker}: {msg.text}" for msg in self._messages.get(key, []) if msg.text]
        text = "\n".join(lines)
        if self.max_chars and len(text) > self.max_chars:
            return text[-self.max_chars :]
        return text


@dataclass(frozen=True)
class AmbientMessage:
    id: str
    sender_id: str
    text: str
    timestamp: int


class GroupAmbientMemory:
    """Low-priority rolling window of ordinary group chatter."""

    def __init__(
        self,
        max_messages: int = 8,
        max_chars: int = 1200,
        max_message_chars: int = 180,
        max_age_seconds: int = 900,
        min_chars: int = 4,
        ignored_prefixes: tuple[str, ...] = ("/", "／", "!", "！"),
    ) -> None:
        self.max_messages = max(0, max_messages)
        self.max_chars = max(0, max_chars)
        self.max_message_chars = max(1, max_message_chars)
        self.max_age_seconds = max(0, max_age_seconds)
        self.min_chars = max(0, min_chars)
        self.ignored_prefixes = ignored_prefixes
        self._messages: dict[str, list[AmbientMessage]] = {}

    def configure(
        self,
        *,
        max_messages: int,
        max_chars: int,
        max_message_chars: int,
        max_age_seconds: int,
        min_chars: int,
        ignored_prefixes: tuple[str, ...],
    ) -> None:
        self.max_messages = max(0, max_messages)
        self.max_chars = max(0, max_chars)
        self.max_message_chars = max(1, max_message_chars)
        self.max_age_seconds = max(0, max_age_seconds)
        self.min_chars = max(0, min_chars)
        self.ignored_prefixes = ignored_prefixes
        for key in list(self._messages):
            self._trim(key)

    def key_for(self, ev: ChatEvent) -> str:
        return f"group:{ev.chat_id}"

    def remember(self, ev: ChatEvent) -> bool:
        if not ev.is_group or ev.mentioned_bot:
            return False
        text = self._normalize_text(ev.text)
        if not self._is_allowed_text(text):
            return False
        key = self.key_for(ev)
        messages = self._messages.setdefault(key, [])
        if any(msg.id == ev.id for msg in messages):
            return False
        messages.append(AmbientMessage(ev.id, ev.sender_id, text, ev.timestamp))
        self._trim(key, now=ev.timestamp)
        return True

    def format_context(self, ev: ChatEvent, now: int | None = None) -> str:
        key = self.key_for(ev)
        self._drop_expired(key, ev.timestamp if now is None else now)
        lines = [f"{msg.sender_id}: {msg.text}" for msg in self._messages.get(key, []) if msg.text]
        text = "\n".join(lines)
        if self.max_chars and len(text) > self.max_chars:
            kept: list[str] = []
            total = 0
            for line in reversed(lines):
                line_len = len(line) + (1 if kept else 0)
                if total + line_len > self.max_chars:
                    break
                kept.append(line)
                total += line_len
            text = "\n".join(reversed(kept))
        return text

    def reset(self, ev: ChatEvent) -> None:
        self._messages.pop(self.key_for(ev), None)

    def _normalize_text(self, text: str) -> str:
        cleaned = " ".join(text.strip().split())
        if len(cleaned) > self.max_message_chars:
            return cleaned[: self.max_message_chars].rstrip()
        return cleaned

    def _is_allowed_text(self, text: str) -> bool:
        if len(text) < self.min_chars:
            return False
        lowered = text.lower()
        if any(lowered.startswith(prefix.lower()) for prefix in self.ignored_prefixes):
            return False
        if _looks_command_like(text):
            return False
        return True

    def _trim(self, key: str, now: int | None = None) -> None:
        messages = self._messages.get(key)
        if messages is None:
            return
        if self.max_messages == 0:
            messages.clear()
            return
        overflow = len(messages) - self.max_messages
        if overflow > 0:
            del messages[:overflow]
        self._drop_expired(key, now)
        messages = self._messages.get(key)
        if messages is None:
            return
        while self.max_chars and len(self.format_context_for_key(key)) > self.max_chars and messages:
            del messages[0]

    def _drop_expired(self, key: str, now: int | None = None) -> None:
        if not self.max_age_seconds:
            return
        messages = self._messages.get(key)
        if not messages:
            return
        current = int(now if now is not None else time.time())
        self._messages[key] = [
            msg for msg in messages if current - msg.timestamp <= self.max_age_seconds
        ]

    def format_context_for_key(self, key: str) -> str:
        lines = [f"{msg.sender_id}: {msg.text}" for msg in self._messages.get(key, []) if msg.text]
        return "\n".join(lines)


def should_include_ambient_for_task(text: str) -> bool:
    normalized = " ".join(text.strip().split()).lower()
    if not normalized:
        return False
    negative_markers = (
        "不要根据聊天记录",
        "不用根据聊天记录",
        "不需要根据聊天记录",
        "不要参考聊天",
        "不用参考聊天",
        "不要参考群聊",
        "不用参考群聊",
    )
    if any(marker in normalized for marker in negative_markers):
        return False
    markers = (
        "刚才",
        "刚刚",
        "上面",
        "前面",
        "上文",
        "前文",
        "群里",
        "他们说",
        "大家说",
        "大家刚说",
        "聊天记录",
        "这件事",
        "这个话题",
        "上面的讨论",
    )
    return any(marker in normalized for marker in markers)


def _looks_command_like(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith(("/", "／")):
        return True
    if stripped.startswith("@") and any(prefix in stripped[:60] for prefix in ("/", "／")):
        return True
    return False
