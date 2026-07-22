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
from qq_agent_bridge.types import ChatEvent, ChatReply, ChatSegment  # type: ignore


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool, str, str | None]] = []
        self.sent_ats: list[tuple[str, tuple[str, ...], str, str | None, str | None]] = []

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
        self.sent_ats.append((chat_id, qqs, text, echo, reply_to))

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
    reply: ChatReply | None = None,
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
        reply=reply,
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


def test_schedule_send_preserves_multiple_mentions_and_reply_reference(tmp_path: Path) -> None:
    async def go() -> None:
        app, adapter = make_app(tmp_path)
        event = make_event(
            "/schedule in 10m -- send 记得集合",
            mid="send-meta",
            segments=(
                ChatSegment(type="mention", qq="12345", text="@12345 "),
                ChatSegment(type="mention", qq="67890", text="@67890 "),
            ),
            reply=ChatReply(message_id="9988", text="原消息"),
        )

        await app._handle(event)
        schedule = app.schedule_store.list_for_chat("group", True)[0]
        assert schedule.mentions == ("12345", "67890")
        assert schedule.reply_to_message_id == "9988"

        await app._send_schedule_text(schedule, "记得集合", "send-meta-run")

        assert adapter.sent_ats[-1][1] == ("12345", "67890")
        assert adapter.sent_ats[-1][2] == "记得集合"
        assert adapter.sent_ats[-1][4] == "9988"

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


def test_scheduled_artifact_validation_failure_counts_as_failed(tmp_path: Path) -> None:
    async def go() -> None:
        agent = FakeAgent(["QQBOT_SEND_FILE: wrong-token missing.pdf", ""])
        app, adapter = make_app(tmp_path, agent)
        now = int(datetime.now(tz=UTC).timestamp())

        await app._handle(
            make_event(
                "/schedule in 1s -- task 生成报告文件",
                mid="artifact-failure-create",
            )
        )
        await app.scheduler.tick(now=now + 2)
        await app.scheduler.wait_for_runs()
        await drain_app(app)

        saved = app.schedule_store.list_for_chat("group", True, active_only=False)[0]
        assert saved.failure_count == 1
        assert any("文件没有成功生成或无法验证" in item[2] for item in adapter.sent)
        assert saved.last_error == "artifact delivery failed"

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


# -- Non-owner explicit schedule safety constraints ---------------------------------


def test_non_owner_explicit_too_frequent_interval_blocked(tmp_path: Path) -> None:
    """Non-owner cannot create a schedule with recurrence faster than the floor."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_min_interval_seconds = 300

        # every 30s — too fast
        await app._handle(
            make_event(
                "/schedule every 30s forever -- send fast-spam",
                sender="reader",
                mid="fast-1",
            )
        )
        assert "周期不能少于" in adapter.sent[-1][2]
        assert app.schedule_store.list_for_chat("group", True) == []

        # every 10m — ok (600s > 300s)
        await app._handle(
            make_event(
                "/schedule every 10m count 3 -- send slow-ok",
                sender="reader",
                mid="slow-1",
            )
        )
        assert "已经设置好了" in adapter.sent[-1][2]
        assert len(app.schedule_store.list_for_chat("group", True)) == 1

    asyncio.run(go())


def test_non_owner_explicit_unbounded_can_be_disabled(tmp_path: Path) -> None:
    """When non_owner_allow_unbounded=False, forever schedules are rejected."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_allow_unbounded = False

        await app._handle(
            make_event(
                "/schedule every 10m forever -- send forever-reminder",
                sender="reader",
                mid="forever-1",
            )
        )
        assert "不允许创建无限次数" in adapter.sent[-1][2]
        assert app.schedule_store.list_for_chat("group", True) == []

    asyncio.run(go())


def test_non_owner_explicit_excessive_count_blocked(tmp_path: Path) -> None:
    """Non-owner is capped at non_owner_max_occurrences."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_max_occurrences = 10

        await app._handle(
            make_event(
                "/schedule every 10m count 50 -- send too-many",
                sender="reader",
                mid="many-1",
            )
        )
        assert "次数不能超过" in adapter.sent[-1][2]
        assert app.schedule_store.list_for_chat("group", True) == []

    asyncio.run(go())


def test_non_owner_explicit_too_many_mentions_blocked(tmp_path: Path) -> None:
    """Non-owner cannot @mention more than the allowed number of people."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_max_mentions = 1

        await app._handle(
            make_event(
                "/schedule in 10m -- send @111 @222 hello",
                sender="reader",
                mid="mentions-1",
                segments=(
                    ChatSegment(type="mention", text="@111 ", qq="111", raw_type="at"),
                    ChatSegment(type="mention", text="@222 ", qq="222", raw_type="at"),
                    ChatSegment(type="text", text="hello", qq=""),
                ),
            )
        )
        assert "只能 @" in adapter.sent[-1][2]
        assert app.schedule_store.list_for_chat("group", True) == []

    asyncio.run(go())


def test_non_owner_explicit_cooldown_enforced(tmp_path: Path) -> None:
    """Rapid schedule creation is rate-limited for non-owners."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_cooldown_seconds = 30

        await app._handle(
            make_event(
                "/schedule in 10m -- send first",
                sender="reader",
                mid="cd-1",
            )
        )
        assert "已经设置好了" in adapter.sent[-1][2]

        await app._handle(
            make_event(
                "/schedule in 20m -- send second",
                sender="reader",
                mid="cd-2",
            )
        )
        assert "创建太频繁" in adapter.sent[-1][2]
        assert len(app.schedule_store.list_for_chat("group", True)) == 1

    asyncio.run(go())


def test_non_owner_explicit_count_cap_per_chat(tmp_path: Path) -> None:
    """Non-owner cannot exceed per-chat active schedule limit."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_max_schedules_per_chat = 2
        app.cfg.scheduler.non_owner_cooldown_seconds = 0

        for i in range(2):
            await app._handle(
                make_event(
                    f"/schedule in {10 + i * 10}m -- send msg{i}",
                    sender="reader",
                    mid=f"cap-{i}",
                )
            )
            assert "已经设置好了" in adapter.sent[-1][2]

        await app._handle(
            make_event(
                "/schedule in 30m -- send extra",
                sender="reader",
                mid="cap-extra",
            )
        )
        assert "最多" in adapter.sent[-1][2] and "定时任务" in adapter.sent[-1][2]
        assert len(app.schedule_store.list_for_chat("group", True)) == 2

    asyncio.run(go())


def test_schedule_help_shows_non_owner_constraints(tmp_path: Path) -> None:
    """Non-owner help text includes safety constraint information."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        await app._handle(make_event("/schedule help", sender="reader"))
        reply = adapter.sent[-1][2]
        assert "非 owner 用户结构化限制" in reply
        assert "最小周期" in reply
        assert "最多次数" in reply
        assert "冷却" in reply

    asyncio.run(go())


def test_schedule_help_omits_constraints_for_owner(tmp_path: Path) -> None:
    """Owner help text does not show non-owner constraint section."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        await app._handle(make_event("/schedule help", sender="owner"))
        reply = adapter.sent[-1][2]
        assert "每天早上八点" in reply  # still has examples
        assert "非 owner 用户" not in reply

    asyncio.run(go())


def test_owner_bypasses_all_non_owner_constraints(tmp_path: Path) -> None:
    """Owner schedules are never gated by non-owner safety constraints."""

    async def go() -> None:
        app, adapter = make_app(tmp_path)
        app.cfg.scheduler.non_owner_min_interval_seconds = 300
        app.cfg.scheduler.non_owner_allow_unbounded = False
        app.cfg.scheduler.non_owner_max_occurrences = 10
        app.cfg.scheduler.non_owner_cooldown_seconds = 30

        # Owner can still create fast, unbounded, high-count schedules
        await app._handle(
            make_event(
                "/schedule every 30s forever -- send owner-fast",
                sender="owner",
                mid="owner-fast",
            )
        )
        assert "已经设置好了" in adapter.sent[-1][2]

        await app._handle(
            make_event(
                "/schedule every 5s count 100 -- send owner-many",
                sender="owner",
                mid="owner-many",
            )
        )
        assert "已经设置好了" in adapter.sent[-1][2]

        assert len(app.schedule_store.list_for_chat("group", True)) == 2

    asyncio.run(go())


# ── Real-agent E2E tests ────────────────────────────────────────────────────

_APP_E2E_ENV = "QQ_AGENT_BRIDGE_APP_E2E"

import os as _os

def _require_app_e2e() -> None:
    if _os.environ.get(_APP_E2E_ENV) != "1":
        pytest.skip(f"set {_APP_E2E_ENV}=1 to run real App+agent schedule E2E")


def test_real_agent_schedule_full_pipeline_through_app_handle(
    tmp_path: Path,
) -> None:
    """Full schedule pipeline through App._handle with real agent.

    Requires QQ_AGENT_BRIDGE_APP_E2E=1.
    /schedule → App._handle → structured parse → schedule created →
    receipt sent → list confirms.
    """
    _require_app_e2e()

    async def go() -> None:
        from datetime import UTC, datetime, timedelta

        # Load production config as base, override for test
        cfg = BridgeConfig.load("config.yaml")
        cfg.owners = ["owner"]
        cfg.allowed_users = ["reader", "owner"]
        cfg.allowed_groups = ["group"]
        cfg.commands = {"ask": True, "task": True, "schedule": True, "stop": True}
        cfg.workspaces[str(tmp_path)] = True
        cfg.agent.default_workspace = str(tmp_path)
        runtime = _os.environ.get("QQ_AGENT_BRIDGE_E2E_RUNTIME", "")
        if runtime:
            cfg.agent.runtime = runtime
        cfg.agent.binary = _os.environ.get("QQ_AGENT_BRIDGE_E2E_BINARY", "")
        cfg.agent.env_runner = _os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_RUNNER", "")
        cfg.agent.env_name = _os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_NAME", "")
        cfg.agent.require_env = False
        cfg.agent.max_runtime_seconds = int(
            _os.environ.get("QQ_AGENT_BRIDGE_E2E_TIMEOUT", "90")
        )
        cfg.agent.max_output_chars = 8000
        cfg.scheduler.enabled = True
        cfg.scheduler.database_path = str(tmp_path / "schedules.sqlite3")
        cfg.scheduler.timezone = "Asia/Shanghai"
        cfg.scheduler.min_interval_seconds = 1
        cfg.scheduler.natural_language_model = _os.environ.get(
            "QQ_AGENT_BRIDGE_E2E_CHAT_MODEL", "auto"
        )
        cfg.scheduler.natural_language_progress_seconds = 60
        cfg.resources.enabled = False
        cfg.storage_maintenance.enabled = False

        adapter = FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001
        app.scheduler.initialize()

        # Create a structured schedule to verify full App pipeline works
        future_time = datetime.now(tz=UTC) + timedelta(days=30)
        scheduled_for = future_time.strftime("%Y-%m-%d %H:%M")

        await app._handle(  # noqa: SLF001
            make_event(
                f"/schedule once {scheduled_for} -- send test-e2e-real-app",
                sender="owner",
                group="group",
                mid="real-struct",
            )
        )
        await drain_app(app)

        assert "已经设置好了" in adapter.sent[-1][2], (
            f"structured schedule creation failed: {adapter.sent}"
        )

        # Verify schedule exists in store
        schedules = app.schedule_store.list_for_chat("group", True)
        assert len(schedules) >= 1, "schedule should be in store"

        # List to confirm
        adapter.sent.clear()
        await app._handle(  # noqa: SLF001
            make_event("/schedule list", sender="owner", group="group", mid="real-list")
        )
        await drain_app(app)
        assert any(
            "test-e2e-real-app" in s[2] for s in adapter.sent
        ), f"schedule list should show created schedule: {adapter.sent}"

    asyncio.run(go())
