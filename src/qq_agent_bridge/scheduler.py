"""Persistent schedule domain model and RFC 5545 recurrence dispatcher."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
import secrets
import time
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from .config import SchedulerConfig
from .types import ChatEvent

logger = logging.getLogger(__name__)

ScheduleKind = Literal["once", "rrule"]
ScheduleAction = Literal["send", "ask", "task"]
RunState = Literal["succeeded", "failed", "cancelled", "interrupted", "missed"]

_RRULE_KEYS = {
    "FREQ",
    "UNTIL",
    "COUNT",
    "INTERVAL",
    "BYSECOND",
    "BYMINUTE",
    "BYHOUR",
    "BYDAY",
    "BYMONTHDAY",
    "BYYEARDAY",
    "BYWEEKNO",
    "BYMONTH",
    "BYSETPOS",
    "WKST",
}
_FREQUENCIES = {"SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}


@dataclass(frozen=True)
class ScheduleSpec:
    kind: ScheduleKind
    action: ScheduleAction
    payload: str
    timezone: str
    start_at: int
    rrule: str | None = None
    description: str = ""
    mentions: tuple[str, ...] = ()


@dataclass
class Schedule:
    id: str
    chat_id: str
    is_group: bool
    creator_id: str
    source_message_id: str
    kind: str
    action: str
    payload: str
    mentions: tuple[str, ...]
    timezone: str
    start_at: int
    next_run_at: int | None
    rrule: str | None = None
    description: str = ""
    status: str = "active"
    run_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    missed_count: int = 0
    consecutive_failures: int = 0
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    last_run_at: int | None = None
    last_error: str = ""

    @classmethod
    def from_spec(
        cls,
        spec: ScheduleSpec,
        *,
        schedule_id: str,
        chat_id: str,
        is_group: bool,
        creator_id: str,
        source_message_id: str,
        created_at: int,
    ) -> Schedule:
        return cls(
            id=schedule_id,
            chat_id=chat_id,
            is_group=is_group,
            creator_id=creator_id,
            source_message_id=source_message_id,
            kind=spec.kind,
            action=spec.action,
            payload=spec.payload,
            mentions=spec.mentions,
            timezone=spec.timezone,
            start_at=spec.start_at,
            next_run_at=first_due_for_spec(spec),
            rrule=spec.rrule,
            description=spec.description,
            created_at=created_at,
            updated_at=created_at,
        )


@dataclass(frozen=True)
class ScheduleRun:
    id: int
    schedule_id: str
    due_at: int
    started_at: int
    finished_at: int | None = None
    state: str = "running"
    job_id: str | None = None
    error: str = ""
    manual: bool = False


@dataclass(frozen=True)
class ScheduleExecutionResult:
    state: RunState
    error: str = ""
    job_id: str | None = None


class ScheduleStoreProtocol(Protocol):
    def initialize(self) -> None: ...
    def recover_interrupted(self, now: int) -> int: ...
    def create(self, schedule: Schedule) -> None: ...
    def get(self, schedule_id: str) -> Schedule | None: ...
    def get_by_source_message(
        self,
        chat_id: str,
        is_group: bool,
        message_id: str,
    ) -> Schedule | None: ...
    def list_for_chat(
        self,
        chat_id: str,
        is_group: bool,
        *,
        creator_id: str | None = None,
        active_only: bool = True,
    ) -> list[Schedule]: ...
    def active_count(self, chat_id: str, is_group: bool) -> int: ...
    def list_due(self, now: int, limit: int = 20) -> list[Schedule]: ...
    def next_due_at(self) -> int | None: ...
    def claim(
        self,
        schedule_id: str,
        *,
        expected_due_at: int,
        claimed_at: int,
        next_run_at: int | None,
    ) -> ScheduleRun | None: ...
    def claim_manual(self, schedule_id: str, *, claimed_at: int) -> ScheduleRun | None: ...
    def finish_run(
        self,
        run_id: int,
        state: str,
        *,
        finished_at: int,
        error: str = "",
        max_consecutive_failures: int = 0,
        max_run_history: int = 0,
        job_id: str | None = None,
    ) -> None: ...
    def advance_missed(
        self,
        schedule_id: str,
        *,
        expected_due_at: int,
        next_run_at: int | None,
        missed: int,
        now: int,
    ) -> bool: ...
    def set_status(
        self,
        schedule_id: str,
        status: str,
        *,
        now: int,
        from_statuses: tuple[str, ...] | None = None,
    ) -> bool: ...


ScheduleExecutor = Callable[[Schedule, ScheduleRun], Awaitable[ScheduleExecutionResult]]


class Scheduler:
    """Wake on the next durable due time and dispatch through an App callback."""

    def __init__(
        self,
        cfg: SchedulerConfig,
        store: ScheduleStoreProtocol,
        executor: ScheduleExecutor,
        *,
        ready: Callable[[], bool] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.executor = executor
        self.ready = ready or (lambda: True)
        self.now = now or time.time
        self._loop_task: asyncio.Task[None] | None = None
        self._run_tasks: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()

    def initialize(self) -> None:
        self.store.initialize()
        recovered = self.store.recover_interrupted(int(self.now()))
        if recovered:
            logger.warning("recovered %s interrupted scheduled runs", recovered)

    async def start(self) -> None:
        if not self.cfg.enabled or self._loop_task:
            return
        self.initialize()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task in list(self._run_tasks):
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks, return_exceptions=True)
        self._run_tasks.clear()

    def reload_config(self, cfg: SchedulerConfig) -> None:
        self.cfg = cfg
        self._wake.set()

    def create(self, spec: ScheduleSpec, ev: ChatEvent, *, now: int | None = None) -> Schedule:
        created_at = int(self.now()) if now is None else int(now)
        existing = self.store.get_by_source_message(ev.chat_id, ev.is_group, ev.id)
        if existing:
            return existing
        if self.store.active_count(ev.chat_id, ev.is_group) >= max(1, self.cfg.max_schedules_per_chat):
            raise ValueError("当前会话的定时任务数量已达到上限")
        schedule = Schedule.from_spec(
            spec,
            schedule_id=f"s{created_at * 1000}-{secrets.token_hex(3)}",
            chat_id=ev.chat_id,
            is_group=ev.is_group,
            creator_id=ev.sender_id,
            source_message_id=ev.id,
            created_at=created_at,
        )
        self.store.create(schedule)
        self._wake.set()
        return schedule

    async def tick(self, *, now: int | None = None) -> int:
        current = int(self.now()) if now is None else int(now)
        if not self.cfg.enabled or not self.ready():
            return 0
        dispatched = 0
        capacity = max(0, max(1, self.cfg.max_concurrent_runs) - len(self._run_tasks))
        if capacity == 0:
            return 0
        for schedule in self.store.list_due(current, limit=capacity):
            schedule = self._normalize_misfire(schedule, current)
            if schedule.next_run_at is None or schedule.next_run_at > current:
                continue
            due_at = schedule.next_run_at
            next_run_at = next_due_after(schedule, due_at)
            run = self.store.claim(
                schedule.id,
                expected_due_at=due_at,
                claimed_at=current,
                next_run_at=next_run_at,
            )
            if not run:
                continue
            task = asyncio.create_task(self._execute(schedule, run))
            self._run_tasks.add(task)
            task.add_done_callback(self._run_tasks.discard)
            dispatched += 1
        return dispatched

    def pause(self, schedule_id: str, *, now: int | None = None) -> bool:
        changed = self.store.set_status(
            schedule_id,
            "paused",
            now=int(self.now()) if now is None else int(now),
            from_statuses=("active",),
        )
        if changed:
            self._wake.set()
        return changed

    def resume(self, schedule_id: str, *, now: int | None = None) -> bool:
        changed = self.store.set_status(
            schedule_id,
            "active",
            now=int(self.now()) if now is None else int(now),
            from_statuses=("paused",),
        )
        if changed:
            self._wake.set()
        return changed

    def cancel(self, schedule_id: str, *, now: int | None = None) -> bool:
        changed = self.store.set_status(
            schedule_id,
            "cancelled",
            now=int(self.now()) if now is None else int(now),
            from_statuses=("active", "paused", "finishing"),
        )
        if changed:
            self._wake.set()
        return changed

    def run_now(self, schedule_id: str, *, now: int | None = None) -> ScheduleRun | None:
        if len(self._run_tasks) >= max(1, self.cfg.max_concurrent_runs):
            return None
        claimed_at = int(self.now()) if now is None else int(now)
        schedule = self.store.get(schedule_id)
        if schedule is None or schedule.status not in {"active", "paused"}:
            return None
        run = self.store.claim_manual(schedule_id, claimed_at=claimed_at)
        if run is None:
            return None
        task = asyncio.create_task(self._execute(schedule, run))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return run

    async def wait_for_runs(self) -> None:
        while self._run_tasks:
            await asyncio.gather(*list(self._run_tasks), return_exceptions=True)

    async def _execute(self, schedule: Schedule, run: ScheduleRun) -> None:
        try:
            result = await self.executor(schedule, run)
        except asyncio.CancelledError:
            self.store.finish_run(
                run.id,
                "interrupted",
                finished_at=int(self.now()),
                error="scheduler stopped",
                max_consecutive_failures=max(0, self.cfg.max_consecutive_failures),
                max_run_history=max(1, self.cfg.max_run_history_per_schedule),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduled run %s failed", run.id)
            result = ScheduleExecutionResult("failed", type(exc).__name__)
        self.store.finish_run(
            run.id,
            result.state,
            finished_at=int(self.now()),
            error=result.error,
            max_consecutive_failures=max(0, self.cfg.max_consecutive_failures),
            max_run_history=max(1, self.cfg.max_run_history_per_schedule),
            job_id=result.job_id,
        )
        self._wake.set()

    def _normalize_misfire(self, schedule: Schedule, now: int) -> Schedule:
        due = schedule.next_run_at
        grace = max(0, self.cfg.misfire_grace_seconds)
        if due is None or now - due <= grace:
            return schedule
        if schedule.kind == "once":
            target = None
            missed = 1
        else:
            target, missed = _recurring_misfire_plan(
                schedule,
                now,
                grace=grace,
                count_limit=max(1, self.cfg.max_occurrences),
            )
        self.store.advance_missed(
            schedule.id,
            expected_due_at=due,
            next_run_at=target,
            missed=max(1, missed),
            now=now,
        )
        return self.store.get(schedule.id) or schedule

    async def _run_loop(self) -> None:
        while True:
            try:
                self._wake.clear()
                await self.tick()
                next_due = self.store.next_due_at()
                now = int(self.now())
                if not self.ready():
                    delay = 2.0
                elif next_due is None:
                    delay = 3600.0
                else:
                    delay = max(0.2, min(3600.0, float(next_due - now)))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("scheduler loop failed")
                await asyncio.sleep(1)


def validate_recurrence_rule(
    rule_text: str,
    *,
    start_at: int,
    timezone: str,
    cfg: SchedulerConfig,
) -> str:
    """Normalize and validate one bounded-complexity RRULE from an untrusted source."""
    raw = rule_text.strip().upper()
    if raw.startswith("RRULE:"):
        raw = raw[len("RRULE:") :]
    if not raw or "\n" in raw or "\r" in raw or ":" in raw:
        raise ValueError("只允许一条 RRULE")
    if len(raw) > 500:
        raise ValueError("RRULE 过长")
    parts: dict[str, str] = {}
    for item in raw.split(";"):
        if "=" not in item:
            raise ValueError("RRULE 字段格式无效")
        key, value = item.split("=", 1)
        if key not in _RRULE_KEYS or not value or key in parts:
            raise ValueError("RRULE 包含无效或重复字段")
        if len(value.split(",")) > 64:
            raise ValueError("RRULE 单个字段包含过多取值")
        parts[key] = value
    if parts.get("FREQ") not in _FREQUENCIES:
        raise ValueError("RRULE 缺少有效 FREQ")
    if "COUNT" in parts and "UNTIL" in parts:
        raise ValueError("RRULE 不能同时使用 COUNT 和 UNTIL")
    if "COUNT" in parts:
        count = _positive_int(parts["COUNT"], "COUNT")
        if count > max(1, cfg.max_occurrences):
            raise ValueError(f"执行次数不能超过 {cfg.max_occurrences}")
    if "INTERVAL" in parts:
        _positive_int(parts["INTERVAL"], "INTERVAL")
    unbounded = "COUNT" not in parts and "UNTIL" not in parts
    if unbounded and not cfg.allow_unbounded:
        raise ValueError("当前配置不允许无限周期")
    try:
        rule = _rrule(raw, start_at, timezone)
    except (TypeError, ValueError) as exc:
        raise ValueError("RRULE 取值无效") from exc
    occurrences = list(_take(rule.xafter(_local_datetime(start_at - 1, timezone), inc=True), 2))
    if not occurrences:
        raise ValueError("RRULE 不会产生任何执行时间")
    if len(occurrences) == 2:
        gap = int((occurrences[1] - occurrences[0]).total_seconds())
        if gap < max(1, cfg.min_interval_seconds):
            raise ValueError(f"周期至少为 {cfg.min_interval_seconds} 秒")
    if "UNTIL" in parts:
        generated = _take(
            rule.xafter(_local_datetime(start_at - 1, timezone), inc=True),
            max(1, cfg.max_occurrences) + 1,
        )
        if sum(1 for _item in generated) > max(1, cfg.max_occurrences):
            raise ValueError(f"时间范围内的执行次数不能超过 {cfg.max_occurrences}")
    return ";".join(f"{key}={value}" for key, value in parts.items())


def first_due_for_spec(spec: ScheduleSpec) -> int:
    if spec.kind == "once":
        return spec.start_at
    if not spec.rrule:
        raise ValueError("recurring schedule requires rrule")
    rule = _rrule(spec.rrule, spec.start_at, spec.timezone)
    first = rule.after(_local_datetime(spec.start_at - 1, spec.timezone), inc=True)
    if first is None:
        raise ValueError("RRULE has no first occurrence")
    return int(first.astimezone(UTC).timestamp())


def next_due_after(
    schedule: Schedule,
    after_epoch: int,
    *,
    anchor_is_occurrence: bool = True,
) -> int | None:
    if schedule.kind == "once" or not schedule.rrule:
        return None
    # Normal dispatch advances from a persisted occurrence. Re-anchoring there
    # avoids replaying an unbounded minutely rule from its original DTSTART on
    # every run. COUNT rules are capped by config and retain the original anchor
    # so their total occurrence limit remains exact.
    has_count = any(part.startswith("COUNT=") for part in schedule.rrule.split(";"))
    anchor = (
        after_epoch
        if anchor_is_occurrence and not has_count
        else schedule.start_at
    )
    result = _rrule(schedule.rrule, anchor, schedule.timezone).after(
        _local_datetime(after_epoch, schedule.timezone),
        inc=False,
    )
    return int(result.astimezone(UTC).timestamp()) if result is not None else None


def _recurring_misfire_plan(
    schedule: Schedule,
    now: int,
    *,
    grace: int,
    count_limit: int,
) -> tuple[int | None, int]:
    due = schedule.next_run_at
    if due is None or not schedule.rrule:
        return None, 0
    has_count = any(part.startswith("COUNT=") for part in schedule.rrule.split(";"))
    anchor = schedule.start_at if has_count else due
    iterator = _rrule(schedule.rrule, anchor, schedule.timezone).xafter(
        _local_datetime(due - 1, schedule.timezone),
        inc=True,
    )
    latest: int | None = None
    next_after: int | None = None
    missed = 0
    for occurrence in iterator:
        epoch = int(occurrence.astimezone(UTC).timestamp())
        if epoch > now:
            next_after = epoch
            break
        latest = epoch
        missed = min(count_limit, missed + 1)
    if latest is not None and now - latest <= grace:
        return latest, max(0, missed - 1)
    return next_after, missed


def _rrule(rule_text: str, start_at: int, timezone: str):
    return rrulestr(
        rule_text,
        dtstart=_local_datetime(start_at, timezone),
        cache=False,
    )


def _local_datetime(epoch: int, timezone: str) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone(ZoneInfo(timezone))


def _positive_int(text: str, field_name: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc
    if value <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return value


def _take(iterator: Iterator[datetime], count: int) -> Iterator[datetime]:
    for _index, item in zip(range(max(0, count)), iterator, strict=False):
        yield item
