from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.long_term_memory import LongTermMemoryStore
from qq_agent_bridge.long_term_memory_models import MemoryProposal, MemoryScope, MemorySource
from qq_agent_bridge.memory_curation import MemoryActor, MemoryValidator
from qq_agent_bridge.memory_review import (
    MAX_CURATOR_OUTPUT_CHARS,
    MAX_CURATOR_PROMPT_CHARS,
    MemoryCurator,
    MemoryReviewCoordinator,
    build_memory_review_coordinator,
    build_restricted_memory_agent,
)
from qq_agent_bridge.storage_gate import GatedAgentAdapter, StorageActivityGate


GROUP = MemoryScope("group", "g")
OWNER = MemoryActor("owner", "owner")


@dataclass
class AgentCall:
    prompt: str
    workspace: str | None
    mode: str
    kwargs: dict[str, Any]


class FakeAgent:
    def __init__(self, result: str = '{"operations": []}') -> None:
        self.result = result
        self.calls: list[AgentCall] = []
        self.release: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0

    async def run(
        self,
        prompt: str,
        workspace: str | None = None,
        mode: str = "ask",
        **kwargs: Any,
    ) -> str:
        self.calls.append(AgentCall(prompt, workspace, mode, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.release is not None:
                await self.release.wait()
            return self.result
        finally:
            self.active -= 1


@pytest.fixture
def cfg() -> BridgeConfig:
    result = BridgeConfig()
    result.long_term_memory.review.model = "auto"
    result.long_term_memory.review.timeout_seconds = 1
    return result


@pytest.fixture
def store(tmp_path: Path) -> LongTermMemoryStore:
    result = LongTermMemoryStore(tmp_path / "memory.sqlite3")
    result.initialize()
    result.set_scope_enabled(GROUP, True)
    yield result
    result.close()


def source(text: str = "SENSITIVE-SOURCE-TEXT") -> MemorySource:
    return MemorySource(
        scope=GROUP,
        message_id="m1",
        sender_id="u1",
        text=text,
        message_timestamp=100,
        direct_interaction=True,
    )


def collect_source(
    store: LongTermMemoryStore,
    *,
    message_id: str,
    text: str = "我喜欢简洁回答",
    created_at: int = 100,
    attempt_count: int = 0,
) -> int:
    source_id = store.collect(
        MemorySource(
            scope=GROUP,
            message_id=message_id,
            sender_id="u1",
            text=text,
            message_timestamp=created_at,
            direct_interaction=True,
            attempt_count=attempt_count,
            created_at=created_at,
        )
    )
    assert source_id is not None
    return source_id


def make_coordinator(
    store: LongTermMemoryStore,
    agent: FakeAgent,
    cfg: BridgeConfig,
    tmp_path: Path,
    *,
    now: int = 1_000,
) -> MemoryReviewCoordinator:
    curator = MemoryCurator(
        agent,
        MemoryValidator(cfg, store=store),
        cfg.long_term_memory.review,
        workspace=tmp_path,
    )
    return MemoryReviewCoordinator(
        store,
        curator,
        cfg.long_term_memory,
        StorageActivityGate(),
        clock=lambda: float(now),
        wall_clock=lambda: float(now),
    )


def test_curator_uses_bounded_json_only_ask_contract(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        agent = FakeAgent()
        curator = MemoryCurator(
            agent,
            MemoryValidator(cfg, store=store),
            cfg.long_term_memory.review,
            workspace=tmp_path,
        )

        outcome = await curator.review(GROUP, (source("x" * 20_000),), ())

        assert outcome.error is None
        assert outcome.accepted == ()
        assert len(agent.calls) == 1
        call = agent.calls[0]
        assert call.mode == "ask"
        assert call.workspace == str(tmp_path)
        assert call.kwargs["model"] == "auto"
        assert call.kwargs.get("progress") is None
        assert call.kwargs.get("trace_id") is None
        assert "x" * 20_000 not in call.kwargs["redact_extra"]
        assert "x" * 2_000 in call.kwargs["redact_extra"]
        assert len(call.prompt) <= MAX_CURATOR_PROMPT_CHARS
        assert "untrusted" in call.prompt.lower()
        assert "QQ_UNTRUSTED_DATA_JSON" in call.prompt
        assert '"operations"' in call.prompt
        assert "Return JSON only" in call.prompt
        assert "|forget" not in call.prompt
        assert "|merge|" not in call.prompt
        assert "Never propose hard deletion" in call.prompt

    asyncio.run(go())


def test_curator_timeout_and_oversized_output_are_mechanical_failures(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        slow = FakeAgent()
        slow.release = asyncio.Event()
        timeout_cfg = cfg.long_term_memory.review
        timeout_cfg.timeout_seconds = 0.01  # type: ignore[assignment]
        timed = MemoryCurator(
            slow,
            MemoryValidator(cfg, store=store),
            timeout_cfg,
            workspace=tmp_path,
        )
        timeout = await timed.review(GROUP, (source(),), ())
        assert timeout.error == "timeout"

        huge = FakeAgent("x" * (MAX_CURATOR_OUTPUT_CHARS + 1))
        bounded = MemoryCurator(
            huge,
            MemoryValidator(cfg, store=store),
            cfg.long_term_memory.review,
            workspace=tmp_path,
        )
        oversized = await bounded.review(GROUP, (source(),), ())
        assert oversized.error == "output_too_large"

    asyncio.run(go())


def test_curator_logs_only_metadata(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def go() -> None:
        agent = FakeAgent("not json SENSITIVE-SOURCE-TEXT")
        curator = MemoryCurator(
            agent,
            MemoryValidator(cfg, store=store),
            cfg.long_term_memory.review,
            workspace=tmp_path,
        )
        with caplog.at_level(logging.INFO, logger="qq_agent_bridge.memory_review"):
            outcome = await curator.review(GROUP, (source(),), ())
        assert outcome.error == "malformed_output"

    asyncio.run(go())
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "SENSITIVE-SOURCE-TEXT" not in rendered
    assert "not json" not in rendered
    assert "scope=g" not in rendered
    assert "malformed_output" in rendered


def test_production_builder_constructs_dedicated_restricted_agent_config(
    cfg: BridgeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(mode=0o700)
    auth_file = fake_home / ".config" / "cursor" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"token":"curator-auth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    project_workspace = tmp_path / "project"
    runtime_skill = project_workspace / "skills" / "qq-agent-runtime" / "SKILL.md"
    runtime_skill.parent.mkdir(parents=True)
    runtime_skill.write_text("project runtime skill", encoding="utf-8")
    cfg.agent.runtime = "custom-cli"
    cfg.agent.command = {"ask": ["agent", "{prompt}"]}
    cfg.agent.use_bwrap = False
    cfg.agent.share_network = True
    cfg.agent.force_task_tools = True
    cfg.agent.trace_enabled = True
    cfg.agent.max_runtime_seconds = 300
    cfg.agent.max_output_chars = 40_000
    normal_sandbox_home = fake_home / ".local" / "state" / "qq-agent-bridge" / "agent-home"
    cfg.agent.sandbox_home = str(normal_sandbox_home)
    cfg.workspaces = {"/writable/project": True}
    gate = StorageActivityGate()

    adapter = build_restricted_memory_agent(cfg, gate, project_workspace)

    assert isinstance(adapter, GatedAgentAdapter)
    restricted = adapter.cfg
    assert restricted is not cfg
    assert restricted.agent is not cfg.agent
    assert restricted.agent.use_bwrap is True
    assert restricted.agent.share_network is False
    assert restricted.agent.force_task_tools is False
    assert restricted.agent.hardened_read_only is True
    assert restricted.agent.log_subprocess_output is False
    assert restricted.agent.trace_enabled is False
    curator_workspace = Path(restricted.agent.default_workspace)
    curator_home = Path(restricted.agent.sandbox_home)
    state_root = fake_home / ".local" / "state" / "qq-agent-bridge"
    assert curator_workspace.parent == state_root
    assert curator_home.parent == state_root
    assert curator_workspace != project_workspace
    assert curator_home != normal_sandbox_home
    assert curator_home != curator_workspace
    assert tuple(curator_workspace.iterdir()) == ()
    assert tuple(curator_home.iterdir()) == ()
    assert curator_home.stat().st_mode & 0o777 == 0o700
    assert restricted.agent.max_runtime_seconds == cfg.long_term_memory.review.timeout_seconds
    assert restricted.agent.max_output_chars == MAX_CURATOR_OUTPUT_CHARS
    assert restricted.workspaces == {str(curator_workspace): True}
    assert restricted.resources.enabled is False
    assert cfg.agent.share_network is True
    assert cfg.agent.trace_enabled is True
    assert cfg.agent.sandbox_home == str(normal_sandbox_home)

    second = build_restricted_memory_agent(cfg, gate, project_workspace)
    assert second.cfg.agent.sandbox_home != restricted.agent.sandbox_home
    assert second.cfg.agent.default_workspace != restricted.agent.default_workspace

    cmd = adapter.delegate._build_cmd(  # noqa: SLF001 - inspect security boundary
        "curate memory",
        str(curator_workspace),
        "ask",
        model=cfg.long_term_memory.review.model,
    )
    rendered_cmd = "\0".join(cmd)
    assert "--unshare-net" in cmd
    assert "--share-net" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "enabled"
    assert _has_mount(
        cmd,
        "--ro-bind",
        str(curator_workspace),
        "/workspace",
    )
    assert not _has_mount(
        cmd,
        "--bind",
        str(curator_workspace),
        "/workspace",
    )
    assert str(project_workspace) not in rendered_cmd
    assert "qq-agent-runtime" not in rendered_cmd
    for forbidden in ("--force", "--trust", "--auto-review", "--approve-mcps"):
        assert forbidden not in cmd

    store = LongTermMemoryStore(tmp_path / "builder-memory.sqlite3")
    store.initialize()
    coordinator = build_memory_review_coordinator(cfg, store, gate, project_workspace)
    try:
        assert isinstance(coordinator.curator.agent, GatedAgentAdapter)
        assert coordinator.curator.workspace == coordinator.curator.agent.cfg.agent.default_workspace
        assert coordinator.curator.workspace != str(project_workspace)
        assert coordinator.curator.agent.cfg.agent.share_network is False
        assert coordinator.curator.agent.cfg.agent.trace_enabled is False
    finally:
        store.close()


def _has_mount(cmd: list[str], flag: str, source: str, target: str) -> bool:
    return any(
        cmd[index : index + 3] == [flag, source, target]
        for index in range(max(0, len(cmd) - 2))
    )


def test_threshold_review_waits_for_idle_deadline(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.message_threshold = 2
        cfg.long_term_memory.review.idle_seconds = 10
        collect_source(store, message_id="m1")
        collect_source(store, message_id="m2")
        agent = FakeAgent()
        coordinator = make_coordinator(store, agent, cfg, tmp_path, now=100)

        coordinator.notify(GROUP)
        assert await coordinator.run_due(now=109) == ()
        outcomes = await coordinator.run_due(now=110)

        assert len(outcomes) == 1
        assert outcomes[0].error is None
        assert len(agent.calls) == 1
        assert store.status(GROUP).pending_count == 0

    asyncio.run(go())


def test_due_scope_below_threshold_waits_for_next_notification(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.message_threshold = 2
        cfg.long_term_memory.review.idle_seconds = 10
        collect_source(store, message_id="m1")
        coordinator = make_coordinator(store, FakeAgent(), cfg, tmp_path, now=100)

        coordinator.notify(GROUP)
        assert await coordinator.run_due(now=110) == ()
        assert GROUP not in coordinator._dirty

        collect_source(store, message_id="m2")
        coordinator.notify(GROUP)
        assert len(await coordinator.run_due(now=110)) == 1

    asyncio.run(go())


def test_periodic_minimum_and_explicit_review_bypass_threshold(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.message_threshold = 40
        cfg.long_term_memory.review.minimum_messages = 2
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        coordinator = make_coordinator(store, agent, cfg, tmp_path)

        assert await coordinator.run_periodic(now=1_000) == ()
        explicit = await coordinator.review_now(GROUP, actor=OWNER)
        assert explicit.error is None
        assert store.status(GROUP).pending_count == 0

        collect_source(store, message_id="m2")
        collect_source(store, message_id="m3")
        periodic = await coordinator.run_periodic(now=1_001)
        assert len(periodic) == 1
        assert periodic[0].error is None
        assert len(agent.calls) == 2

    asyncio.run(go())


def test_reviews_are_serialized_one_at_a_time(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path)

        first = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        second = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        while not agent.calls:
            await asyncio.sleep(0)
        assert agent.max_active == 1
        agent.release.set()
        first_outcome, second_outcome = await asyncio.gather(first, second)
        assert agent.max_active == 1
        assert first_outcome.error is None
        assert second_outcome.error == "no_sources"

    asyncio.run(go())


def test_interactive_background_cancel_does_not_cancel_explicit_review(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path)
        explicit = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        while not agent.calls:
            await asyncio.sleep(0)

        coordinator.cancel_background_for_interactive()
        agent.release.set()
        outcome = await explicit

        assert outcome.error is None
        assert store.status(GROUP).pending_count == 0

    asyncio.run(go())


def test_global_disable_fences_running_explicit_review(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path)
        explicit = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        while not agent.calls:
            await asyncio.sleep(0)

        disabled = deepcopy(cfg.long_term_memory)
        disabled.enabled = False
        coordinator.reload(disabled)
        agent.release.set()
        outcome = await explicit

        assert outcome.error in {"cancelled", "disabled"}
        assert store.status(GROUP).pending_count == 1

    asyncio.run(go())


def test_background_cancellation_before_commit_retains_sources(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.message_threshold = 1
        cfg.long_term_memory.review.idle_seconds = 1
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path, now=100)
        coordinator.notify(GROUP)

        due = asyncio.create_task(coordinator.run_due(now=101))
        while not agent.calls:
            await asyncio.sleep(0)
        coordinator.cancel_background_for_interactive()
        outcomes = await due

        assert outcomes[0].error == "cancelled"
        assert store.status(GROUP).pending_count == 1

    asyncio.run(go())


def test_cancellation_epoch_blocks_commit_when_curator_suppresses_cancellation(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.message_threshold = 1
        cfg.long_term_memory.review.idle_seconds = 1
        collect_source(store, message_id="m1")
        cancelled = asyncio.Event()
        release = asyncio.Event()

        class CancellationResistantAgent(FakeAgent):
            async def run(
                self,
                prompt: str,
                workspace: str | None = None,
                mode: str = "ask",
                **kwargs: Any,
            ) -> str:
                self.calls.append(AgentCall(prompt, workspace, mode, kwargs))
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return '{"operations": []}'

        agent = CancellationResistantAgent()
        coordinator = make_coordinator(store, agent, cfg, tmp_path, now=100)
        coordinator.notify(GROUP)
        due = asyncio.create_task(coordinator.run_due(now=101))
        while not agent.calls:
            await asyncio.sleep(0)

        coordinator.cancel_background_for_interactive()
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        outcomes = await asyncio.wait_for(due, 1)

        assert outcomes[0].error == "cancelled"
        assert store.status(GROUP).pending_count == 1

    asyncio.run(go())


def test_failed_review_keeps_sources_and_backs_off(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        source_id = collect_source(store, message_id="m1")
        agent = FakeAgent("not json")
        coordinator = make_coordinator(store, agent, cfg, tmp_path, now=1_000)

        outcome = await coordinator.review_now(GROUP, actor=None)

        assert outcome.error == "malformed_output"
        assert outcome.next_attempt_at == 1_060
        assert store.status(GROUP).pending_count == 1
        assert store.pending_sources(GROUP, limit=10, now=1_059) == ()
        pending = store.pending_sources(GROUP, limit=10, now=1_060)
        assert [value.id for value in pending] == [source_id]
        assert pending[0].attempt_count == 1

    asyncio.run(go())


def test_schema_incomplete_operation_keeps_source_for_retry(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        source_id = collect_source(store, message_id="schema-incomplete")
        coordinator = make_coordinator(
            store,
            FakeAgent('{"operations":[{"operation":"add"}]}'),
            cfg,
            tmp_path,
            now=1_000,
        )

        outcome = await coordinator.review_now(GROUP, actor=None)

        assert outcome.error == "malformed_output"
        assert outcome.accepted == ()
        assert outcome.next_attempt_at == 1_060
        assert store.status(GROUP).pending_count == 1
        pending = store.pending_sources(GROUP, limit=10, now=1_060)
        assert [value.id for value in pending] == [source_id]
        assert pending[0].attempt_count == 1

    asyncio.run(go())


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numeric_constant_keeps_source_for_retry(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
    constant: str,
) -> None:
    async def go() -> None:
        source_id = collect_source(store, message_id=f"constant-{constant}")
        output = (
            '{"operations":[{"operation":"add","source_ids":['
            f"{source_id}"
            '],"subject_kind":"user","subject_id":"u1",'
            '"content":"我喜欢简洁回答","source_kind":"self_statement",'
            f'"confidence":{constant}'
            "}]}"
        )
        coordinator = make_coordinator(
            store,
            FakeAgent(output),
            cfg,
            tmp_path,
            now=1_000,
        )

        outcome = await coordinator.review_now(GROUP, actor=None)

        assert outcome.error == "malformed_output"
        assert outcome.accepted == ()
        assert outcome.next_attempt_at == 1_060
        assert store.status(GROUP).pending_count == 1
        pending = store.pending_sources(GROUP, limit=10, now=1_060)
        assert [value.id for value in pending] == [source_id]
        assert pending[0].attempt_count == 1

    asyncio.run(go())


def test_max_attempt_failure_defers_until_next_periodic_cycle(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.max_attempts = 3
        cfg.long_term_memory.review.interval_seconds = 600
        collect_source(store, message_id="m1", attempt_count=2)
        agent = FakeAgent("not json")
        coordinator = make_coordinator(store, agent, cfg, tmp_path, now=1_000)

        outcome = await coordinator.review_now(GROUP, actor=OWNER)

        assert outcome.next_attempt_at == 1_600
        assert await coordinator.run_periodic(now=1_599) == ()
        cfg.long_term_memory.review.message_threshold = 1
        cfg.long_term_memory.review.idle_seconds = 1
        coordinator.wall_clock = lambda: 1_600.0
        coordinator.notify(GROUP)
        assert await coordinator.run_due(now=1_001) == ()
        assert len(await coordinator.run_periodic(now=1_600)) == 1

    asyncio.run(go())


def test_mixed_failure_schedules_each_source_from_its_own_attempt_count(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.max_attempts = 3
        cfg.long_term_memory.review.interval_seconds = 600
        fresh_id = collect_source(store, message_id="fresh", attempt_count=0)
        exhausted_id = collect_source(store, message_id="exhausted", attempt_count=2)
        coordinator = make_coordinator(store, FakeAgent("not json"), cfg, tmp_path, now=1_000)

        outcome = await coordinator.review_now(GROUP, actor=OWNER)

        assert outcome.error == "malformed_output"
        assert outcome.next_attempt_at == 1_060
        rows = store._conn.execute(  # noqa: SLF001 - inspect durable retry state
            "SELECT id, attempt_count, next_attempt_at FROM review_buffer ORDER BY id"
        ).fetchall()
        states = {
            int(row["id"]): (int(row["attempt_count"]), int(row["next_attempt_at"]))
            for row in rows
        }
        assert states == {
            fresh_id: (1, 1_060),
            exhausted_id: (3, 1_600),
        }
        assert store.status(GROUP).pending_count == 2

    asyncio.run(go())


def test_mixed_failure_deadlines_survive_store_restart(
    cfg: BridgeConfig,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        database = tmp_path / "restart-memory.sqlite3"
        initial = LongTermMemoryStore(database)
        initial.initialize()
        initial.set_scope_enabled(GROUP, True)
        cfg.long_term_memory.review.max_attempts = 3
        cfg.long_term_memory.review.interval_seconds = 600
        retrying_id = collect_source(initial, message_id="retrying", attempt_count=1)
        exhausted_id = collect_source(initial, message_id="exhausted", attempt_count=2)
        coordinator = make_coordinator(
            initial,
            FakeAgent("not json"),
            cfg,
            tmp_path,
            now=1_000,
        )

        outcome = await coordinator.review_now(GROUP, actor=OWNER)
        assert outcome.next_attempt_at == 1_120
        initial.close()

        reopened = LongTermMemoryStore(database)
        reopened.initialize()
        try:
            rows = reopened._conn.execute(  # noqa: SLF001 - verify restart persistence
                "SELECT id, attempt_count, next_attempt_at FROM review_buffer ORDER BY id"
            ).fetchall()
            states = {
                int(row["id"]): (
                    int(row["attempt_count"]),
                    int(row["next_attempt_at"]),
                )
                for row in rows
            }
            assert states == {
                retrying_id: (2, 1_120),
                exhausted_id: (3, 1_600),
            }
            assert reopened.status(GROUP).pending_count == 2
        finally:
            reopened.close()

    asyncio.run(go())


def test_validated_commit_and_source_deletion_are_atomic(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        collect_source(store, message_id="m1")
        agent = FakeAgent(
            '{"operations":[{"operation":"add","subject_kind":"user",'
            '"subject_id":"u1","category":"preference","content":"喜欢简洁回答",'
            '"confidence":0.9,"status":"active","sensitivity":"normal",'
            '"source_kind":"self_statement","explicit_memory":false,\n'
            '"decay_exempt":false,"expires_at":null,"source_ids":[1],'
            '"related_item_ids":[],"item_id":null}]}'
        )
        coordinator = make_coordinator(store, agent, cfg, tmp_path)

        outcome = await coordinator.review_now(GROUP, actor=None)

        assert outcome.error is None
        assert len(outcome.committed) == 1
        assert outcome.committed[0].content == "喜欢简洁回答"
        assert store.status(GROUP).pending_count == 0

    asyncio.run(go())


def test_explicit_owner_review_does_not_blanket_confirm_curator_claims(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        source_id = store.collect(
            MemorySource(
                scope=GROUP,
                message_id="unrelated",
                sender_id="someone-else",
                text="hello everyone",
                message_timestamp=100,
            )
        )
        assert source_id is not None
        agent = FakeAgent(
            json.dumps(
                {
                    "operations": [
                        {
                            "operation": "add",
                            "source_ids": [source_id],
                            "subject_kind": "user",
                            "subject_id": "victim",
                            "category": "identity",
                            "content": "victim manages payroll",
                            "confidence": 0.9,
                            "status": "active",
                            "sensitivity": "normal",
                            "source_kind": "owner_confirmed",
                            "explicit_memory": False,
                            "decay_exempt": False,
                            "expires_at": None,
                        }
                    ]
                }
            )
        )
        coordinator = make_coordinator(store, agent, cfg, tmp_path)

        outcome = await coordinator.review_now(
            GROUP, actor=MemoryActor("owner", "group_owner")
        )

        assert outcome.error is None
        assert outcome.accepted == ()
        assert outcome.rejected[0].reason == "owner_confirmation_required"
        assert store.list_items(GROUP, limit=10) == ()
        assert store.status(GROUP).pending_count == 0

    asyncio.run(go())


def test_successful_review_records_actual_trigger_class(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.minimum_messages = 1
        collect_source(store, message_id="m1")
        coordinator = make_coordinator(store, FakeAgent(), cfg, tmp_path)

        assert len(await coordinator.run_periodic(now=1_000)) == 1
        row = store._conn.execute(  # noqa: SLF001 - verify durable audit metadata
            "SELECT trigger_class FROM review_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["trigger_class"] == "periodic"

    asyncio.run(go())


def test_disable_before_commit_retains_sources(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    class DisablingCurator:
        async def review(self, *_args: Any, **_kwargs: Any) -> Any:
            from qq_agent_bridge.memory_review import CuratorOutcome

            store.set_scope_enabled(GROUP, False)
            return CuratorOutcome(
                accepted=(
                    MemoryProposal.add(
                        subject_kind="user",
                        subject_id="u1",
                        content="喜欢简洁回答",
                        source_kind="self_statement",
                    ),
                ),
                proposed_count=1,
            )

    async def go() -> None:
        collect_source(store, message_id="m1")
        coordinator = MemoryReviewCoordinator(
            store,
            DisablingCurator(),
            cfg.long_term_memory,
            StorageActivityGate(),
            clock=lambda: 1_000.0,
            wall_clock=lambda: 1_000.0,
        )

        outcome = await coordinator.review_now(GROUP, actor=OWNER)

        assert outcome.error == "disabled"
        assert store.status(GROUP).pending_count == 1
        assert store.list_items(GROUP) == ()

    asyncio.run(go())


def test_ttl_cleanup_and_decay_run_on_maintenance_cycle(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        store.raw_ttl_seconds = 100
        store.decay_grace_seconds = 0
        old_source = collect_source(store, message_id="old", created_at=100)
        seed_source = collect_source(store, message_id="seed", created_at=200)
        item = store.commit_review(
            GROUP,
            (seed_source,),
            (
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="u1",
                    category="project",
                    content="长期项目",
                    confidence=0.8,
                    source_kind="self_statement",
                    created_at=200,
                ),
            ),
        )[0]
        coordinator = make_coordinator(store, FakeAgent(), cfg, tmp_path, now=200_000)

        summary = await coordinator.run_maintenance(now=200_000)

        assert summary.expired_sources == 1
        assert summary.decayed_items == 1
        assert store.pending_sources(GROUP, 10, now=200_000) == ()
        assert store.get_item(GROUP, item.id).effective_score < item.effective_score  # type: ignore[union-attr]
        assert old_source > 0

    asyncio.run(go())


def test_maintenance_waits_for_review_to_finish(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path)
        review = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        while not agent.calls:
            await asyncio.sleep(0)

        maintenance = asyncio.create_task(coordinator.run_maintenance(now=2_000))
        await asyncio.sleep(0)
        assert maintenance.done() is False

        agent.release.set()
        await review
        await maintenance

    asyncio.run(go())


def test_reload_disable_and_stop_cancel_background_work_cleanly(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        cfg.long_term_memory.review.message_threshold = 1
        cfg.long_term_memory.review.idle_seconds = 1
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path, now=100)
        await coordinator.start()
        coordinator.notify(GROUP)
        due = asyncio.create_task(coordinator.run_due(now=101))
        while not agent.calls:
            await asyncio.sleep(0)

        disabled = cfg.long_term_memory
        disabled.enabled = False
        coordinator.reload(disabled)
        assert (await due)[0].error == "cancelled"
        await coordinator.stop()

        assert coordinator.running is False
        assert store.status(GROUP).pending_count == 1

    asyncio.run(go())


def test_reload_updates_curator_limits_and_wakes_maintenance_interval(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        decay_calls = 0
        original_decay = store.apply_decay

        def counted_decay(now: int) -> int:
            nonlocal decay_calls
            decay_calls += 1
            return original_decay(now)

        monkeypatch.setattr(store, "apply_decay", counted_decay)
        cfg.long_term_memory.decay.interval_seconds = 1_000
        coordinator = make_coordinator(store, FakeAgent(), cfg, tmp_path)
        await coordinator.start()
        assert decay_calls == 1

        reloaded = deepcopy(cfg.long_term_memory)
        reloaded.review.model = "reloaded-model"
        reloaded.review.timeout_seconds = 7
        reloaded.decay.interval_seconds = 0.01  # type: ignore[assignment]
        coordinator.reload(reloaded)
        await asyncio.sleep(0.05)

        assert coordinator.curator.cfg is reloaded.review
        assert decay_calls >= 2
        await coordinator.stop()

    asyncio.run(go())


def test_curator_accepts_markdown_wrapped_json_output(
    tmp_path: Path, cfg: BridgeConfig, store: LongTermMemoryStore,
) -> None:
    """Full pipeline: model returns markdown-wrapped JSON -> parse -> validate -> commit."""
    scope = MemoryScope("group", "g1")
    store.set_scope_enabled(scope, True)

    source = MemorySource(
        id=1,
        scope=scope,
        message_id="m1", sender_id="u1",
        text="我喜欢喝咖啡", message_timestamp=1000,
    )
    store.collect(source)

    # Model output wrapped in markdown fence with trailing prose
    model_output = (
        '```json\n'
        '{"operations":['
        '{"operation":"add","source_ids":[1],'
        '"subject_kind":"user","subject_id":"u1",'
        '"category":"preference","content":"喜欢喝咖啡",'
        '"confidence":0.91,"status":"active",'
        '"sensitivity":"normal","source_kind":"self_statement",'
        '"explicit_memory":false,"decay_exempt":false,"expires_at":null}'
        ']}\n'
        '```\n'
        '这是提取的记忆。'
    )

    curator = MemoryCurator(
        FakeAgent(model_output),
        MemoryValidator(cfg, store=store),
        cfg.long_term_memory.review,
        workspace=tmp_path,
    )

    outcome = asyncio.run(curator.review(scope, (source,), ()))

    assert outcome.error is None
    assert outcome.proposed_count == 1
    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].content == "喜欢喝咖啡"


def test_e2e_collection_trigger_review_commit_pipeline(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    """Full MemoryReviewCoordinator pipeline: collection -> trigger -> review -> commit.

    Verifies that after a successful review triggered via review_now:
    - memory_items table has new rows with the expected content
    - review_buffer has no remaining pending rows
    """
    async def go() -> None:
        # Step 1: Collect messages into review_buffer
        source_id_1 = collect_source(store, message_id="m1", text="我喜欢简洁回答")
        source_id_2 = collect_source(store, message_id="m2", text="我每天都喝咖啡")
        assert store.status(GROUP).pending_count == 2

        # Step 2: Build coordinator with a FakeAgent that returns clean JSON
        # (not markdown-wrapped -- the real model may return clean JSON directly)
        agent = FakeAgent(
            json.dumps(
                {
                    "operations": [
                        {
                            "operation": "add",
                            "source_ids": [source_id_1],
                            "subject_kind": "user",
                            "subject_id": "u1",
                            "category": "preference",
                            "content": "喜欢简洁回答",
                            "confidence": 0.91,
                            "status": "active",
                            "sensitivity": "normal",
                            "source_kind": "self_statement",
                            "explicit_memory": False,
                            "decay_exempt": False,
                            "expires_at": None,
                        },
                        {
                            "operation": "add",
                            "source_ids": [source_id_2],
                            "subject_kind": "user",
                            "subject_id": "u1",
                            "category": "preference",
                            "content": "每天都喝咖啡",
                            "confidence": 0.91,
                            "status": "active",
                            "sensitivity": "normal",
                            "source_kind": "self_statement",
                            "explicit_memory": False,
                            "decay_exempt": False,
                            "expires_at": None,
                        },
                    ]
                }
            )
        )
        coordinator = make_coordinator(store, agent, cfg, tmp_path)

        # Step 3: Trigger a review
        outcome = await coordinator.review_now(GROUP, actor=None)

        # Step 4: Verify items appear in memory_items table
        assert outcome.error is None
        assert len(outcome.committed) == 2
        committed_contents = {item.content for item in outcome.committed}
        assert "喜欢简洁回答" in committed_contents
        assert "每天都喝咖啡" in committed_contents

        # Step 5: Verify review_buffer has no remaining pending rows
        assert store.status(GROUP).pending_count == 0

        # Step 6: Verify items are retrievable via list_items
        items = store.list_items(GROUP, limit=10)
        assert len(items) == 2
        item_contents = {item.content for item in items}
        assert "喜欢简洁回答" in item_contents
        assert "每天都喝咖啡" in item_contents

    asyncio.run(go())


def test_stop_cancels_an_uncommitted_explicit_review(
    cfg: BridgeConfig,
    store: LongTermMemoryStore,
    tmp_path: Path,
) -> None:
    async def go() -> None:
        collect_source(store, message_id="m1")
        agent = FakeAgent()
        agent.release = asyncio.Event()
        coordinator = make_coordinator(store, agent, cfg, tmp_path)
        review = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        queued = asyncio.create_task(coordinator.review_now(GROUP, actor=OWNER))
        while not agent.calls:
            await asyncio.sleep(0)

        await coordinator.stop()

        assert review.done()
        assert queued.done()
        assert (await review).error == "cancelled"
        assert (await queued).error == "cancelled"
        assert store.status(GROUP).pending_count == 1

    asyncio.run(go())
