"""App-level scheduling tests."""
from __future__ import annotations

import asyncio
import re
import sys
import wave
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.cursor_adapter import CustomCommandAdapter  # type: ignore
from qq_agent_bridge.main import App  # type: ignore
from qq_agent_bridge.policy import Job, Policy  # type: ignore
from qq_agent_bridge.resources import PreparedResource  # type: ignore
from qq_agent_bridge.types import ChatEvent, ChatResource  # type: ignore


def extract_outgoing_prompt_context(prompt: str) -> tuple[str, str]:
    outbox_match = re.search(r"可发送资源目录：(.+)", prompt)
    token_match = re.search(r"资源发送令牌：(\S+)", prompt)
    assert outbox_match is not None
    assert token_match is not None
    return outbox_match.group(1).strip(), token_match.group(1)


def write_wav(path: Path, duration_seconds: int, sample_rate: int = 8000) -> None:
    frames = b"\0\0" * sample_rate * duration_seconds
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool, str, str | None]] = []
        self.sent_at: list[tuple[str, str, str, str | None]] = []
        self.sent_images: list[tuple[str, bool, Path, str | None]] = []
        self.sent_files: list[tuple[str, bool, Path, str | None]] = []
        self.sent_voices: list[tuple[str, bool, Path, str | None]] = []

    async def send(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
    ) -> None:
        self.sent.append((chat_id, is_group, text, echo))

    async def send_image(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_images.append((chat_id, is_group, path, echo))

    async def send_file(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_files.append((chat_id, is_group, path, echo))

    async def send_voice(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_voices.append((chat_id, is_group, path, echo))

    async def send_at(
        self,
        chat_id: str,
        qq: str,
        text: str,
        echo: str | None = None,
    ) -> None:
        self.sent_at.append((chat_id, qq, text, echo))


def make_cfg() -> BridgeConfig:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["reader"],
        allowed_groups=["group"],
        commands={
            "ask": True,
            "plan": True,
            "search": True,
            "task": True,
            "status": True,
            "help": True,
            "profile": True,
            "reset": True,
            "code": True,
            "approve": True,
            "stop": True,
            "reload": True,
        },
        workspaces={"/tmp": True},
        dangerous_requires_confirm=True,
    )
    cfg.agent.default_workspace = "/tmp"
    return cfg


def make_ev(
    text: str,
    sender: str = "reader",
    group: str | None = None,
    mid: str = "m1",
    mentioned: bool = True,
    resources: tuple[ChatResource, ...] = (),
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=group or sender,
        sender_id=sender,
        is_group=group is not None,
        mentioned_bot=mentioned,
        text=text,
        timestamp=1,
        resources=resources,
    )


async def wait_until_sent(adapter: FakeAdapter, expected: str) -> None:
    for _ in range(200):
        if any(expected in item[2] for item in adapter.sent):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"message containing {expected!r} was not sent: {adapter.sent!r}")


def make_app(cfg: BridgeConfig, runner: Any, adapter: FakeAdapter) -> App:
    app = App(cfg)
    app.adapter = adapter  # type: ignore[assignment]

    async def job_runner(job: Any) -> str:
        return await runner(job.cmd, job.args, job.event)

    app.policy = Policy(cfg, job_runner)
    return app


def test_handle_returns_before_ask_job_finishes() -> None:
    async def go() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            started.set()
            await release.wait()
            return f"reply to {args}"

        app = make_app(cfg, runner, adapter)

        await asyncio.wait_for(app._handle(make_ev("/ask hello")), timeout=0.05)
        await asyncio.wait_for(started.wait(), timeout=0.2)
        assert adapter.sent == []

        release.set()
        await wait_until_sent(adapter, "reply to hello")

    asyncio.run(go())


def test_app_uses_configured_custom_agent_runtime() -> None:
    cfg = make_cfg()
    cfg.agent.runtime = "custom-cli"
    cfg.agent.command = {"ask": ["agent-bin", "{prompt}"]}
    cfg.agent.env_runner = ""
    cfg.agent.use_bwrap = False

    app = App(cfg)

    assert isinstance(app.cursor, CustomCommandAdapter)


def test_bare_group_mention_casual_text_replies_without_ask_job() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        called = False

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            nonlocal called
            called = True
            return f"{cmd}:{args}"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("@123456 你好", group="group", mid="casual-at"))

        assert not called
        assert app.policy.jobs == {}  # type: ignore[union-attr]
        assert adapter.sent == [("group", True, "在呢", "casual-at")]

    asyncio.run(go())


def test_group_explicit_ask_command_still_invokes_ask_job() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return f"{cmd}:{args}"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("@123456 /ask 你好", group="group", mid="explicit-ask"))
        await wait_until_sent(adapter, "ask:你好")

    asyncio.run(go())


def test_echo_only_ignores_unmentioned_group_messages() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        app = App(make_cfg(), echo_only=True)
        app.adapter = adapter  # type: ignore[assignment]

        await app._handle(make_ev("/task hello", group="group", mentioned=False, mid="echo-no-at"))

        assert adapter.sent == []

    asyncio.run(go())


def test_echo_only_replies_to_mentioned_group_messages() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        app = App(make_cfg(), echo_only=True)
        app.adapter = adapter  # type: ignore[assignment]

        await app._handle(make_ev("@1000000001 ping", group="group", mentioned=True, mid="echo-at"))

        assert adapter.sent == [("group", True, "[echo] @1000000001 ping", "echo-at")]

    asyncio.run(go())


def test_unmentioned_group_messages_can_trigger_batched_proactive_reply() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.batch_seconds = 0.01
        cfg.proactive.min_messages = 3
        cfg.proactive.cooldown_seconds = 0
        cfg.proactive.quiet_after_bot_seconds = 0

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            assert mode == "ask"
            assert model == "auto"
            assert "最近聊天" in prompt
            return '{"speak": true, "reply": "我插一句，先看现象再猜原因会稳一点。"}'

        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("线上接口变慢了", group="group", mentioned=False, mid="pro-1"))
        await app._handle(make_ev("是不是缓存问题", group="group", mentioned=False, mid="pro-2"))
        await app._handle(make_ev("先看日志？", group="group", mentioned=False, mid="pro-3"))
        await wait_until_sent(adapter, "先看现象")
        await app.proactive.stop()

        assert adapter.sent == [
            ("group", True, "我插一句，先看现象再猜原因会稳一点。", "proactive-pro-3")
        ]

    asyncio.run(go())


def test_unmentioned_group_proactive_reply_can_at_recent_sender() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.batch_seconds = 0.01
        cfg.proactive.min_messages = 3
        cfg.proactive.cooldown_seconds = 0
        cfg.proactive.quiet_after_bot_seconds = 0
        cfg.proactive.reply_message_delay_seconds = 0

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]

        async def fake_cursor(*args: Any, **kwargs: Any) -> str:
            return '{"speak": true, "messages": [{"at": "12345", "text": "你这个梗接住了"}]}'

        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("这机器人有点意思", sender="12345", group="group", mentioned=False, mid="pro-at-1"))
        await app._handle(make_ev("感觉可以整活", sender="23456", group="group", mentioned=False, mid="pro-at-2"))
        await app._handle(make_ev("来一句", sender="12345", group="group", mentioned=False, mid="pro-at-3"))
        for _ in range(200):
            if adapter.sent_at:
                break
            await asyncio.sleep(0.01)
        await app.proactive.stop()

        assert adapter.sent_at == [("group", "12345", "你这个梗接住了", "proactive-pro-at-3")]
        assert adapter.sent == []

    asyncio.run(go())


def test_unmentioned_group_commands_do_not_trigger_proactive_reply() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.batch_seconds = 0.01
        cfg.proactive.min_messages = 1
        called = False

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]

        async def fake_cursor(*args: Any, **kwargs: Any) -> str:
            nonlocal called
            called = True
            return '{"speak": true, "reply": "不该发送"}'

        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task 未at不应触发", group="group", mentioned=False, mid="pro-cmd-1"))
        await app._handle(
            make_ev("@示例机器人 /task 复制出来也不应触发", group="group", mentioned=False, mid="pro-cmd-2")
        )
        await asyncio.sleep(0.05)
        await app.proactive.stop()

        assert not called
        assert adapter.sent == []

    asyncio.run(go())


def test_proactive_uses_ambient_context_from_previous_batch() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.batch_seconds = 0.01
        cfg.proactive.min_messages = 2
        cfg.proactive.cooldown_seconds = 0
        cfg.proactive.quiet_after_bot_seconds = 0
        prompts: list[str] = []

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return '{"speak": false, "reply": ""}'

        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("我昨天晚上吃的麦当劳不行", group="group", mentioned=False, mid="food-1"))
        await app._handle(make_ev("肚子疼", group="group", mentioned=False, mid="food-2"))
        for _ in range(200):
            if len(prompts) >= 1:
                break
            await asyncio.sleep(0.01)

        await app._handle(make_ev("你不要再吃那个了", group="group", mentioned=False, mid="food-3", sender="friend"))
        await app._handle(make_ev("好呢", group="group", mentioned=False, mid="food-4"))
        for _ in range(200):
            if len(prompts) >= 2:
                break
            await asyncio.sleep(0.01)
        await app.proactive.stop()

        assert len(prompts) == 2
        assert "最近群聊背景" in prompts[1]
        assert "我昨天晚上吃的麦当劳不行" in prompts[1]
        assert "你不要再吃那个了" in prompts[1]

    asyncio.run(go())


def test_recent_normal_group_reply_suppresses_proactive_reply() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.batch_seconds = 0.01
        cfg.proactive.min_messages = 3
        cfg.proactive.cooldown_seconds = 0
        cfg.proactive.quiet_after_bot_seconds = 60
        calls: list[str] = []

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            calls.append(prompt)
            if "最近聊天" in prompt:
                return '{"speak": true, "reply": "不该发送"}'
            return "普通回复"

        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("@1000000001 你好", group="group", mentioned=True, mid="normal-at"))
        await wait_until_sent(adapter, "在呢")
        for idx in range(3):
            await app._handle(
                make_ev(f"继续闲聊 {idx}", group="group", mentioned=False, mid=f"after-bot-{idx}")
            )
        await asyncio.sleep(0.05)
        await app.proactive.stop()

        assert calls == []
        assert not any("不该发送" in item[2] for item in adapter.sent)

    asyncio.run(go())


def test_private_plain_text_invokes_default_ask() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return f"{cmd}:{args}"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("你好"))
        await wait_until_sent(adapter, "ask:你好")

    asyncio.run(go())


def test_approve_schedules_result_reply() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return f"finished {cmd} {args}"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("/code edit file", sender="owner", mid="code-1"))
        jid, job = next(iter(app.policy.jobs.items()))  # type: ignore[union-attr]
        assert job.confirm_nonce is not None

        await app._handle(
            make_ev(f"/approve {jid} {job.confirm_nonce}", sender="owner", mid="approve-1")
        )

        await wait_until_sent(adapter, "approved")
        await wait_until_sent(adapter, "finished code edit file")

    asyncio.run(go())


def test_cancelled_reply_task_does_not_send_cancelled_message() -> None:
    async def go() -> None:
        release = asyncio.Event()
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            await release.wait()
            return "late reply"

        app = make_app(cfg, runner, adapter)
        await app._handle(make_ev("/ask hello"))
        assert len(app._reply_tasks) == 1
        reply_task = next(iter(app._reply_tasks))
        reply_task.cancel()
        await asyncio.gather(reply_task, return_exceptions=True)

        assert adapter.sent == []

        release.set()
        tasks = [job.task for job in app.policy.jobs.values() if job.task]  # type: ignore[union-attr]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(go())


def test_reply_chunks_wait_between_sends(monkeypatch: object) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.bot.reply_chunk_delay_seconds = 0.25  # type: ignore[attr-defined]
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("qq_agent_bridge.main.asyncio.sleep", fake_sleep)  # type: ignore[attr-defined]

        async def done() -> str:
            return "x" * 1900

        job = Job(
            id="chunk-delay-job",
            cmd="ask",
            args="long",
            event=make_ev("/ask long", group="group", mid="chunk-delay"),
            task=asyncio.create_task(done()),
        )

        await app._reply_when_done(job)

        assert len(adapter.sent) == 3
        assert sleeps == [0.25, 0.25]
        assert [item[3] for item in adapter.sent] == ["chunk-delay-0", "chunk-delay-1", "chunk-delay-2"]

    asyncio.run(go())


def test_status_path_runs_cleanup_for_seen_messages() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.max_seen_messages = 1

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("/help", mid="help-1"))
        await app._handle(make_ev("/status", mid="status-1"))

        assert len(app.policy.seen) == 1  # type: ignore[union-attr]
        assert "status-1" in app.policy.seen  # type: ignore[union-attr]

    asyncio.run(go())


def test_help_lists_reset_command() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("/help", sender="owner", mid="help-reset"))

        assert any("/reset" in item[2] for item in adapter.sent)

    asyncio.run(go())


def test_owner_reload_reloads_config_file(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.cooldown_seconds = 900
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": ["owner"],
                    "allowed_users": ["reader"],
                    "allowed_groups": ["group"],
                    "workspaces": {"/tmp": True},
                    "commands": {
                        "ask": True,
                        "status": True,
                        "help": True,
                        "reload": True,
                    },
                    "agent": {
                        "default_workspace": "/tmp",
                        "env_runner": "",
                        "require_env": False,
                        "use_bwrap": False,
                        "chat_model": "auto",
                    },
                    "proactive": {
                        "enabled": True,
                        "batch_seconds": 4,
                        "min_messages": 2,
                        "cooldown_seconds": 16,
                        "quiet_after_bot_seconds": 16,
                        "max_per_hour": 180,
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)
        app.config_path = config_path

        await app._handle(make_ev("/reload", sender="owner", mid="reload-1"))

        assert any("配置已重载" in item[2] for item in adapter.sent)
        assert app.cfg.proactive.cooldown_seconds == 16
        assert app.cfg.proactive.batch_seconds == 4
        assert app.policy is not None
        assert app.policy.cfg is app.cfg
        assert app.cursor.cfg is app.cfg
        assert app.search.cfg is app.cfg
        assert app.resources.cfg is app.cfg
        assert app.proactive.cfg is app.cfg

    asyncio.run(go())


def test_non_owner_reload_is_denied() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("/reload", sender="reader", mid="reload-reader"))

        assert adapter.sent == [("reader", False, "[denied] owner-only", "reload-reader")]

    asyncio.run(go())


def test_private_user_can_update_own_profile(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": ["owner"],
                    "allowed_users": ["reader"],
                    "allowed_groups": ["group"],
                    "commands": {"profile": True},
                    "profiles": {"default": "默认", "groups": {}, "users": {}},
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)
        app.config_path = config_path

        await app._handle(make_ev("/profile set 你是私聊学习搭子", sender="reader", mid="profile-private-set"))

        loaded = BridgeConfig.load(config_path)
        assert loaded.profiles.users["reader"] == "你是私聊学习搭子"
        assert app.cfg.profiles.users["reader"] == "你是私聊学习搭子"
        assert adapter.sent == [("reader", False, "已更新你的私聊 profile", "profile-private-set")]

    asyncio.run(go())


def test_group_owner_can_update_group_profile(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": ["owner"],
                    "allowed_users": ["reader"],
                    "allowed_groups": ["group"],
                    "commands": {"profile": True},
                    "profiles": {"default": "", "groups": {}, "users": {}},
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)
        app.config_path = config_path

        await app._handle(
            make_ev("/profile set 你是这个群里的技术搭子", sender="owner", group="group", mid="profile-group-set")
        )

        loaded = BridgeConfig.load(config_path)
        assert loaded.profiles.groups["group"] == "你是这个群里的技术搭子"
        assert app.cfg.profiles.groups["group"] == "你是这个群里的技术搭子"
        assert adapter.sent == [("group", True, "已更新本群 profile", "profile-group-set")]

    asyncio.run(go())


def test_group_non_owner_cannot_update_group_profile(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "profiles:\n  default: old\n  groups: {}\n  users: {}\n",
            encoding="utf-8",
        )

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)
        app.config_path = config_path

        await app._handle(
            make_ev("/profile set 不该成功", sender="reader", group="group", mid="profile-group-denied")
        )

        loaded = BridgeConfig.load(config_path)
        assert loaded.profiles.groups == {}
        assert adapter.sent == [("group", True, "[denied] owner-only", "profile-group-denied")]

    asyncio.run(go())


def test_profile_view_and_clear_current_scope(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.profiles.users["reader"] = "你是私聊学习搭子"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": ["owner"],
                    "allowed_users": ["reader"],
                    "commands": {"profile": True},
                    "profiles": {
                        "default": "默认",
                        "groups": {},
                        "users": {"reader": "你是私聊学习搭子"},
                    },
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)
        app.config_path = config_path

        await app._handle(make_ev("/profile", sender="reader", mid="profile-view"))
        await app._handle(make_ev("/profile clear", sender="reader", mid="profile-clear"))

        loaded = BridgeConfig.load(config_path)
        assert "reader" not in loaded.profiles.users
        assert "当前 profile：\n你是私聊学习搭子" in adapter.sent[0][2]
        assert adapter.sent[1] == ("reader", False, "已清除你的私聊 profile，将使用默认 profile", "profile-clear")

    asyncio.run(go())


def test_help_is_role_aware_and_qq_friendly() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("/help", group="group", mid="help-reader"))
        await app._handle(make_ev("/help", sender="owner", group="group", mid="help-owner"))

        reader_help = adapter.sent[0][2]
        owner_help = adapter.sent[1][2]
        assert "群里 @我" in reader_help
        assert "/search" in reader_help
        assert "/reset" not in reader_help
        assert "/code" not in reader_help
        assert "/reset" in owner_help
        assert "/code" in owner_help

    asyncio.run(go())


def test_self_question_uses_local_reply_without_cursor() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        called = False

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            nonlocal called
            called = True
            return "should not be used"

        app = make_app(cfg, runner, adapter)

        await app._handle(make_ev("@123456 你是谁", group="group", mid="self-1"))

        assert not called
        assert len(adapter.sent) == 1
        reply = adapter.sent[0][2]
        assert "QQ" in reply
        assert "助手" in reply
        for forbidden in ("Cursor", "cursor", "NapCat", "OneBot", "/home/", "token"):
            assert forbidden not in reply

    asyncio.run(go())


def test_search_sends_progress_then_final_without_cursor() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            raise AssertionError("search must not call cursor")

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_search(query: str) -> str:
            assert query == "memory"
            return "src/x.py:1: memory match"

        app.search.search = fake_search  # type: ignore[attr-defined, method-assign]

        await app._handle(make_ev("/search memory", mid="search-1"))
        await wait_until_sent(adapter, "我搜一下")
        await wait_until_sent(adapter, "src/x.py:1")

        assert adapter.sent[0][2] == "收到，我搜一下。"
        assert adapter.sent[-1][2] == "src/x.py:1: memory match"

    asyncio.run(go())


def test_task_command_uses_task_model_and_task_prompt() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        calls: list[tuple[str, str | None, str]] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            calls.append((mode, model, prompt))
            return "整理好了"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(
            make_ev("@1000000001 /task 百度一下“张三”相关经历，整理好发给我", group="group", mid="task-1")
        )
        await wait_until_sent(adapter, "我处理一下")
        await wait_until_sent(adapter, "整理好了")

        assert calls
        mode, model, prompt = calls[0]
        assert mode == "task"
        assert model == "composer"
        assert "任务模式" in prompt
        assert "联网搜索" in prompt
        assert "来源" in prompt
        assert "可发送资源目录" in prompt
        assert "资源发送令牌" in prompt
        assert "张三" in prompt

    asyncio.run(go())


def test_task_prompt_uses_workspace_local_runtime_skill_references(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "整理好了"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("@1000000001 /task 查天气", group="group", mid="task-skill-refs"))
        await wait_until_sent(adapter, "整理好了")

        reference_base = "downloads/qq-agent-bridge/runtime-skills/qq-agent-runtime/references"
        assert prompts
        assert f"{reference_base}/weather.md" in prompts[0]
        assert "`skills/qq-agent-runtime/references/weather.md`" not in prompts[0]
        assert (tmp_path / reference_base / "weather.md").is_file()

    asyncio.run(go())


def test_task_progress_reporter_exists_before_cursor_starts() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        seen_reporter = False

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]

        async def fake_agent_runner(job: Any) -> str:
            nonlocal seen_reporter
            seen_reporter = job.id in app._progress_reporters
            return "done"

        app.policy = Policy(cfg, fake_agent_runner)

        await app._handle(make_ev("/task long", group="group", mid="progress-race"))
        await wait_until_sent(adapter, "done")

        assert seen_reporter

    asyncio.run(go())


def test_task_progress_directive_sends_intermediate_message() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            assert progress is not None
            await progress("已解析链接")
            await progress("已抽帧")
            return "最终结果"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task long", group="group", mid="long-progress"))
        await wait_until_sent(adapter, "已解析链接")
        await wait_until_sent(adapter, "最终结果")

        texts = [item[2] for item in adapter.sent]
        assert "QQBOT_PROGRESS" not in "\n".join(texts)
        assert texts.index("已解析链接") < texts.index("最终结果")

    asyncio.run(go())


def test_ask_does_not_pass_progress_callback() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        progress_values: list[Any] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            progress_values.append(progress)
            return "ask ok"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/ask hi", mid="ask-progress"))
        await wait_until_sent(adapter, "ask ok")

        assert progress_values == [None]

    asyncio.run(go())


def test_silent_task_sends_heartbeat_before_final_answer() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            await asyncio.sleep(1.2)
            return "最终结果"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task long", group="group", mid="heartbeat-1"))
        await wait_until_sent(adapter, "还在处理")
        await wait_until_sent(adapter, "最终结果")

    asyncio.run(go())


def test_stop_cancels_heartbeat_for_long_task() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        release = asyncio.Event()

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            await release.wait()
            return "done"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task long", group="group", mid="stop-heartbeat"))
        jid = next(iter(app.policy.jobs))
        await app._handle(make_ev(f"/stop {jid}", sender="owner", group="group", mid="stop-heartbeat-2"))
        await asyncio.sleep(1.2)

        assert not any("还在处理" in item[2] for item in adapter.sent)

    asyncio.run(go())


def test_implicit_ask_web_task_stays_ask_with_task_guidance_in_prompt() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        calls: list[tuple[str, str | None, str]] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            calls.append((mode, model, prompt))
            return "普通回复"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(
            make_ev("@1000000001 百度一下“张三”相关经历，整理好发给我", group="group", mid="ask-web-1")
        )
        await wait_until_sent(adapter, "普通回复")

        assert calls
        mode, model, prompt = calls[0]
        assert mode == "ask"
        assert model == "auto"
        assert "/task" in prompt
        assert "受阻" in prompt

    asyncio.run(go())


def test_explicit_ask_file_generation_stays_ask_with_task_guidance_in_prompt() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        calls: list[tuple[str, str | None, str]] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            calls.append((mode, model, prompt))
            return "普通回复"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/ask 整理成 excel 发给我", mid="ask-file-task"))
        await wait_until_sent(adapter, "普通回复")

        assert calls
        mode, _model, prompt = calls[0]
        assert mode == "ask"
        assert "/task" in prompt
        assert "生成文件" in prompt

    asyncio.run(go())


def test_search_progress_and_result_are_not_added_to_conversation_memory() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "第二轮回答"

        async def fake_search(query: str) -> str:
            return "最终搜索结果"

        app.cursor.run = fake_cursor  # type: ignore[method-assign]
        app.search.search = fake_search  # type: ignore[attr-defined, method-assign]

        await app._handle(make_ev("/search 第一轮", mid="search-mem-1"))
        await wait_until_sent(adapter, "最终搜索结果")
        await app._handle(make_ev("/ask 第二轮", mid="search-mem-2"))
        await wait_until_sent(adapter, "第二轮回答")

        assert len(prompts) == 1
        assert "我搜一下" not in prompts[0]
        assert "最终搜索结果" not in prompts[0]
        assert "第一轮" not in prompts[0]
        assert "用户消息：第二轮" in prompts[0]

    asyncio.run(go())


def test_plan_progress_is_not_added_to_conversation_memory() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return "最终计划"
            return "第二轮回答"

        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/plan 第一轮", mid="plan-progress-1"))
        await wait_until_sent(adapter, "我整理一下")
        await wait_until_sent(adapter, "最终计划")
        await app._handle(make_ev("/ask 第二轮", mid="plan-progress-2"))
        await wait_until_sent(adapter, "第二轮回答")

        assert "我整理一下" not in prompts[1]
        assert "助手: 最终计划" in prompts[1]

    asyncio.run(go())


def test_completed_ask_is_added_to_conversation_memory() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return f"reply {len(prompts)}"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/ask 第一轮", mid="mem-1"))
        await wait_until_sent(adapter, "reply 1")
        await app._handle(make_ev("/ask 第二轮", mid="mem-2"))
        await wait_until_sent(adapter, "reply 2")

        assert len(prompts) == 2
        assert "历史对话：" in prompts[1]
        assert "用户: 第一轮" in prompts[1]
        assert "助手: reply 1" in prompts[1]
        assert "用户消息：第二轮" in prompts[1]

    asyncio.run(go())


def test_unmentioned_group_text_is_used_as_ambient_context_for_ask() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.enabled = False
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "可以先看日志。"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("这个接口有点慢", group="group", mentioned=False, mid="ambient-1"))
        assert adapter.sent == []

        await app._handle(make_ev("@1000000001 怎么看", group="group", mid="ambient-2"))
        await wait_until_sent(adapter, "可以先看日志")

        assert "最近群聊背景：" in prompts[0]
        assert "reader: 这个接口有点慢" in prompts[0]
        assert "不是当前用户的直接请求" in prompts[0]

    asyncio.run(go())


def test_unmentioned_command_like_text_is_not_ambient_context() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.enabled = False
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "ok"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("@示例机器人 /task hello", group="group", mentioned=False, mid="ambient-cmd"))
        await app._handle(make_ev("@1000000001 继续", group="group", mid="ambient-cmd-ask"))
        await wait_until_sent(adapter, "ok")

        assert "最近群聊背景：" not in prompts[0]
        assert "/task hello" not in prompts[0]

    asyncio.run(go())


def test_task_uses_ambient_context_only_when_referencing_prior_chat() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.enabled = False
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return f"reply {len(prompts)}"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("今天讨论接口超时", group="group", mentioned=False, mid="task-amb-1"))
        await app._handle(make_ev("/task 生成一个报告", group="group", mid="task-amb-2"))
        await wait_until_sent(adapter, "reply 1")
        await app._handle(make_ev("/task 根据刚才聊天整理行动项", group="group", mid="task-amb-3"))
        await wait_until_sent(adapter, "reply 2")

        assert "最近群聊背景：" not in prompts[0]
        assert "今天讨论接口超时" in prompts[1]
        assert "最近群聊背景：" in prompts[1]

    asyncio.run(go())


def test_code_does_not_include_ambient_context() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.proactive.enabled = False
        cfg.dangerous_requires_confirm = False
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "done"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("刚才说要改 README", group="group", mentioned=False, mid="code-amb-1"))
        await app._handle(make_ev("/code 根据刚才聊天改 README", sender="owner", group="group", mid="code-amb-2"))
        await wait_until_sent(adapter, "done")

        assert "最近群聊背景：" not in prompts[0]
        assert "刚才说要改 README" not in prompts[0]

    asyncio.run(go())


def test_ask_and_plan_use_configured_cursor_models() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.agent.chat_model = "auto"
        cfg.agent.task_model = "composer"
        calls: list[tuple[str, str | None]] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            calls.append((mode, model))
            return f"{mode} ok"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/ask 普通聊天", mid="model-ask"))
        await wait_until_sent(adapter, "ask ok")
        await app._handle(make_ev("/plan 做个任务", mid="model-plan"))
        await wait_until_sent(adapter, "plan ok")

        assert calls == [("ask", "auto"), ("plan", "composer")]

    asyncio.run(go())


def test_attached_resources_are_passed_to_cursor_prompt_without_memory_pollution() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return f"reply {len(prompts)}"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_prepare(ev: ChatEvent) -> tuple[PreparedResource, ...]:
            if not ev.resources:
                return ()
            return (
                PreparedResource(
                    kind="image",
                    name="cat.jpg",
                    local_path="downloads/qq-agent-bridge/2026-06-28/ev-res-1/00-deadbeef.jpg",
                ),
                PreparedResource(
                    kind="url",
                    name="https://example.com/page",
                    url="https://example.com/page",
                ),
            )

        app.resources.prepare = fake_prepare  # type: ignore[attr-defined, method-assign]

        await app._handle(
            make_ev(
                "/ask 看看附件",
                mid="res-1",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg"),),
            )
        )
        await wait_until_sent(adapter, "reply 1")
        await app._handle(make_ev("/ask 下一轮", mid="res-2"))
        await wait_until_sent(adapter, "reply 2")

        assert "用户附带资源：" in prompts[0]
        assert "downloads/qq-agent-bridge/2026-06-28/ev-res-1/00-deadbeef.jpg" in prompts[0]
        assert "https://example.com/page" in prompts[0]
        assert "downloads/qq-agent-bridge/2026-06-28/ev-res-1/00-deadbeef.jpg" not in prompts[1]
        assert "https://qq.example/cat.jpg" not in prompts[1]
        assert "用户消息：下一轮" in prompts[1]

    asyncio.run(go())


def test_private_attachment_only_message_defaults_to_ask() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "看到了"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_prepare(ev: ChatEvent) -> tuple[PreparedResource, ...]:
            return (PreparedResource(kind="image", name="cat.jpg", local_path="downloads/cat.jpg"),)

        app.resources.prepare = fake_prepare  # type: ignore[attr-defined, method-assign]

        await app._handle(
            make_ev(
                "",
                mid="res-only-private",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg"),),
            )
        )
        await wait_until_sent(adapter, "看到了")

        assert len(prompts) == 1
        assert "downloads/cat.jpg" in prompts[0]

    asyncio.run(go())


def test_mentioned_group_attachment_only_message_defaults_to_ask() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "看到了"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_prepare(ev: ChatEvent) -> tuple[PreparedResource, ...]:
            return (PreparedResource(kind="image", name="cat.jpg", local_path="downloads/cat.jpg"),)

        app.resources.prepare = fake_prepare  # type: ignore[attr-defined, method-assign]

        await app._handle(
            make_ev(
                "@1000000001",
                group="group",
                mid="res-only-mentioned-group",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg"),),
            )
        )
        await wait_until_sent(adapter, "看到了")

        assert len(prompts) == 1
        assert "downloads/cat.jpg" in prompts[0]

    asyncio.run(go())


def test_unmentioned_group_attachment_only_message_is_ignored() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        called = False

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            nonlocal called
            called = True
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(
            make_ev(
                "",
                group="group",
                mentioned=False,
                mid="res-only-group",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg"),),
            )
        )

        assert not called
        assert adapter.sent == []

    asyncio.run(go())


def test_unmentioned_group_attachment_is_cached_for_next_sender_mention() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "看到了"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_prepare(ev: ChatEvent) -> tuple[PreparedResource, ...]:
            if ev.resources:
                return (PreparedResource(kind="image", name="cat.jpg", local_path="downloads/cached.jpg"),)
            return ()

        app.resources.prepare = fake_prepare  # type: ignore[attr-defined, method-assign]

        await app._handle(
            make_ev(
                "",
                group="group",
                mentioned=False,
                mid="cached-image",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg"),),
            )
        )
        assert adapter.sent == []

        await app._handle(
            make_ev("@1000000001 /task 分析刚才那张图", group="group", mid="use-cached-image")
        )
        await wait_until_sent(adapter, "看到了")

        assert len(prompts) == 1
        assert "用户附带资源：" in prompts[0]
        assert "downloads/cached.jpg" in prompts[0]

    asyncio.run(go())


def test_cached_group_attachment_is_not_shared_across_senders() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return "没看到附件"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_prepare(ev: ChatEvent) -> tuple[PreparedResource, ...]:
            if ev.resources:
                return (PreparedResource(kind="image", name="cat.jpg", local_path="downloads/cached.jpg"),)
            return ()

        app.resources.prepare = fake_prepare  # type: ignore[attr-defined, method-assign]

        await app._handle(
            make_ev(
                "",
                sender="reader",
                group="group",
                mentioned=False,
                mid="cached-other-image",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg"),),
            )
        )
        await app._handle(
            make_ev("@1000000001 看看刚才的图", sender="other", group="group", mid="other-use")
        )
        await wait_until_sent(adapter, "没看到附件")

        assert len(prompts) == 1
        assert "用户附带资源：" not in prompts[0]
        assert "downloads/cached.jpg" not in prompts[0]

    asyncio.run(go())


def test_cached_group_attachment_is_consumed_after_use() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return f"reply {len(prompts)}"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        async def fake_prepare(ev: ChatEvent) -> tuple[PreparedResource, ...]:
            if ev.resources:
                return (PreparedResource(kind="image", name="cat.jpg", local_path="downloads/cached.jpg"),)
            return ()

        app.resources.prepare = fake_prepare  # type: ignore[attr-defined, method-assign]

        await app._handle(
            make_ev(
                "",
                group="group",
                mentioned=False,
                mid="consume-cache",
                resources=(ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg"),),
            )
        )
        await app._handle(make_ev("@1000000001 看看刚才的图", group="group", mid="use-cache-1"))
        await wait_until_sent(adapter, "reply 1")
        await app._handle(make_ev("@1000000001 再看一次刚才的图", group="group", mid="use-cache-2"))
        await wait_until_sent(adapter, "reply 2")

        assert "downloads/cached.jpg" in prompts[0]
        assert "downloads/cached.jpg" not in prompts[1]

    asyncio.run(go())


def test_unmentioned_group_command_is_silent() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        called = False

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            nonlocal called
            called = True
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(
            make_ev("@示例机器人 /task hello", group="group", mentioned=False, mid="no-at-command")
        )

        assert not called
        assert adapter.sent == []

    asyncio.run(go())


def test_unmentioned_disallowed_group_command_is_silent() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        called = False

        async def runner(cmd: str, args: str, ev: ChatEvent) -> str:
            nonlocal called
            called = True
            return "unused"

        app = make_app(cfg, runner, adapter)

        await app._handle(
            make_ev("/task hello", group="not-allowed", mentioned=False, mid="no-at-disallowed-group")
        )

        assert not called
        assert adapter.sent == []

    asyncio.run(go())


def test_reset_clears_current_conversation_memory() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        prompts: list[str] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            return f"reply {len(prompts)}"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("ambient 背景", group="group", mentioned=False, mid="reset-ambient"))
        await app._handle(make_ev("/ask 第一轮", group="group", mid="reset-1"))
        await wait_until_sent(adapter, "reply 1")
        await app._handle(make_ev("/reset", sender="owner", group="group", mid="reset-2"))
        await wait_until_sent(adapter, "已清空当前会话记忆和最近群聊背景")
        await app._handle(make_ev("/ask 第二轮", group="group", mid="reset-3"))
        await wait_until_sent(adapter, "reply 2")

        assert len(prompts) == 2
        assert "历史对话：" not in prompts[1]
        assert "第一轮" not in prompts[1]
        assert "ambient 背景" not in prompts[1]

    asyncio.run(go())


def test_cursor_output_can_send_image_and_file_resources(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            outbox_rel, token = extract_outgoing_prompt_context(prompt)
            outbox = Path(workspace or cfg.agent.default_workspace) / outbox_rel
            image = outbox / "plot.png"
            report = outbox / "report.pdf"
            image.write_bytes(b"png")
            report.write_bytes(b"pdf")
            return (
                "生成好了\n"
                f"QQBOT_SEND_IMAGE: {token} {outbox_rel}/plot.png\n"
                f"QQBOT_SEND_FILE: {token} {outbox_rel}/report.pdf\n"
            )

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task 生成图和报告", group="group", mid="send-res-1"))
        await wait_until_sent(adapter, "生成好了")

        assert len(adapter.sent_images) == 1
        assert len(adapter.sent_files) == 1
        assert adapter.sent_images[0][:2] == ("group", True)
        assert adapter.sent_images[0][2].read_bytes() == b"png"
        assert "sending" in adapter.sent_images[0][2].parts
        assert adapter.sent_images[0][3] == "send-res-1-r0"
        assert adapter.sent_files[0][:2] == ("group", True)
        assert adapter.sent_files[0][2].read_bytes() == b"pdf"
        assert "sending" in adapter.sent_files[0][2].parts
        assert adapter.sent_files[0][3] == "send-res-1-r1"
        assert all("QQBOT_SEND" not in sent[2] for sent in adapter.sent)

    asyncio.run(go())


def test_cursor_output_can_send_human_voice_resource(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            outbox_rel, token = extract_outgoing_prompt_context(prompt)
            voice = Path(workspace or cfg.agent.default_workspace) / outbox_rel / "reply.wav"
            write_wav(voice, 2)
            return f"QQBOT_SEND_VOICE: {token} {outbox_rel}/reply.wav duration=12\n"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task 生成一段人声发给我", group="group", mid="send-voice"))
        for _ in range(20):
            if adapter.sent_voices:
                break
            await asyncio.sleep(0.01)

        assert len(adapter.sent_voices) == 1
        chat_id, is_group, sent_path, echo = adapter.sent_voices[0]
        assert (chat_id, is_group, echo) == ("group", True, "send-voice-r0")
        assert sent_path.read_bytes().startswith(b"RIFF")
        assert "sending" in sent_path.parts
        assert adapter.sent_files == []

    asyncio.run(go())


def test_cursor_output_can_send_only_resources_without_empty_text(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            outbox_rel, token = extract_outgoing_prompt_context(prompt)
            image = Path(workspace or cfg.agent.default_workspace) / outbox_rel / "plot.png"
            image.write_bytes(b"png")
            return f"QQBOT_SEND_IMAGE: {token} {outbox_rel}/plot.png\n"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task 生成图", group="group", mid="send-res-only"))
        for _ in range(20):
            if adapter.sent_images:
                break
            await asyncio.sleep(0.01)

        assert adapter.sent == [("group", True, "收到，我处理一下。", "send-res-only-progress")]
        assert all("[empty]" not in sent[2] for sent in adapter.sent)
        assert len(adapter.sent_images) == 1
        assert adapter.sent_images[0][:2] == ("group", True)
        assert adapter.sent_images[0][2].read_bytes() == b"png"
        assert adapter.sent_images[0][3] == "send-res-only-r0"

    asyncio.run(go())


def test_cursor_output_rejects_resource_paths_outside_workspace(tmp_path: Path) -> None:
    async def go() -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("nope", encoding="utf-8")
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            _outbox_rel, token = extract_outgoing_prompt_context(prompt)
            return f"QQBOT_SEND_FILE: {token} {outside}\n"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task 发文件", group="group", mid="send-res-2"))
        await wait_until_sent(adapter, "已拒绝发送")

        assert adapter.sent_files == []
        assert adapter.sent_images == []

    asyncio.run(go())


def test_search_output_does_not_trigger_resource_sending(tmp_path: Path) -> None:
    async def go() -> None:
        outbox = tmp_path / "downloads" / "qq-agent-bridge" / "outgoing" / "job"
        outbox.mkdir(parents=True)
        report = outbox / "report.pdf"
        report.write_bytes(b"pdf")
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)

        async def fake_search(query: str) -> str:
            return f"QQBOT_SEND_FILE: any-token {report.relative_to(tmp_path)}"

        app.search.search = fake_search  # type: ignore[method-assign]

        await app._handle(make_ev("/search token", group="group", mid="search-send"))
        await wait_until_sent(adapter, "QQBOT_SEND_FILE")

        assert adapter.sent_files == []
        assert adapter.sent_images == []

    asyncio.run(go())
