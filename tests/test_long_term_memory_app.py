"""Application lifecycle tests for scoped long-term memory."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any

import yaml
import pytest

import qq_agent_bridge.main as main_module
from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.long_term_memory import LongTermMemoryStore
from qq_agent_bridge.long_term_memory_models import MemoryProposal, MemoryScope
from qq_agent_bridge.main import App
from qq_agent_bridge.memory_review import CuratorOutcome
from qq_agent_bridge.memory_curation import RejectedProposal
from qq_agent_bridge.policy import Job, Policy
from qq_agent_bridge.types import ChatEvent, ChatReply


class FakeAdapter:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order
        self.sent: list[tuple[str, bool, str, str | None]] = []
        self.started = asyncio.Event()

    async def start(self, _handler: Any) -> None:
        if self.order is not None:
            self.order.append("onebot-start")
        self.started.set()

    async def stop(self) -> None:
        if self.order is not None:
            self.order.append("onebot-stop")

    async def send(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        del reply_to
        self.sent.append((chat_id, is_group, text, echo))


class FakeCoordinator:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order
        self.started = False
        self.stopped = False
        self.cancel_count = 0
        self.notifications: list[MemoryScope] = []
        self.reloads: list[Any] = []
        self.review_started = asyncio.Event()
        self.review_release = asyncio.Event()
        self.review_outcome = CuratorOutcome(
            accepted=(
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="owner",
                    category="preference",
                    content="喜欢简短回答",
                    confidence=0.9,
                ),
                MemoryProposal(
                    operation="reinforce",
                    item_id="item-1",
                    confidence=0.9,
                ),
                MemoryProposal(
                    operation="mark_candidate",
                    subject_kind="user",
                    subject_id="owner",
                    category="preference",
                    content="也许喜欢蓝色",
                    confidence=0.4,
                    status="candidate",
                ),
            ),
            rejected=(
                RejectedProposal(
                    proposal=MemoryProposal.add(
                        subject_kind="user",
                        subject_id="owner",
                        category="preference",
                        content="不可靠内容",
                        confidence=0.2,
                    ),
                    reason="low_confidence",
                    index=3,
                ),
            ),
            proposed_count=4,
            source_count=2,
        )

    async def start(self) -> None:
        self.started = True
        if self.order is not None:
            self.order.append("memory-start")

    async def stop(self) -> None:
        self.stopped = True
        if self.order is not None:
            self.order.append("memory-stop")

    def notify(self, scope: MemoryScope) -> None:
        self.notifications.append(scope)

    def reload(self, cfg: Any) -> None:
        self.reloads.append(cfg)

    def cancel_background_for_interactive(self) -> None:
        self.cancel_count += 1

    async def review_now(self, scope: MemoryScope, actor: Any) -> CuratorOutcome:
        del scope, actor
        self.review_started.set()
        await self.review_release.wait()
        return self.review_outcome


def config(tmp_path: Path) -> BridgeConfig:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["owner", "member"],
        allowed_groups=["group"],
        commands={
            "ask": "user",
            "task": "user",
            "help": "user",
            "memory": "user",
            "reset": "owner",
            "reload": "owner",
        },
        workspaces={str(tmp_path): True},
    )
    cfg.agent.default_workspace = str(tmp_path)
    cfg.storage_maintenance.enabled = False
    cfg.scheduler.enabled = False
    cfg.proactive.enabled = False
    cfg.resources.enabled = False
    cfg.long_term_memory.enabled = True
    cfg.long_term_memory.default_scope_enabled = False
    cfg.long_term_memory.database_path = "data/memory.sqlite3"
    return cfg


def event(
    text: str,
    *,
    sender: str = "member",
    group: str | None = "group",
    mentioned: bool = True,
    mid: str = "m1",
    reply: ChatReply | None = None,
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=group or sender,
        sender_id=sender,
        is_group=group is not None,
        mentioned_bot=mentioned,
        text=text,
        timestamp=10,
        reply=reply,
    )


def install_fake_memory_builders(
    monkeypatch: Any,
    coordinator: FakeCoordinator,
) -> None:
    monkeypatch.setattr(
        main_module,
        "build_memory_review_coordinator",
        lambda _cfg, _store, _gate, _workspace: coordinator,
    )

    async def interpret(_prompt: str) -> str:
        return '{"intent":"status"}'

    monkeypatch.setattr(
        main_module,
        "build_memory_command_interpreter",
        lambda _cfg, _gate, _workspace: interpret,
    )


def test_initialization_applies_only_explicit_scope_overrides_and_protects_db(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        cfg.long_term_memory.users = {"member": True}
        database = tmp_path / "data" / "memory.sqlite3"
        previous = LongTermMemoryStore(database)
        previous.initialize()
        previous.set_scope_enabled(MemoryScope("group", "persisted"), True)
        previous.close()
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")

        await app._initialize_long_term_memory()

        assert app.long_term_memory_database_path == database
        assert app.long_term_memory_store is not None
        assert app.long_term_memory_store.is_scope_enabled(MemoryScope("group", "group"))
        assert app.long_term_memory_store.is_scope_enabled(MemoryScope("private", "member"))
        assert app.long_term_memory_store.is_scope_enabled(MemoryScope("group", "persisted"))
        assert not app.long_term_memory_store.is_scope_enabled(MemoryScope("group", "absent"))
        assert coordinator.started
        for path in (database.parent, database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            assert app.storage_maintainer.is_protected(path)
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_disabled_global_memory_is_a_noop(tmp_path: Path) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.enabled = False
        app = App(cfg, config_path=tmp_path / "config.yaml")

        await app._initialize_long_term_memory()

        assert app.long_term_memory_store is None
        assert app.long_term_memory_collector is None
        assert app.memory_review_coordinator is None
        assert not (tmp_path / "data" / "memory.sqlite3").exists()

    asyncio.run(go())


def test_run_starts_memory_before_onebot_and_closes_it_on_shutdown(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        order: list[str] = []
        coordinator = FakeCoordinator(order)
        install_fake_memory_builders(monkeypatch, coordinator)
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        app = App(cfg, config_path=tmp_path / "config.yaml")
        adapter = FakeAdapter(order)
        app.adapter = adapter  # type: ignore[assignment]
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(adapter.started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(task, 1)

        assert order.index("memory-start") < order.index("onebot-start")
        assert order.index("onebot-stop") < order.index("memory-stop")
        assert coordinator.stopped
        assert app.long_term_memory_store is None

    asyncio.run(go())


def test_adapter_start_failure_cleans_initialized_memory_in_reverse_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        order: list[str] = []
        coordinator = FakeCoordinator(order)
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(config(tmp_path), config_path=tmp_path / "config.yaml")

        class FailingAdapter(FakeAdapter):
            async def start(self, _handler: Any) -> None:
                order.append("onebot-start")
                raise RuntimeError("injected adapter failure")

        app.adapter = FailingAdapter(order)  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="injected adapter failure"):
            await app.run()

        assert order.index("onebot-stop") < order.index("memory-stop")
        assert coordinator.stopped
        assert app.long_term_memory_store is None

    asyncio.run(go())


def test_scheduler_start_failure_cleans_adapter_and_memory_in_reverse_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        order: list[str] = []
        cfg = config(tmp_path)
        cfg.scheduler.enabled = True
        coordinator = FakeCoordinator(order)
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        app.adapter = FakeAdapter(order)  # type: ignore[assignment]

        class FailingScheduler:
            def initialize(self) -> None:
                order.append("scheduler-initialize")

            async def start(self) -> None:
                order.append("scheduler-start")
                raise RuntimeError("injected scheduler failure")

            async def stop(self) -> None:
                order.append("scheduler-stop")

        app.scheduler = FailingScheduler()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="injected scheduler failure"):
            await app.run()

        assert order.index("scheduler-stop") < order.index("onebot-stop")
        assert order.index("onebot-stop") < order.index("memory-stop")
        assert app.long_term_memory_store is None

    asyncio.run(go())


def test_cancellation_during_memory_start_closes_local_store_and_coordinator(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        entered = asyncio.Event()

        class BlockingCoordinator(FakeCoordinator):
            async def start(self) -> None:
                entered.set()
                await asyncio.Future()

        coordinator = BlockingCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(config(tmp_path), config_path=tmp_path / "config.yaml")
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(entered.wait(), 1)

        task.cancel()
        await asyncio.wait_for(task, 1)

        assert coordinator.stopped
        assert app.long_term_memory_store is None
        reopened = LongTermMemoryStore(tmp_path / "data" / "memory.sqlite3")
        reopened.initialize()
        reopened.close()

    asyncio.run(go())


def test_cancellation_during_memory_start_bounds_resistant_coordinator_stop(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        start_entered = asyncio.Event()
        stop_cancelled = asyncio.Event()
        release = asyncio.Event()

        class ResistantStartupCoordinator(FakeCoordinator):
            async def start(self) -> None:
                start_entered.set()
                await asyncio.Future()

            async def stop(self) -> None:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    stop_cancelled.set()
                    await release.wait()

        coordinator = ResistantStartupCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        monkeypatch.setattr(main_module, "MEMORY_REVIEW_SHUTDOWN_GRACE_SECONDS", 0.01)
        app = App(config(tmp_path), config_path=tmp_path / "config.yaml")
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(start_entered.wait(), 1)

        task.cancel()
        await asyncio.sleep(0.05)
        returned_before_release = task.done()
        release.set()
        await asyncio.wait_for(task, 1)

        assert stop_cancelled.is_set()
        assert returned_before_release
        assert app.long_term_memory_store is None

    asyncio.run(go())


def test_database_failure_disables_only_long_term_memory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        adapter = FakeAdapter()
        app.adapter = adapter  # type: ignore[assignment]

        def fail(_self: LongTermMemoryStore) -> None:
            raise sqlite3.DatabaseError("damaged content that must not be logged")

        monkeypatch.setattr(LongTermMemoryStore, "initialize", fail)
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(adapter.started.wait(), 1)
        assert app.long_term_memory_store is None
        assert app.long_term_memory_error == "DatabaseError"
        await app._handle(event("/memory status"))
        assert "不可用" in adapter.sent[-1][2]
        task.cancel()
        await asyncio.wait_for(task, 1)

    asyncio.run(go())


def test_enabled_group_and_private_events_collect_without_waiting_for_review(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        cfg.long_term_memory.users = {"member": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        app.adapter = FakeAdapter()  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()

        await asyncio.wait_for(
            app._handle(event("群里普通聊天", mentioned=False, mid="group-message")),
            0.2,
        )
        await asyncio.wait_for(
            app._handle(event("私聊普通聊天", group=None, mid="private-message")),
            0.2,
        )

        assert app.long_term_memory_store is not None
        group_sources = app.long_term_memory_store.pending_sources(
            MemoryScope("group", "group"), 10
        )
        private_sources = app.long_term_memory_store.pending_sources(
            MemoryScope("private", "member"), 10
        )
        assert [source.text for source in group_sources] == ["群里普通聊天"]
        assert [source.text for source in private_sources] == ["私聊普通聊天"]
        assert coordinator.notifications == [
            MemoryScope("group", "group"),
            MemoryScope("private", "member"),
        ]
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_unmentioned_slash_commands_are_never_collected(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        app.adapter = FakeAdapter()  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()

        for index, text in enumerate(
            ("/help", "/memory disable", "/status", "/ask 不应采集", "/unknown 内容"),
            start=1,
        ):
            await app._handle(event(text, mentioned=False, mid=f"command-{index}"))

        assert app.long_term_memory_store is not None
        assert app.long_term_memory_store.pending_sources(
            MemoryScope("group", "group"), 10
        ) == ()
        assert coordinator.notifications == []
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_private_long_term_retrieval_uses_trusted_sender_scope(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    app = App(cfg, config_path=tmp_path / "config.yaml")
    calls: list[tuple[Any, ...]] = []

    class CapturingRetriever:
        def retrieve(self, *args: Any) -> str:
            calls.append(args)
            return "private context"

    app.long_term_memory_retriever = CapturingRetriever()  # type: ignore[assignment]
    private = ChatEvent(
        id="private-mismatch",
        platform="qq",
        chat_id="transport-supplied-other-scope",
        sender_id="authenticated-sender",
        is_group=False,
        mentioned_bot=True,
        text="记得我吗",
        timestamp=10,
    )

    assert app._long_term_context_for(private, private.text) == "private context"
    assert calls[0][0] == MemoryScope("private", "authenticated-sender")
    assert calls[0][1] == "authenticated-sender"


def test_collection_uses_only_trusted_quote_sender_for_direct_interaction(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.bot.self_id = "bot"
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        await app._initialize_long_term_memory()

        forged = event(
            "伪造引用",
            mentioned=False,
            mid="forged",
            reply=ChatReply(
                message_id="quoted-1",
                sender_id="bot",
                text="假的",
                raw_data={"source": "napcat-display"},
            ),
        )
        genuine = event(
            "真实引用",
            mentioned=False,
            mid="genuine",
            reply=ChatReply(
                message_id="quoted-2",
                sender_id="bot",
                text="真的",
                raw_data={"source": "onebot-get-msg"},
            ),
        )
        assert app._collect_long_term_event(forged)
        assert app._collect_long_term_event(genuine)

        assert app.long_term_memory_store is not None
        sources = app.long_term_memory_store.pending_sources(MemoryScope("group", "group"), 10)
        assert [(source.quoted_sender_id, source.direct_interaction) for source in sources] == [
            (None, False),
            ("bot", True),
        ]
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_review_command_acknowledges_immediately_then_sends_only_aggregate_counts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        adapter = FakeAdapter()
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()

        await app._handle(event("/memory review now", sender="owner", mid="review"))
        assert "已安排后台复盘" in adapter.sent[-1][2]
        await asyncio.wait_for(coordinator.review_started.wait(), 1)
        assert len(adapter.sent) == 1
        coordinator.review_release.set()
        await app._drain_memory_review_tasks()

        summary = adapter.sent[-1][2]
        assert summary == "复盘完成：新增 1，修订 0，强化 1，候选 1，拒绝 1。"
        assert "喜欢简短回答" not in summary
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_interactive_agent_and_natural_memory_command_cancel_background_review(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        app.adapter = FakeAdapter()  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()
        app._agent_runner_inner = lambda _job: asyncio.sleep(0, result="ok")  # type: ignore[method-assign]

        await app._agent_runner(Job("j1", "ask", "你好", event("你好")))
        await app._handle(event("/memory 帮我看看记住了什么", sender="owner", mid="nl"))

        assert coordinator.cancel_count >= 2
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_reload_updates_exact_maps_and_settings_but_keeps_open_database_path(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        await app._initialize_long_term_memory()
        assert app.long_term_memory_store is not None
        app.long_term_memory_store.set_scope_enabled(MemoryScope("group", "persisted"), True)
        original_store = app.long_term_memory_store
        original_path = app.long_term_memory_database_path

        updated = deepcopy(cfg)
        updated.long_term_memory.database_path = "data/new-memory.sqlite3"
        updated.long_term_memory.groups = {"group": False, "new-group": True}
        updated.long_term_memory.retrieval.max_items = 3
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": updated.owners,
                    "allowed_users": updated.allowed_users,
                    "allowed_groups": updated.allowed_groups,
                    "workspaces": updated.workspaces,
                    "commands": updated.commands,
                    "agent": {"default_workspace": updated.agent.default_workspace},
                    "storage_maintenance": {"enabled": False},
                    "long_term_memory": {
                        "enabled": True,
                        "default_scope_enabled": False,
                        "database_path": updated.long_term_memory.database_path,
                        "groups": updated.long_term_memory.groups,
                        "users": {},
                        "retrieval": {"max_items": 3},
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        ok, message = await app._reload_config()

        assert ok
        assert "long_term_memory.database_path 变更需要重启" in message
        assert app.long_term_memory_store is original_store
        assert app.long_term_memory_database_path == original_path
        assert not original_store.is_scope_enabled(MemoryScope("group", "group"))
        assert original_store.is_scope_enabled(MemoryScope("group", "new-group"))
        assert original_store.is_scope_enabled(MemoryScope("group", "persisted"))
        assert app.long_term_memory_retriever is not None
        assert app.long_term_memory_retriever.cfg.max_items == 3
        assert app.long_term_memory_collector is not None
        assert app.long_term_memory_collector.cfg is app.cfg
        assert app.memory_commands is not None and app.memory_commands.cfg is app.cfg
        assert coordinator.reloads == [app.cfg]
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_reload_scope_failure_keeps_old_config_runtime_and_persistent_choices(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        await app._initialize_long_term_memory()
        assert app.long_term_memory_store is not None
        store = app.long_term_memory_store
        store._conn.execute(
            """
            CREATE TRIGGER reject_scope BEFORE INSERT ON memory_scopes
            WHEN NEW.scope_id = 'reject-me'
            BEGIN
                SELECT RAISE(ABORT, 'injected scope failure');
            END
            """
        )
        old_cfg = app.cfg
        old_collector = app.long_term_memory_collector
        old_retriever = app.long_term_memory_retriever
        old_commands = app.memory_commands
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "storage_maintenance": {"enabled": False},
                    "long_term_memory": {
                        "enabled": True,
                        "default_scope_enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                        "groups": {"group": False, "reject-me": True},
                        "users": {},
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        ok, message = await app._reload_config()

        assert not ok
        assert message.startswith("[error] 配置重载失败：")
        assert app.cfg is old_cfg
        assert app.long_term_memory_collector is old_collector
        assert app.long_term_memory_retriever is old_retriever
        assert app.memory_commands is old_commands
        assert store.default_scope_enabled is False
        assert store.is_scope_enabled(MemoryScope("group", "group"))
        assert not store.is_scope_enabled(MemoryScope("group", "reject-me"))
        assert coordinator.reloads == []
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_reload_coordinator_failure_rolls_back_scope_choices_and_runtime(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        coordinator.current_cfg = cfg  # type: ignore[attr-defined]
        install_fake_memory_builders(monkeypatch, coordinator)
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        await app._initialize_long_term_memory()
        assert app.long_term_memory_store is not None
        store = app.long_term_memory_store
        old_cfg = app.cfg
        old_collector = app.long_term_memory_collector
        reload_calls: list[Any] = []

        def fail_new_runtime(candidate: Any) -> None:
            reload_calls.append(candidate)
            coordinator.current_cfg = candidate  # type: ignore[attr-defined]
            if candidate is not old_cfg:
                raise RuntimeError("injected coordinator failure")

        coordinator.reload = fail_new_runtime  # type: ignore[method-assign]
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "storage_maintenance": {"enabled": False},
                    "long_term_memory": {
                        "enabled": True,
                        "default_scope_enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                        "groups": {"group": False, "new-group": True},
                        "users": {},
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        ok, _message = await app._reload_config()

        assert not ok
        assert app.cfg is old_cfg
        assert app.long_term_memory_collector is old_collector
        assert coordinator.current_cfg is old_cfg  # type: ignore[attr-defined]
        assert reload_calls[-1] is old_cfg
        assert store.default_scope_enabled is False
        assert store.is_scope_enabled(MemoryScope("group", "group"))
        assert not store.is_scope_enabled(MemoryScope("group", "new-group"))
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_reload_scheduler_start_failure_rolls_back_entire_runtime_and_memory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        await app._initialize_long_term_memory()
        assert app.long_term_memory_store is not None
        store = app.long_term_memory_store

        class FailingScheduler:
            def __init__(self) -> None:
                self.cfg = cfg.scheduler
                self.running = False

            def reload_config(self, candidate: Any) -> None:
                self.cfg = candidate

            async def start(self) -> None:
                if self.cfg.enabled:
                    raise RuntimeError("injected scheduler start failure")
                self.running = True

            async def stop(self) -> None:
                self.running = False

        scheduler = FailingScheduler()
        app.scheduler = scheduler  # type: ignore[assignment]
        old_cfg = app.cfg
        old_agent = app.agent
        old_collector = app.long_term_memory_collector
        old_proactive = app.proactive
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "scheduler": {"enabled": True},
                    "storage_maintenance": {"enabled": False},
                    "long_term_memory": {
                        "enabled": True,
                        "default_scope_enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                        "groups": {"group": False, "new-group": True},
                        "users": {},
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        ok, message = await app._reload_config()

        assert not ok and message.startswith("[error] 配置重载失败：")
        assert app.cfg is old_cfg
        assert app.agent is old_agent
        assert app.long_term_memory_collector is old_collector
        assert app.proactive is old_proactive
        assert app.storage_maintainer.cfg is old_cfg
        assert scheduler.cfg is old_cfg.scheduler
        assert not scheduler.running
        assert store.default_scope_enabled is False
        assert store.is_scope_enabled(MemoryScope("group", "group"))
        assert not store.is_scope_enabled(MemoryScope("group", "new-group"))
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_reload_proactive_stop_failure_rolls_back_scheduler_and_memory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        await app._initialize_long_term_memory()
        assert app.long_term_memory_store is not None
        store = app.long_term_memory_store
        old_cfg = app.cfg
        old_collector = app.long_term_memory_collector
        old_proactive = app.proactive

        async def fail_stop() -> None:
            raise RuntimeError("injected proactive stop failure")

        old_proactive.stop = fail_stop  # type: ignore[method-assign]
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "scheduler": {"enabled": False},
                    "storage_maintenance": {"enabled": False},
                    "long_term_memory": {
                        "enabled": True,
                        "default_scope_enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                        "groups": {"group": False, "new-group": True},
                        "users": {},
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        ok, message = await app._reload_config()

        assert not ok and message.startswith("[error] 配置重载失败：")
        assert app.cfg is old_cfg
        assert app.long_term_memory_collector is old_collector
        assert app.proactive is old_proactive
        assert app.scheduler.cfg is old_cfg.scheduler
        assert store.default_scope_enabled is False
        assert store.is_scope_enabled(MemoryScope("group", "group"))
        assert not store.is_scope_enabled(MemoryScope("group", "new-group"))
        old_proactive.stop = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_reload_memory_failure_restores_stopped_proactive_batch_and_one_timer(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.proactive.enabled = True
        cfg.proactive.allowed_groups = ["group"]
        cfg.proactive.batch_seconds = 600
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        old_cfg = app.cfg
        old_proactive = app.proactive
        pending = event("pending chat", mentioned=False, mid="pending-proactive")
        old_proactive.observe(pending)
        original_timer = old_proactive._timers["group"]  # noqa: SLF001

        async def fail_memory(_cfg: BridgeConfig) -> tuple[bool, str]:
            return False, "injected memory failure"

        app._reload_long_term_memory = fail_memory  # type: ignore[method-assign]
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "scheduler": {"enabled": False},
                    "storage_maintenance": {"enabled": False},
                    "proactive": {
                        "enabled": True,
                        "allowed_groups": ["group"],
                        "batch_seconds": 600,
                    },
                    "long_term_memory": {
                        "enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        ok, message = await app._reload_config()

        assert not ok and message.startswith("[error] 配置重载失败：")
        assert app.cfg is old_cfg
        assert app.proactive is old_proactive
        assert original_timer.cancelled()
        assert "group" in old_proactive._batches  # noqa: SLF001
        restored = tuple(old_proactive._timers.values())  # noqa: SLF001
        assert len(restored) == 1
        assert restored[0] is not original_timer
        assert not restored[0].done()
        await old_proactive.stop()

    asyncio.run(go())


@pytest.mark.parametrize(
    "transition",
    ("scheduler-start", "scheduler-stop", "proactive-stop"),
)
def test_reload_cancellation_rolls_back_each_awaited_transition_before_reraise(
    tmp_path: Path,
    transition: str,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.scheduler.enabled = transition == "scheduler-stop"
        cfg.proactive.enabled = True
        cfg.proactive.allowed_groups = ["group"]
        cfg.proactive.batch_seconds = 600
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        old_cfg = app.cfg
        old_proactive = app.proactive
        old_proactive.observe(event("pending chat", mentioned=False, mid="cancel-pending"))
        entered = asyncio.Event()

        class CancellableScheduler:
            def __init__(self) -> None:
                self.cfg = cfg.scheduler
                self.running = transition == "scheduler-stop"
                self._suspend_start = transition == "scheduler-start"
                self._suspend_stop = transition == "scheduler-stop"

            def reload_config(self, candidate: Any) -> None:
                self.cfg = candidate

            async def start(self) -> None:
                self.running = True
                if self._suspend_start:
                    self._suspend_start = False
                    entered.set()
                    await asyncio.Future()

            async def stop(self) -> None:
                self.running = False
                if self._suspend_stop:
                    self._suspend_stop = False
                    entered.set()
                    await asyncio.Future()

        scheduler = CancellableScheduler()
        app.scheduler = scheduler  # type: ignore[assignment]
        original_proactive_stop = old_proactive.stop
        if transition == "proactive-stop":

            async def cancellable_proactive_stop() -> None:
                await original_proactive_stop()
                entered.set()
                await asyncio.Future()

            old_proactive.stop = cancellable_proactive_stop  # type: ignore[method-assign]

        new_scheduler_enabled = transition == "scheduler-start"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "scheduler": {"enabled": new_scheduler_enabled},
                    "storage_maintenance": {"enabled": False},
                    "proactive": {
                        "enabled": True,
                        "allowed_groups": ["group"],
                        "batch_seconds": 600,
                    },
                    "long_term_memory": {
                        "enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        reload_task = asyncio.create_task(app._reload_config())
        await asyncio.wait_for(entered.wait(), 1)
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        assert app.cfg is old_cfg
        assert app.proactive is old_proactive
        assert scheduler.cfg is old_cfg.scheduler
        assert scheduler.running is (transition == "scheduler-stop")
        assert "group" in old_proactive._batches  # noqa: SLF001
        active_timers = tuple(
            timer
            for timer in old_proactive._timers.values()  # noqa: SLF001
            if not timer.done()
        )
        assert len(active_timers) == 1
        old_proactive.stop = original_proactive_stop  # type: ignore[method-assign]
        await old_proactive.stop()

    asyncio.run(go())


@pytest.mark.parametrize("transition", ("scheduler-start", "scheduler-stop"))
@pytest.mark.parametrize("memory_succeeds", (False, True))
def test_reload_handoff_routes_new_chat_arriving_during_scheduler_transition_once(
    tmp_path: Path,
    transition: str,
    memory_succeeds: bool,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.scheduler.enabled = transition == "scheduler-stop"
        cfg.proactive.enabled = True
        cfg.proactive.allowed_groups = ["group"]
        cfg.proactive.batch_seconds = 600
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        old_cfg = app.cfg
        old_proactive = app.proactive
        entered = asyncio.Event()
        release = asyncio.Event()

        class PausedScheduler:
            def __init__(self) -> None:
                self.cfg = cfg.scheduler
                self.running = transition == "scheduler-stop"
                self._pause_start = transition == "scheduler-start"
                self._pause_stop = transition == "scheduler-stop"

            def reload_config(self, candidate: Any) -> None:
                self.cfg = candidate

            async def start(self) -> None:
                self.running = True
                if self._pause_start:
                    self._pause_start = False
                    entered.set()
                    await release.wait()

            async def stop(self) -> None:
                self.running = False
                if self._pause_stop:
                    self._pause_stop = False
                    entered.set()
                    await release.wait()

        app.scheduler = PausedScheduler()  # type: ignore[assignment]

        async def memory_result(_cfg: BridgeConfig) -> tuple[bool, str]:
            if memory_succeeds:
                return True, ""
            return False, "injected memory failure"

        app._reload_long_term_memory = memory_result  # type: ignore[method-assign]
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "scheduler": {"enabled": transition == "scheduler-start"},
                    "storage_maintenance": {"enabled": False},
                    "proactive": {
                        "enabled": True,
                        "allowed_groups": ["group"],
                        "batch_seconds": 600,
                    },
                    "long_term_memory": {
                        "enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        reload_task = asyncio.create_task(app._reload_config())
        await asyncio.wait_for(entered.wait(), 1)
        late = event("late chat", mentioned=False, mid=f"late-{transition}")
        old_proactive.observe(late)
        release.set()
        ok, _message = await reload_task

        assert ok is memory_succeeds
        assert (app.cfg is old_cfg) is (not memory_succeeds)
        target = app.proactive
        assert (target is old_proactive) is (not memory_succeeds)
        assert "group" in target._batches  # noqa: SLF001
        target_timers = tuple(
            timer
            for timer in target._timers.values()  # noqa: SLF001
            if not timer.done()
        )
        assert len(target_timers) == 1
        if memory_succeeds:
            assert "group" not in old_proactive._batches  # noqa: SLF001
            assert not old_proactive._timers  # noqa: SLF001
        await target.stop()
        if target is not old_proactive:
            await old_proactive.stop()

    asyncio.run(go())


def test_reload_handoff_keeps_new_chat_arriving_while_proactive_stop_waits(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.proactive.enabled = True
        cfg.proactive.allowed_groups = ["group"]
        cfg.proactive.batch_seconds = 600
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        old_proactive = app.proactive
        timer_cancelled = asyncio.Event()
        release_timer = asyncio.Event()
        stop_started = asyncio.Event()

        async def resistant_timer() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                timer_cancelled.set()
                await release_timer.wait()

        blocker = asyncio.create_task(resistant_timer())
        old_proactive._timers["blocking"] = blocker  # noqa: SLF001
        original_stop = old_proactive.stop

        async def signalled_stop() -> None:
            stop_started.set()
            await original_stop()

        old_proactive.stop = signalled_stop  # type: ignore[method-assign]

        async def fail_memory(_cfg: BridgeConfig) -> tuple[bool, str]:
            return False, "injected memory failure"

        app._reload_long_term_memory = fail_memory  # type: ignore[method-assign]
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "scheduler": {"enabled": False},
                    "storage_maintenance": {"enabled": False},
                    "proactive": {
                        "enabled": True,
                        "allowed_groups": ["group"],
                        "batch_seconds": 600,
                    },
                    "long_term_memory": {
                        "enabled": True,
                        "database_path": cfg.long_term_memory.database_path,
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        reload_task = asyncio.create_task(app._reload_config())
        await asyncio.wait_for(stop_started.wait(), 1)
        await asyncio.wait_for(timer_cancelled.wait(), 1)
        old_proactive.observe(event("late during stop", mentioned=False, mid="late-stop"))
        release_timer.set()
        ok, _message = await reload_task

        assert not ok
        assert app.proactive is old_proactive
        assert "group" in old_proactive._batches  # noqa: SLF001
        active_timers = tuple(
            timer
            for timer in old_proactive._timers.values()  # noqa: SLF001
            if not timer.done()
        )
        assert len(active_timers) == 1
        old_proactive.stop = original_stop  # type: ignore[method-assign]
        await old_proactive.stop()

    asyncio.run(go())


def test_reload_global_disable_does_not_depend_on_interpreter_rebuild(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        config_path = tmp_path / "config.yaml"
        app = App(cfg, config_path=config_path)
        await app._initialize_long_term_memory()

        def forbidden_rebuild(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("disabled reload must not rebuild an interpreter")

        monkeypatch.setattr(
            main_module,
            "build_memory_command_interpreter",
            forbidden_rebuild,
        )
        config_path.write_text(
            yaml.safe_dump(
                {
                    "owners": cfg.owners,
                    "allowed_users": cfg.allowed_users,
                    "allowed_groups": cfg.allowed_groups,
                    "workspaces": cfg.workspaces,
                    "commands": cfg.commands,
                    "agent": {"default_workspace": cfg.agent.default_workspace},
                    "storage_maintenance": {"enabled": False},
                    "long_term_memory": {
                        "enabled": False,
                        "database_path": cfg.long_term_memory.database_path,
                    },
                }
            ),
            encoding="utf-8",
        )

        ok, message = await app._reload_config()

        assert ok and "长期记忆配置重载失败" not in message
        assert not app._long_term_memory_accepting
        assert app.long_term_memory_collector is not None
        assert not app.long_term_memory_collector.memory_cfg.enabled
        assert app.long_term_memory_retriever is not None
        assert not app.long_term_memory_retriever.enabled
        assert app.memory_commands is not None
        disabled = await app.memory_commands.handle(event("", sender="owner"), "随便看看")
        assert disabled.text.startswith("[disabled]")
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_disabled_scope_and_denied_commands_do_not_collect(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.commands["task"] = "disabled"
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        adapter = FakeAdapter()
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()

        await app._handle(event("普通消息", mentioned=False, mid="disabled-scope"))
        await app._handle(event("/task 不应采集", mid="denied-task"))

        assert app.long_term_memory_store is not None
        assert app.long_term_memory_store.pending_sources(MemoryScope("group", "group"), 10) == ()
        assert coordinator.notifications == []
        assert adapter.sent[-1][2].startswith("[denied]")
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_help_subcommand_is_not_collected_as_semantic_task(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        app.adapter = FakeAdapter()  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()

        await app._handle(event("/task help", mid="task-help"))

        assert app.long_term_memory_store is not None
        assert app.long_term_memory_store.pending_sources(MemoryScope("group", "group"), 10) == ()
        assert coordinator.notifications == []
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_scope_choice_persists_across_restart_when_config_omits_scope(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        first_coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, first_coordinator)
        first = App(cfg, config_path=tmp_path / "config.yaml")
        first.adapter = FakeAdapter()  # type: ignore[assignment]
        first.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await first._initialize_long_term_memory()
        await first._handle(event("/memory disable", sender="owner", mid="disable"))
        await first._shutdown_long_term_memory()

        restarted_cfg = config(tmp_path)
        restarted_cfg.long_term_memory.groups = {}
        second_coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, second_coordinator)
        second = App(restarted_cfg, config_path=tmp_path / "config.yaml")
        await second._initialize_long_term_memory()

        assert second.long_term_memory_store is not None
        assert not second.long_term_memory_store.is_scope_enabled(
            MemoryScope("group", "group")
        )
        await second._shutdown_long_term_memory()

    asyncio.run(go())


def test_reset_clears_recent_context_but_preserves_long_term_items(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        cfg = config(tmp_path)
        cfg.long_term_memory.groups = {"group": True}
        coordinator = FakeCoordinator()
        install_fake_memory_builders(monkeypatch, coordinator)
        app = App(cfg, config_path=tmp_path / "config.yaml")
        adapter = FakeAdapter()
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, lambda _job: asyncio.sleep(0, result="ok"))
        await app._initialize_long_term_memory()
        await app._handle(
            event("/memory remember 我喜欢简短回答", sender="owner", mid="remember")
        )

        await app._handle(event("/reset", sender="owner", mid="reset"))

        assert app.long_term_memory_store is not None
        items = app.long_term_memory_store.list_items(
            MemoryScope("group", "group"),
            subject_id="owner",
        )
        assert [item.content for item in items] == ["我喜欢简短回答"]
        assert "长期记忆不受影响" in adapter.sent[-1][2]
        await app._shutdown_long_term_memory()

    asyncio.run(go())


def test_shutdown_drain_is_bounded_for_cancellation_resistant_review_delivery(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        app = App(config(tmp_path), config_path=tmp_path / "config.yaml")
        release = asyncio.Event()
        cancellation_seen = asyncio.Event()

        async def resistant() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()

        task = asyncio.create_task(resistant())
        app._memory_review_tasks.add(task)
        task.add_done_callback(app._memory_review_tasks.discard)
        monkeypatch.setattr(main_module, "MEMORY_REVIEW_SHUTDOWN_GRACE_SECONDS", 0.01)

        drain = asyncio.create_task(app._drain_memory_review_tasks(cancel=True))
        await asyncio.sleep(0.05)
        returned_with_resistant_task = drain.done()
        release.set()
        await drain
        await task

        assert cancellation_seen.is_set()
        assert returned_with_resistant_task

    asyncio.run(go())


def test_memory_coordinator_shutdown_has_an_app_level_deadline(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    async def go() -> None:
        app = App(config(tmp_path), config_path=tmp_path / "config.yaml")
        release = asyncio.Event()
        cancellation_seen = asyncio.Event()

        class ResistantCoordinator:
            async def stop(self) -> None:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancellation_seen.set()
                    await release.wait()

        app.memory_review_coordinator = ResistantCoordinator()  # type: ignore[assignment]
        monkeypatch.setattr(main_module, "MEMORY_REVIEW_SHUTDOWN_GRACE_SECONDS", 0.01)

        shutdown = asyncio.create_task(app._shutdown_long_term_memory())
        await asyncio.sleep(0.05)
        returned_before_release = shutdown.done()
        release.set()
        await asyncio.wait_for(shutdown, 1)

        assert cancellation_seen.is_set()
        assert returned_before_release

    asyncio.run(go())
