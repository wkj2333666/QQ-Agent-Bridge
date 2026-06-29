"""Short-lived cache for group attachments sent before bot mention."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from .types import ChatEvent, ChatResource


@dataclass(frozen=True)
class _Entry:
    resources: tuple[ChatResource, ...]
    created: float


class AttachmentCache:
    def __init__(
        self,
        ttl_seconds: int = 600,
        max_items: int = 4,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self.max_items = max(0, max_items)
        self.now = now or time.time
        self._entries: dict[tuple[str, str], _Entry] = {}

    def remember(self, ev: ChatEvent) -> None:
        if not ev.is_group or ev.mentioned_bot or not ev.resources or self.max_items <= 0:
            return
        resources = ev.resources[: self.max_items]
        if not resources:
            return
        self._prune()
        self._entries[(ev.chat_id, ev.sender_id)] = _Entry(resources=resources, created=self.now())

    def pop(self, chat_id: str, sender_id: str) -> tuple[ChatResource, ...]:
        key = (chat_id, sender_id)
        entry = self._entries.pop(key, None)
        if not entry:
            return ()
        if self._expired(entry):
            return ()
        return entry.resources

    def _prune(self) -> None:
        expired = [key for key, entry in self._entries.items() if self._expired(entry)]
        for key in expired:
            self._entries.pop(key, None)

    def _expired(self, entry: _Entry) -> bool:
        return self.now() - entry.created > self.ttl_seconds
