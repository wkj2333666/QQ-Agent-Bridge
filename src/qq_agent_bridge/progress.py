"""Per-job progress reporting for QQ long tasks."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .config import ProgressConfig
from .redactor import redact
from .types import ChatEvent

logger = logging.getLogger(__name__)

ProgressSend = Callable[[str, str], Awaitable[None]]
DonePredicate = Callable[[], bool]


class ProgressReporter:
    def __init__(
        self,
        job_id: str,
        event: ChatEvent,
        cfg: ProgressConfig,
        send: ProgressSend,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.job_id = job_id
        self.event = event
        self.cfg = cfg
        self.send = send
        self.now = now or time.monotonic
        self._count = 0
        self._stopped = False
        self._last_sent_at = self.now()

    async def send_progress(self, text: str) -> None:
        if self._stopped or not self.cfg.enabled:
            return
        if self._count >= max(0, self.cfg.max_progress_messages):
            return
        current = self.now()
        if self._count and current - self._last_sent_at < self.cfg.min_progress_interval_seconds:
            return
        cleaned = redact(text).strip()
        if not cleaned:
            return
        cleaned = cleaned[: max(0, self.cfg.max_progress_chars)]
        try:
            await self.send(cleaned, f"{self.event.id}-progress-{self._count}")
        except Exception:  # noqa: BLE001 - progress must not fail the job
            logger.exception("failed to send progress for job %s", self.job_id)
            return
        self._count += 1
        self._last_sent_at = current

    async def run_heartbeat(self, done: DonePredicate) -> None:
        if not self.cfg.enabled:
            return
        first = max(0, self.cfg.first_heartbeat_seconds)
        interval = max(1, self.cfg.heartbeat_seconds)
        await asyncio.sleep(first)
        while not self._stopped and not done():
            current = self.now()
            if current - self._last_sent_at >= interval:
                try:
                    await self.send("还在处理，已经跑了一会儿。", f"{self.event.id}-heartbeat")
                except Exception:  # noqa: BLE001 - heartbeat must not fail the job
                    logger.exception("failed to send heartbeat for job %s", self.job_id)
                self._last_sent_at = self.now()
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._stopped = True
