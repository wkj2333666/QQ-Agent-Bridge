"""Scheduler service tests with a fake clock and executor."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import SchedulerConfig  # type: ignore
from qq_agent_bridge.schedule_store import ScheduleStore  # type: ignore
from qq_agent_bridge.scheduler import (  # type: ignore
    Schedule,
    ScheduleExecutionResult,
    ScheduleSpec,
    Scheduler,
    next_due_after,
)
from qq_agent_bridge.types import ChatEvent  # type: ignore


def make_event(mid: str = "m1") -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id="group",
        sender_id="owner",
        is_group=True,
        mentioned_bot=True,
        text="/schedule",
        timestamp=100,
    )


def make_cfg() -> SchedulerConfig:
    return SchedulerConfig(
        enabled=True,
        timezone="Asia/Shanghai",
        min_interval_seconds=60,
        max_schedules_per_chat=20,
        max_occurrences=100,
        misfire_grace_seconds=300,
        max_consecutive_failures=2,
    )


def test_once_schedule_executes_once_and_completes(tmp_path: Path) -> None:
    async def go() -> None:
        calls: list[str] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            calls.append(schedule.payload)
            return ScheduleExecutionResult("succeeded")

        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: True)
        scheduler.initialize()
        spec = ScheduleSpec(
            kind="once",
            action="send",
            payload="喝水",
            timezone="Asia/Shanghai",
            start_at=110,
        )
        schedule = scheduler.create(spec, make_event(), now=100)

        await scheduler.tick(now=109)
        assert calls == []
        await scheduler.tick(now=110)
        await scheduler.wait_for_runs()

        assert calls == ["喝水"]
        assert store.get(schedule.id).status == "completed"  # type: ignore[union-attr]

    asyncio.run(go())


def test_counted_interval_runs_exact_number_of_times(tmp_path: Path) -> None:
    async def go() -> None:
        calls: list[int] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            calls.append(run.due_at)
            return ScheduleExecutionResult("succeeded")

        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: True)
        scheduler.initialize()
        spec = ScheduleSpec(
            kind="rrule",
            action="task",
            payload="检查",
            timezone="Asia/Shanghai",
            start_at=100,
            rrule="FREQ=MINUTELY;COUNT=3",
        )
        schedule = scheduler.create(spec, make_event(), now=90)

        for timestamp in (100, 160, 220, 280):
            await scheduler.tick(now=timestamp)
            await scheduler.wait_for_runs()

        assert calls == [100, 160, 220]
        assert store.get(schedule.id).status == "completed"  # type: ignore[union-attr]

    asyncio.run(go())


def test_daily_schedule_keeps_local_wall_clock_without_end(tmp_path: Path) -> None:
    async def go() -> None:
        calls: list[int] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            calls.append(run.due_at)
            return ScheduleExecutionResult("succeeded")

        first = int(datetime(2026, 7, 14, 0, 0, tzinfo=UTC).timestamp())
        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: True)
        scheduler.initialize()
        spec = ScheduleSpec(
            kind="rrule",
            action="task",
            payload="天气",
            timezone="Asia/Shanghai",
            start_at=first,
            rrule="FREQ=DAILY",
        )
        schedule = scheduler.create(spec, make_event(), now=first - 60)

        await scheduler.tick(now=first)
        await scheduler.wait_for_runs()

        saved = store.get(schedule.id)
        assert calls == [first]
        assert saved is not None
        assert saved.status == "active"
        assert saved.next_run_at == first + 86400

    asyncio.run(go())


def test_weekly_schedule_advances_to_same_local_weekday(tmp_path: Path) -> None:
    async def go() -> None:
        calls: list[int] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            calls.append(run.due_at)
            return ScheduleExecutionResult("succeeded")

        first = int(datetime(2026, 7, 14, 0, 0, tzinfo=UTC).timestamp())
        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: True)
        scheduler.initialize()
        schedule = scheduler.create(
            ScheduleSpec(
                kind="rrule",
                action="send",
                payload="喝水",
                timezone="Asia/Shanghai",
                start_at=first,
                rrule="FREQ=WEEKLY;BYDAY=TU",
            ),
            make_event(),
            now=first - 60,
        )

        await scheduler.tick(now=first)
        await scheduler.wait_for_runs()

        saved = store.get(schedule.id)
        assert calls == [first]
        assert saved is not None
        assert saved.next_run_at == first + 7 * 86400

    asyncio.run(go())


def test_scheduler_does_not_execute_when_gateway_is_unavailable(tmp_path: Path) -> None:
    async def go() -> None:
        calls: list[str] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            calls.append(schedule.id)
            return ScheduleExecutionResult("succeeded")

        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: False)
        scheduler.initialize()
        scheduler.create(
            ScheduleSpec(
                kind="once",
                action="send",
                payload="test",
                timezone="Asia/Shanghai",
                start_at=100,
            ),
            make_event(),
            now=90,
        )

        await scheduler.tick(now=100)

        assert calls == []
        assert store.list_due(100)

    asyncio.run(go())


def test_consecutive_failures_pause_unbounded_schedule(tmp_path: Path) -> None:
    async def go() -> None:
        async def execute(schedule, run) -> ScheduleExecutionResult:
            return ScheduleExecutionResult("failed", "boom")

        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: True)
        scheduler.initialize()
        schedule = scheduler.create(
            ScheduleSpec(
                kind="rrule",
                action="task",
                payload="test",
                timezone="Asia/Shanghai",
                start_at=100,
                rrule="FREQ=MINUTELY",
            ),
            make_event(),
            now=90,
        )

        for timestamp in (100, 160):
            await scheduler.tick(now=timestamp)
            await scheduler.wait_for_runs()

        saved = store.get(schedule.id)
        assert saved is not None
        assert saved.status == "paused"
        assert saved.consecutive_failures == 2

    asyncio.run(go())


def test_misfire_count_excludes_occurrence_replayed_within_grace(tmp_path: Path) -> None:
    async def go() -> None:
        calls: list[int] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            calls.append(run.due_at)
            return ScheduleExecutionResult("succeeded")

        cfg = make_cfg()
        cfg.misfire_grace_seconds = 30
        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(cfg, store, execute, ready=lambda: True)
        scheduler.initialize()
        schedule = scheduler.create(
            ScheduleSpec(
                kind="rrule",
                action="send",
                payload="test",
                timezone="Asia/Shanghai",
                start_at=100,
                rrule="FREQ=MINUTELY",
            ),
            make_event(),
            now=90,
        )

        await scheduler.tick(now=235)
        await scheduler.wait_for_runs()

        saved = store.get(schedule.id)
        assert calls == [220]
        assert saved is not None
        assert saved.missed_count == 2
        assert saved.next_run_at == 280

    asyncio.run(go())


def test_scheduler_limits_concurrent_due_runs(tmp_path: Path) -> None:
    async def go() -> None:
        release = asyncio.Event()
        started: list[str] = []

        async def execute(schedule, run) -> ScheduleExecutionResult:
            started.append(schedule.id)
            await release.wait()
            return ScheduleExecutionResult("succeeded")

        cfg = make_cfg()
        cfg.max_concurrent_runs = 2
        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(cfg, store, execute, ready=lambda: True)
        scheduler.initialize()
        for index in range(3):
            scheduler.create(
                ScheduleSpec(
                    kind="once",
                    action="send",
                    payload=str(index),
                    timezone="Asia/Shanghai",
                    start_at=100,
                ),
                make_event(mid=f"m{index}"),
                now=90,
            )

        assert await scheduler.tick(now=100) == 2
        await asyncio.sleep(0)
        assert len(started) == 2
        assert await scheduler.tick(now=100) == 0
        release.set()
        await scheduler.wait_for_runs()
        assert await scheduler.tick(now=100) == 1
        await scheduler.wait_for_runs()
        assert len(started) == 3

    asyncio.run(go())


def test_long_lived_minutely_rule_advances_incrementally() -> None:
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    current = int(datetime(2026, 7, 13, tzinfo=UTC).timestamp())
    schedule = Schedule.from_spec(
        ScheduleSpec(
            kind="rrule",
            action="send",
            payload="test",
            timezone="Asia/Shanghai",
            start_at=start,
            rrule="FREQ=MINUTELY",
        ),
        schedule_id="fast",
        chat_id="group",
        is_group=True,
        creator_id="owner",
        source_message_id="fast-message",
        created_at=start,
    )

    before = time.perf_counter()
    result = next_due_after(schedule, current)
    elapsed = time.perf_counter() - before

    assert result == current + 60
    assert elapsed < 0.5


def test_incremental_rrule_keeps_month_end_workday_semantics() -> None:
    first = int(datetime(2026, 7, 31, 10, 0, tzinfo=UTC).timestamp())
    schedule = Schedule.from_spec(
        ScheduleSpec(
            kind="rrule",
            action="task",
            payload="整理本月工作",
            timezone="Asia/Shanghai",
            start_at=first,
            rrule="FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
        ),
        schedule_id="monthly",
        chat_id="group",
        is_group=True,
        creator_id="owner",
        source_message_id="monthly-message",
        created_at=first,
    )

    assert next_due_after(schedule, first) == int(
        datetime(2026, 8, 31, 10, 0, tzinfo=UTC).timestamp()
    )


def test_fast_forward_uses_original_interval_phase() -> None:
    first = int(datetime(2026, 7, 14, 0, 0, tzinfo=UTC).timestamp())
    schedule = Schedule.from_spec(
        ScheduleSpec(
            kind="rrule",
            action="send",
            payload="test",
            timezone="Asia/Shanghai",
            start_at=first,
            rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
        ),
        schedule_id="biweekly",
        chat_id="group",
        is_group=True,
        creator_id="owner",
        source_message_id="biweekly-message",
        created_at=first,
    )
    after = int(datetime(2026, 7, 22, 0, 0, tzinfo=UTC).timestamp())

    assert next_due_after(schedule, after, anchor_is_occurrence=False) == int(
        datetime(2026, 7, 28, 0, 0, tzinfo=UTC).timestamp()
    )


def test_new_schedule_wakes_idle_dispatch_loop(tmp_path: Path) -> None:
    async def go() -> None:
        fired = asyncio.Event()

        async def execute(schedule, run) -> ScheduleExecutionResult:
            fired.set()
            return ScheduleExecutionResult("succeeded")

        store = ScheduleStore(tmp_path / "schedules.sqlite3")
        scheduler = Scheduler(make_cfg(), store, execute, ready=lambda: True, now=lambda: 100)
        await scheduler.start()
        scheduler.create(
            ScheduleSpec(
                kind="once",
                action="send",
                payload="wake",
                timezone="Asia/Shanghai",
                start_at=100,
            ),
            make_event(mid="wake-message"),
            now=90,
        )
        try:
            await asyncio.wait_for(fired.wait(), timeout=0.5)
        finally:
            await scheduler.stop()

    asyncio.run(go())
