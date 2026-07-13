"""Persistent schedule store tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.schedule_store import ScheduleStore  # type: ignore
from qq_agent_bridge.scheduler import Schedule, ScheduleSpec  # type: ignore


def make_schedule(*, schedule_id: str = "s1", next_run_at: int = 100) -> Schedule:
    spec = ScheduleSpec(
        kind="rrule",
        action="task",
        payload="检查服务状态",
        timezone="Asia/Shanghai",
        start_at=next_run_at,
        rrule="FREQ=MINUTELY;COUNT=3",
        description="每分钟，共 3 次",
    )
    return Schedule.from_spec(
        spec,
        schedule_id=schedule_id,
        chat_id="group",
        is_group=True,
        creator_id="owner",
        source_message_id="m1",
        created_at=90,
    )


def test_store_persists_and_resolves_active_schedule_indices(tmp_path: Path) -> None:
    path = tmp_path / "data" / "schedules.sqlite3"
    store = ScheduleStore(path)
    store.initialize()
    store.create(make_schedule(schedule_id="s-old"))
    second = make_schedule(schedule_id="s-new")
    second.source_message_id = "m2"
    store.create(second)

    reopened = ScheduleStore(path)
    reopened.initialize()

    assert reopened.resolve_ref("group", True, "0").id == "s-old"  # type: ignore[union-attr]
    assert reopened.resolve_ref("group", True, "-1").id == "s-new"  # type: ignore[union-attr]
    assert reopened.get_by_source_message("group", True, "m1").id == "s-old"  # type: ignore[union-attr]
    assert reopened.get("s-old").rrule == "FREQ=MINUTELY;COUNT=3"  # type: ignore[union-attr]
    assert reopened.get("s-old").description == "每分钟，共 3 次"  # type: ignore[union-attr]


def test_source_message_deduplication_is_scoped_to_chat(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    store.initialize()
    first = make_schedule(schedule_id="s-group")
    second = make_schedule(schedule_id="s-private")
    second.chat_id = "owner"
    second.is_group = False
    store.create(first)
    store.create(second)

    assert store.get_by_source_message("group", True, "m1").id == "s-group"  # type: ignore[union-attr]
    assert store.get_by_source_message("owner", False, "m1").id == "s-private"  # type: ignore[union-attr]


def test_store_claim_is_atomic_and_completion_updates_counts(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    store.initialize()
    store.create(make_schedule())

    run = store.claim("s1", expected_due_at=100, claimed_at=100, next_run_at=160)
    duplicate = store.claim("s1", expected_due_at=100, claimed_at=100, next_run_at=160)

    assert run is not None
    assert duplicate is None
    claimed = store.get("s1")
    assert claimed is not None
    assert claimed.run_count == 1
    assert claimed.next_run_at == 160

    store.finish_run(run.id, "succeeded", finished_at=105)
    finished = store.get("s1")
    assert finished is not None
    assert finished.success_count == 1
    assert finished.consecutive_failures == 0


def test_store_recovers_interrupted_runs_without_replaying_claim(tmp_path: Path) -> None:
    path = tmp_path / "schedules.sqlite3"
    store = ScheduleStore(path)
    store.initialize()
    store.create(make_schedule())
    run = store.claim("s1", expected_due_at=100, claimed_at=100, next_run_at=160)
    assert run is not None

    reopened = ScheduleStore(path)
    reopened.initialize()
    recovered = reopened.recover_interrupted(now=120)

    assert recovered == 1
    assert reopened.list_runs("s1")[0].state == "interrupted"
    assert reopened.get("s1").next_run_at == 160  # type: ignore[union-attr]


def test_store_prunes_run_details_but_keeps_aggregate_counts(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    store.initialize()
    store.create(make_schedule())

    for due_at in (100, 160, 220):
        run = store.claim(
            "s1",
            expected_due_at=due_at,
            claimed_at=due_at,
            next_run_at=due_at + 60,
        )
        assert run is not None
        store.finish_run(
            run.id,
            "succeeded",
            finished_at=due_at + 1,
            max_run_history=2,
        )

    saved = store.get("s1")
    assert saved is not None
    assert saved.success_count == 3
    assert len(store.list_runs("s1")) == 2
