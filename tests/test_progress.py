"""Long task progress reporter tests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.progress import ProgressReporter  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


def make_ev() -> ChatEvent:
    return ChatEvent(
        id="m1",
        platform="qq",
        chat_id="group",
        sender_id="reader",
        is_group=True,
        mentioned_bot=True,
        text="/task long",
        timestamp=1,
    )


def test_progress_reporter_rate_limits_and_caps_messages() -> None:
    async def go() -> None:
        sent: list[str] = []

        async def send(text: str, echo: str) -> None:
            sent.append(text)

        cfg = BridgeConfig()
        cfg.progress.min_progress_interval_seconds = 10
        cfg.progress.max_progress_messages = 2
        cfg.progress.max_progress_chars = 6
        now = [100.0]
        reporter = ProgressReporter("j1", make_ev(), cfg.progress, send, now=lambda: now[0])

        await reporter.send_progress("第一条很长很长")
        await reporter.send_progress("太快")
        now[0] += 11
        await reporter.send_progress("第二条")
        now[0] += 11
        await reporter.send_progress("第三条")

        assert sent == ["第一条很长很", "第二条"]

    asyncio.run(go())


def test_progress_reporter_sends_heartbeat_after_silence() -> None:
    async def go() -> None:
        sent: list[str] = []

        async def send(text: str, echo: str) -> None:
            sent.append(text)

        cfg = BridgeConfig()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        reporter = ProgressReporter("j1", make_ev(), cfg.progress, send)
        task = asyncio.create_task(reporter.run_heartbeat(lambda: False))
        await asyncio.sleep(1.2)
        reporter.stop()
        await task

        assert any("还在处理" in item for item in sent)

    asyncio.run(go())
