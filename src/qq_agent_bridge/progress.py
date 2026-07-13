"""Per-job progress reporting for QQ long tasks."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

from .config import ProgressConfig
from .redactor import redact
from .types import ChatEvent

logger = logging.getLogger(__name__)

ProgressSend = Callable[[str, str], Awaitable[None]]
DonePredicate = Callable[[], bool]
Sleep = Callable[[float], Awaitable[None]]

_LOW_INFORMATION_PROGRESS = {
    "正在处理任务的一步",
    "正在处理任务的一步。",
    "这一步处理完了",
    "这一步处理完了。",
}


class ProgressReporter:
    def __init__(
        self,
        job_id: str,
        event: ChatEvent,
        cfg: ProgressConfig,
        send: ProgressSend,
        now: Callable[[], float] | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.job_id = job_id
        self.event = event
        self.cfg = cfg
        self.send = send
        self.now = now or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self._count = 0
        self._heartbeat_count = 0
        self._consecutive_heartbeats = 0
        self._stopped = False
        self._started_at = self.now()
        self._last_sent_at = self._started_at
        self._latest_progress = ""

    async def send_progress(self, text: str) -> None:
        if self._stopped or not self.cfg.enabled:
            return
        cleaned = redact(text).strip()
        if not cleaned:
            return
        cleaned = cleaned[: max(0, self.cfg.max_progress_chars)]
        if not cleaned or cleaned in _LOW_INFORMATION_PROGRESS:
            return
        # Keep the newest real phase even when its immediate QQ message is rate-limited.
        self._latest_progress = cleaned
        self._consecutive_heartbeats = 0
        if self._count >= max(0, self.cfg.max_progress_messages):
            return
        current = self.now()
        if self._count and current - self._last_sent_at < self.cfg.min_progress_interval_seconds:
            return
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
        max_heartbeats = max(0, self.cfg.max_heartbeat_messages)
        if max_heartbeats == 0:
            return
        first = max(0, self.cfg.first_heartbeat_seconds)
        interval = max(1, self.cfg.heartbeat_seconds)
        await self.sleep(first)
        while (
            not self._stopped
            and not done()
            and self._heartbeat_count < max_heartbeats
        ):
            current = self.now()
            required_silence = interval * min(self._consecutive_heartbeats + 1, 4)
            if current - self._last_sent_at >= required_silence:
                heartbeat = self._heartbeat_text(current)
                try:
                    await self.send(
                        heartbeat,
                        f"{self.event.id}-heartbeat-{self._heartbeat_count}",
                    )
                except Exception:  # noqa: BLE001 - heartbeat must not fail the job
                    logger.exception("failed to send heartbeat for job %s", self.job_id)
                self._heartbeat_count += 1
                self._consecutive_heartbeats += 1
                self._last_sent_at = self.now()
            if self._heartbeat_count < max_heartbeats:
                await self.sleep(interval)

    def _heartbeat_text(self, current: float) -> str:
        elapsed = self._format_elapsed(max(0, current - self._started_at))
        phase = self._ongoing_phase(self._latest_progress)
        if phase:
            return f"还在处理：{phase}（已运行{elapsed}）。"
        subject = self._task_subject()
        return f"还在处理“{subject}”（已运行{elapsed}），暂时没有新的阶段结果。"

    def _task_subject(self) -> str:
        text = redact(self.event.text).strip()
        text = re.sub(r"^\s*/(?:task|code)\b\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"https?://\S+", "", text)
        text = " ".join(text.split()).strip(" ，。:：")
        if not text:
            return "当前任务"
        return text if len(text) <= 42 else text[:39].rstrip() + "..."

    def _ongoing_phase(self, text: str) -> str:
        phase = " ".join(text.split()).strip().rstrip("。！？!?")
        if not phase:
            return ""
        if "正在" not in phase and (
            phase.endswith("完成")
            or any(marker in phase for marker in ("已完成", "完成了", "处理完", "已看完"))
        ):
            return "上一阶段已完成，正在继续处理后续内容"
        return phase

    def _format_elapsed(self, seconds: float) -> str:
        rounded = max(1, int(seconds))
        if rounded < 60:
            return f"{rounded}秒"
        minutes = rounded // 60
        if minutes < 60:
            return f"{minutes}分钟"
        hours, remaining = divmod(minutes, 60)
        return f"{hours}小时{remaining}分钟" if remaining else f"{hours}小时"

    def stop(self) -> None:
        self._stopped = True
