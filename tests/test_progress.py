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


def test_progress_heartbeat_reuses_latest_phase_and_stops_at_cap() -> None:
    async def go() -> None:
        sent: list[tuple[str, str]] = []
        clock = [100.0]

        async def sleep(seconds: float) -> None:
            clock[0] += seconds
            await asyncio.sleep(0)

        async def send(text: str, echo: str) -> None:
            sent.append((text, echo))

        cfg = BridgeConfig()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        cfg.progress.min_progress_interval_seconds = 0
        cfg.progress.max_heartbeat_messages = 2
        reporter = ProgressReporter(
            "j1",
            make_ev(),
            cfg.progress,
            send,
            now=lambda: clock[0],
            sleep=sleep,
        )

        await reporter.send_progress("正在转写视频音频。")
        await reporter.run_heartbeat(lambda: False)

        heartbeats = [(text, echo) for text, echo in sent if "已运行" in text]
        assert len(heartbeats) == 2
        assert all("正在转写视频音频" in text for text, _echo in heartbeats)
        assert len({echo for _text, echo in heartbeats}) == 2
        assert all(text != "还在处理，已经跑了一会儿。" for text, _echo in heartbeats)

    asyncio.run(go())


def test_rate_limited_progress_still_updates_heartbeat_phase() -> None:
    async def go() -> None:
        sent: list[str] = []
        clock = [100.0]

        async def sleep(seconds: float) -> None:
            clock[0] += seconds
            await asyncio.sleep(0)

        async def send(text: str, echo: str) -> None:
            sent.append(text)

        cfg = BridgeConfig()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        cfg.progress.min_progress_interval_seconds = 10
        cfg.progress.max_heartbeat_messages = 1
        reporter = ProgressReporter(
            "j1",
            make_ev(),
            cfg.progress,
            send,
            now=lambda: clock[0],
            sleep=sleep,
        )

        await reporter.send_progress("正在下载视频。")
        await reporter.send_progress("正在转写字幕。")
        await reporter.run_heartbeat(lambda: False)

        assert sent[0] == "正在下载视频。"
        assert "正在转写字幕" in sent[-1]

    asyncio.run(go())


def test_completed_progress_heartbeat_does_not_claim_finished_phase_is_still_running() -> None:
    async def go() -> None:
        sent: list[str] = []
        clock = [100.0]

        async def sleep(seconds: float) -> None:
            clock[0] += seconds
            await asyncio.sleep(0)

        async def send(text: str, echo: str) -> None:
            sent.append(text)

        cfg = BridgeConfig()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        cfg.progress.min_progress_interval_seconds = 0
        cfg.progress.max_heartbeat_messages = 1
        reporter = ProgressReporter(
            "j1",
            make_ev(),
            cfg.progress,
            send,
            now=lambda: clock[0],
            sleep=sleep,
        )

        await reporter.send_progress("视频字幕转写完成。")
        await reporter.run_heartbeat(lambda: False)

        assert "上一阶段已完成，正在继续处理后续内容" in sent[-1]

    asyncio.run(go())
