"""QQ-facing schedule command and execution tests."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.main import App  # type: ignore
from qq_agent_bridge.policy import Policy  # type: ignore
from qq_agent_bridge.types import ChatEvent, ChatSegment  # type: ignore


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool, str, str | None]] = []
        self.sent_ats: list[tuple[str, tuple[str, ...], str, str | None]] = []

    def is_connected(self) -> bool:
        return True

    async def send(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self.sent.append((chat_id, is_group, text, echo))

    async def send_ats(
        self,
        chat_id: str,
        qqs: tuple[str, ...],
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self.sent_ats.append((chat_id, qqs, text, echo))

    async def send_at(
        self,
        chat_id: str,
        qq: str,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        await self.send_ats(chat_id, (qq,), text, echo, reply_to)

    async def send_image(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_file(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_voice(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeAgent:
    def __init__(self, results: list[str] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, str, str, str | None]] = []

    async def run(
        self,
        prompt: str,
        workspace: str,
        mode: str,
        model: str | None = None,
        progress=None,
    ) -> str:
        self.calls.append((prompt, workspace, mode, model))
        return self.results.pop(0) if self.results else "任务完成"


def make_cfg(tmp_path: Path) -> BridgeConfig:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["reader"],
        allowed_groups=["group"],
        commands={
            "ask": True,
            "task": True,
            "status": True,
            "stop": True,
            "help": True,
            "schedule": True,
        },
        workspaces={str(tmp_path): True},
    )
    cfg.agent.default_workspace = str(tmp_path)
    cfg.agent.use_bwrap = False
    cfg.agent.require_env = False
    cfg.scheduler.enabled = True
    cfg.scheduler.database_path = "data/schedules.sqlite3"
    cfg.scheduler.timezone = "Asia/Shanghai"
    cfg.scheduler.min_interval_seconds = 1
    cfg.scheduler.natural_language_progress_seconds = 60
    return cfg


def make_event(
    text: str,
    *,
    sender: str = "owner",
    group: str | None = "group",
    mid: str = "m1",
    segments: tuple[ChatSegment, ...] = (),
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=group or sender,
        sender_id=sender,
        is_group=group is not None,
        mentioned_bot=True,
        text=text,
        timestamp=1,
        segments=segments,
    )


def make_app(tmp_path: Path, agent: FakeAgent | None = None) -> tuple[App, FakeAdapter]:
    cfg = make_cfg(tmp_path)
    app = App(cfg, config_path=tmp_path / "config.yaml")
    adapter = FakeAdapter()
    app.adapter = adapter  # type: ignore[assignment]
    if agent:
        app.agent = agent  # type: ignore[assignment]
        app.cursor = agent  # type: ignore[assignment]
        app.schedule_nl_parser.agent = agent
    app.policy = Policy(cfg, app._agent_runner)
    app.scheduler.initialize()
    return app, adapter


async def drain_app(app: App) -> None:
    for _ in range(100):
        pending = [task for task in app._reply_tasks if not task.done()]
        if not pending:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("app background tasks did not finish")


def future_local_datetime() -> datetime:
    return (
        datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)
    ).replace(second=0, microsecond=0)


def test_schedule_help_contains_natural_and_structured_examples(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)

        await app._handle(make_event("/schedule help"))

        reply = adapter.sent[-1][2]
        assert "/schedule 每天早上八点告诉我北京市天气" in reply
        assert "/schedule daily 08:00" in reply
        assert "/schedule cancel -1" in reply
        assert "Asia/Shanghai" in reply

    asyncio.run(go())


def test_owner_creates_structured_schedule_and_list_shows_it(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        scheduled_for = future_local_datetime().strftime("%Y-%m-%d %H:%M")

        await app._handle(
            make_event(
                f"/schedule once {scheduled_for} -- send 记得开会",
                mid="create-1",
            )
        )
        await app._handle(make_event("/schedule list", mid="list-1"))

        assert "已经设置好了" in adapter.sent[-2][2]
        assert scheduled_for in adapter.sent[-2][2]
        assert "记得开会" in adapter.sent[-1][2]
        assert "0." in adapter.sent[-1][2]

    asyncio.run(go())


def test_group_non_owner_cannot_create_owner_only_schedule(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.commands["schedule"] = "owner"

        await app._handle(
            make_event(
                "/schedule in 10m -- send test",
                sender="reader",
                mid="denied-1",
            )
        )

        assert adapter.sent[-1][2] == "[denied] owner-only"
        assert app.schedule_store.list_for_chat("group", True) == []

    asyncio.run(go())


def test_group_non_owner_can_create_schedule_when_group_permission_is_user(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        agent = FakeAgent(
            [
                """{
                  "ambiguous": false,
                  "kind": "rrule",
                  "action": "send",
                  "payload": "查询济宁市天气：如果有雨，就在群里提醒；如果没雨就不需要发消息",
                  "send_text": "查询济宁市天气：如果有雨，就在群里提醒；如果没雨就不需要发消息",
                  "time_phrase": "每天早上7点",
                  "payload_phrase": "查询济宁市天气：如果有雨，就在群里提醒；如果没雨就不需要发消息",
                  "dtstart_local": "2099-01-01T07:00",
                  "rrule": "FREQ=DAILY",
                  "clarification": "",
                  "safety": {"safe": true, "risk_level": "low", "reason": "每日天气提醒频率低"}
                }"""
            ]
        )
        app, adapter = make_app(tmp_path, agent)
        app.cfg.commands["schedule"] = "owner"
        app.cfg.command_groups["group"] = {"schedule": "user"}

        await app._handle(
            make_event(
                "/schedule 每天早上7点查询济宁市天气：如果有雨，就在群里提醒；如果没雨就不需要发消息",
                sender="reader",
                mid="group-user-schedule",
            )
        )
        await drain_app(app)

        assert any("正在理解你说的时间和任务内容" in item[2] for item in adapter.sent)
        assert "已经设置好了" in adapter.sent[-1][2]
        schedules = app.schedule_store.list_for_chat("group", True)
        assert len(schedules) == 1
        assert schedules[0].creator_id == "reader"

    asyncio.run(go())


def test_private_allowed_user_can_create_own_schedule(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(
            [
                """{
                  "ambiguous": false,
                  "kind": "once",
                  "action": "send",
                  "payload": "喝水",
                  "send_text": "喝水",
                  "time_phrase": "in 10m",
                  "payload_phrase": "喝水",
                  "dtstart_local": "2099-01-01T10:00",
                  "rrule": null,
                  "clarification": "",
                  "safety": {"safe": true, "risk_level": "low", "reason": "一次提醒"}
                }"""
            ]
        )
        app, adapter = make_app(tmp_path, agent)

        await app._handle(
            make_event(
                "/schedule in 10m -- send 喝水",
                sender="reader",
                group=None,
                mid="private-1",
            )
        )

        await drain_app(app)
        assert "已经设置好了" in adapter.sent[-1][2]
        saved = app.schedule_store.list_for_chat("reader", False)
        assert len(saved) == 1
        assert saved[0].creator_id == "reader"

    asyncio.run(go())


def test_private_user_unsafe_schedule_is_rejected_without_persistence(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(
            [
                """{
                  "ambiguous": false,
                  "kind": "rrule",
                  "action": "task",
                  "payload": "不停搜索并发送结果",
                  "send_text": null,
                  "time_phrase": "每分钟",
                  "payload_phrase": "不停搜索并发送结果",
                  "dtstart_local": "2099-01-01T10:00",
                  "rrule": "FREQ=MINUTELY",
                  "clarification": "",
                  "safety": {"safe": false, "risk_level": "high", "reason": "可能形成持续请求风暴"}
                }"""
            ]
        )
        app, adapter = make_app(tmp_path, agent)

        await app._handle(
            make_event(
                "/schedule 每分钟不停搜索并发送结果",
                sender="reader",
                group=None,
                mid="private-unsafe-1",
            )
        )
        await drain_app(app)

        assert "安全审查未通过" in adapter.sent[-1][2]
        assert "没有创建定时任务" in adapter.sent[-1][2]
        assert app.schedule_store.list_for_chat("reader", False) == []
        assert len(agent.calls) == 1

    asyncio.run(go())


def test_schedule_management_uses_indices_and_defaults_to_latest(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        await app._handle(
            make_event("/schedule every 1h forever -- send first", mid="manage-1")
        )
        await app._handle(
            make_event("/schedule every 2h forever -- send second", mid="manage-2")
        )

        await app._handle(make_event("/schedule pause", mid="manage-pause"))
        schedules = app.schedule_store.list_for_chat("group", True)
        assert schedules[-1].payload == "second"
        assert schedules[-1].status == "paused"

        await app._handle(make_event("/schedule resume -1", mid="manage-resume"))
        assert app.schedule_store.list_for_chat("group", True)[-1].status == "active"

        await app._handle(make_event("/schedule cancel 0", mid="manage-cancel"))
        remaining = app.schedule_store.list_for_chat("group", True)
        assert [item.payload for item in remaining] == ["second"]
        assert adapter.sent[-1][2] == "已取消这个定时任务。"

    asyncio.run(go())


def test_schedule_visibility_and_management_are_scoped_to_creator_unless_owner(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        await app._handle(
            make_event(
                "/schedule every 1h forever -- send owner-only reminder",
                sender="owner",
                mid="owner-schedule",
            )
        )
        await app._handle(
            make_event(
                "/schedule every 2h forever -- send reader reminder",
                sender="reader",
                mid="reader-schedule",
            )
        )
        owner_schedule, reader_schedule = app.schedule_store.list_for_chat("group", True)

        await app._handle(make_event("/schedule list", sender="reader", mid="reader-list"))
        reader_list = adapter.sent[-1][2]
        assert "reader reminder" in reader_list
        assert "owner-only reminder" not in reader_list
        assert owner_schedule.id not in reader_list

        await app._handle(
            make_event(
                f"/schedule cancel {owner_schedule.id}",
                sender="reader",
                mid="reader-cancel-owner",
            )
        )
        assert "没有找到这个定时任务" in adapter.sent[-1][2]
        assert app.schedule_store.get(owner_schedule.id).status == "active"

        await app._handle(
            make_event(
                f"/schedule cancel {reader_schedule.id}",
                sender="owner",
                mid="owner-cancel-reader",
            )
        )
        assert "已取消这个定时任务" in adapter.sent[-1][2]
        assert app.schedule_store.get(reader_schedule.id).status == "cancelled"

        await app._handle(make_event("/schedule list", sender="owner", mid="owner-list"))
        owner_list = adapter.sent[-1][2]
        assert "owner-only reminder" in owner_list
        assert "reader reminder" not in owner_list

    asyncio.run(go())


def test_schedule_help_remains_available_when_dispatcher_is_disabled(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.enabled = False

        await app._handle(make_event("/schedule help", mid="disabled-help"))

        assert "/schedule 每天早上八点告诉我北京市天气" in adapter.sent[-1][2]

    asyncio.run(go())


def test_private_schedule_mutation_can_be_disabled(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.allow_private_users = False

        await app._handle(
            make_event(
                "/schedule in 10m -- send 喝水",
                sender="reader",
                group=None,
                mid="private-disabled",
            )
        )

        assert adapter.sent[-1][2] == "[denied] private-schedule-disabled"
        assert app.schedule_store.list_for_chat("reader", False) == []

    asyncio.run(go())


def test_natural_schedule_replies_immediately_then_sends_canonical_receipt(tmp_path: Path) -> None:
    async def go() -> None:
        scheduled_for = future_local_datetime().strftime("%Y-%m-%dT%H:%M")
        agent = FakeAgent(
            [
                f"""{{
                  "ambiguous": false,
                  "kind": "rrule",
                  "action": "task",
                  "payload": "告诉我北京市天气",
                  "time_phrase": "每天早上八点",
                  "payload_phrase": "告诉我北京市天气",
                  "dtstart_local": "{scheduled_for}",
                  "rrule": "FREQ=DAILY",
                  "clarification": ""
                }}"""
            ]
        )
        app, adapter = make_app(tmp_path, agent)

        await app._handle(
            make_event(
                "/schedule 每天早上八点告诉我北京市天气",
                mid="natural-1",
            )
        )

        assert adapter.sent[0][2] == "收到，我正在理解你说的时间和任务内容，稍等一下。"
        assert app.policy is not None
        assert "schedule" in app.policy.get_status()
        await drain_app(app)
        assert "已经设置好了" in adapter.sent[-1][2]
        assert "每天早上八点" in adapter.sent[-1][2]
        assert len(app.schedule_store.list_for_chat("group", True)) == 1

    asyncio.run(go())


def test_natural_schedule_ambiguity_explains_that_nothing_was_created(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(
            [
                """{
                  "ambiguous": true,
                  "kind": null,
                  "action": null,
                  "payload": "",
                  "time_phrase": "每天",
                  "payload_phrase": "提醒我吃药",
                  "dtstart_local": null,
                  "rrule": null,
                  "clarification": "还缺少每天执行的具体时间。"
                }"""
            ]
        )
        app, adapter = make_app(tmp_path, agent)

        await app._handle(make_event("/schedule 每天提醒我吃药", mid="natural-ambiguous"))
        await drain_app(app)

        assert "具体时间" in adapter.sent[-1][2]
        assert "没有创建" in adapter.sent[-1][2]
        assert app.schedule_store.list_for_chat("group", True) == []

    asyncio.run(go())


def test_scheduled_send_preserves_real_qq_mentions(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        target = ChatSegment(type="mention", text="@12345 ", qq="12345", raw_type="at")
        now = int(datetime.now(tz=UTC).timestamp())

        await app._handle(
            make_event(
                "/schedule in 1s -- send @12345 起来喝水",
                mid="at-create",
                segments=(target,),
            )
        )
        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()

        assert adapter.sent_ats[-1][1] == ("12345",)
        assert adapter.sent_ats[-1][2] == "起来喝水"

    asyncio.run(go())


def test_scheduled_task_reuses_job_pipeline_and_sends_result(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(["北京今天晴，最高 31°C。"])
        app, adapter = make_app(tmp_path, agent)
        now = int(datetime.now(tz=UTC).timestamp())

        await app._handle(
            make_event(
                "/schedule in 1s -- task 查询北京天气",
                mid="task-create",
            )
        )
        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()
        await drain_app(app)

        assert any("定时任务" in item[2] and "开始执行" in item[2] for item in adapter.sent)
        assert any("北京今天晴" in item[2] for item in adapter.sent)
        scheduled_jobs = [job for job in app.policy.jobs.values() if job.source == "schedule"]  # type: ignore[union-attr]
        assert len(scheduled_jobs) == 1
        assert scheduled_jobs[0].cmd == "task"
        assert "计划触发时间" in agent.calls[-1][0]

    asyncio.run(go())


def test_scheduled_task_mentions_target_in_progress_and_final_reply(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(["该喝水了"])
        app, adapter = make_app(tmp_path, agent)
        target = ChatSegment(type="mention", text="@12345 ", qq="12345", raw_type="at")
        now = int(datetime.now(tz=UTC).timestamp())
        await app._handle(
            make_event(
                "/schedule in 1s -- task @12345 提醒喝水",
                mid="task-at-create",
                segments=(target,),
            )
        )

        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()
        await drain_app(app)

        assert [item[1] for item in adapter.sent_ats[-2:]] == [("12345",), ("12345",)]
        assert adapter.sent_ats[-1][2] == "该喝水了"

    asyncio.run(go())


def test_scheduled_task_rechecks_permissions_at_execution_time(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(["不应该执行"])
        app, adapter = make_app(tmp_path, agent)
        now = int(datetime.now(tz=UTC).timestamp())
        await app._handle(
            make_event("/schedule in 1s -- task 查询北京天气", mid="permission-create")
        )
        app.cfg.commands["task"] = False

        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()

        saved = app.schedule_store.list_for_chat("group", True, active_only=False)[0]
        assert saved.failure_count == 1
        assert agent.calls == []
        assert any("cmd-disabled" in item[2] for item in adapter.sent)

    asyncio.run(go())


def test_group_schedule_stops_when_creator_is_no_longer_owner(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.commands["schedule"] = "owner"
        now = int(datetime.now(tz=UTC).timestamp())
        await app._handle(
            make_event("/schedule in 1s -- send 不应该发送", mid="owner-create")
        )
        app.cfg.owners = []

        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()

        saved = app.schedule_store.list_for_chat("group", True, active_only=False)[0]
        assert saved.failure_count == 1
        assert not any(item[2] == "不应该发送" for item in adapter.sent)
        assert any("owner-only" in item[2] for item in adapter.sent)

    asyncio.run(go())


def test_group_schedule_created_under_user_permission_runs_for_non_owner(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.commands["schedule"] = "owner"
        app.cfg.command_groups["group"] = {"schedule": "user"}
        now = int(datetime.now(tz=UTC).timestamp())
        await app._handle(
            make_event(
                "/schedule in 1s -- send 非 owner 定时提醒",
                sender="reader",
                mid="reader-create",
            )
        )

        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()

        saved = app.schedule_store.list_for_chat("group", True, active_only=False)[0]
        assert saved.failure_count == 0
        assert any(item[2] == "非 owner 定时提醒" for item in adapter.sent)

    asyncio.run(go())
