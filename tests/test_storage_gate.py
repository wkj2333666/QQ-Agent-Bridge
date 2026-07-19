"""Storage activity/maintenance coordination tests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import qq_agent_bridge.storage_gate as storage_gate_module  # type: ignore
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.storage_gate import (  # type: ignore
    GatedAgentAdapter,
    StorageActivityGate,
    build_restricted_agent_adapter,
)


def test_maintenance_waits_for_activity_and_blocks_new_activity() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        first_entered = asyncio.Event()
        maintenance_waiting = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with gate.activity():
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-exit")

        async def maintain() -> None:
            await first_entered.wait()
            maintenance_waiting.set()
            async with gate.maintenance():
                order.append("maintenance")

        async def second() -> None:
            await maintenance_waiting.wait()
            await asyncio.sleep(0)
            async with gate.activity():
                order.append("second")

        first_task = asyncio.create_task(first())
        maintenance_task = asyncio.create_task(maintain())
        second_task = asyncio.create_task(second())
        await first_entered.wait()
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first_task, maintenance_task, second_task)

        assert order == ["first-enter", "first-exit", "maintenance", "second"]
        assert gate.active_count == 0

    asyncio.run(go())


def test_activity_is_reentrant_in_same_task() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        async with gate.activity():
            assert gate.active_count == 1
            async with gate.activity():
                assert gate.active_count == 1
            assert gate.active_count == 1
        assert gate.active_count == 0

    asyncio.run(go())


def test_child_task_does_not_inherit_parent_reentrancy() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        child_entered = asyncio.Event()
        release_child = asyncio.Event()

        async def child() -> None:
            async with gate.activity():
                child_entered.set()
                await release_child.wait()

        async with gate.activity():
            task = asyncio.create_task(child())
            await child_entered.wait()
            assert gate.active_count == 2
            release_child.set()
            await task
            assert gate.active_count == 1

    asyncio.run(go())


def test_cancelled_maintenance_waiter_does_not_block_new_activity() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def active() -> None:
            async with gate.activity():
                entered.set()
                await release.wait()

        async def maintain() -> None:
            async with gate.maintenance():
                raise AssertionError("cancelled waiter must not enter")

        active_task = asyncio.create_task(active())
        await entered.wait()
        maintenance_task = asyncio.create_task(maintain())
        await asyncio.sleep(0)
        maintenance_task.cancel()
        await asyncio.gather(maintenance_task, return_exceptions=True)
        release.set()
        await active_task

        async with asyncio.timeout(0.2):
            async with gate.activity():
                assert gate.active_count == 1

    asyncio.run(go())


def test_cancelled_activity_releases_gate() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        entered = asyncio.Event()

        async def active() -> None:
            async with gate.activity():
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(active())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        async with asyncio.timeout(0.2):
            async with gate.maintenance():
                assert gate.active_count == 0

    asyncio.run(go())


def test_gated_agent_adapter_preserves_full_run_contract() -> None:
    class FakeAgent:
        cfg = object()

        def __init__(self) -> None:
            self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def run(self, *args: Any, **kwargs: Any) -> str:
            self.calls.append((args, kwargs))
            return "ok"

    async def go() -> None:
        gate = StorageActivityGate()
        delegate = FakeAgent()
        adapter = GatedAgentAdapter(delegate, gate)

        async def progress(_text: str) -> None:
            return None

        result = await adapter.run(
            "prompt",
            "/workspace",
            "task",
            model="auto",
            progress=progress,
            trace_id="job-1",
            redact_extra=("secret",),
        )

        assert result == "ok"
        assert adapter.cfg is delegate.cfg
        assert gate.active_count == 0
        assert delegate.calls == [
            (
                ("prompt", "/workspace", "task"),
                {
                    "model": "auto",
                    "progress": progress,
                    "trace_id": "job-1",
                    "redact_extra": ("secret",),
                },
            )
        ]

    asyncio.run(go())


def test_restricted_adapter_disposal_removes_only_its_owned_private_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(mode=0o700)
    unrelated = fake_home / ".local" / "state" / "qq-agent-bridge" / "keep.txt"
    unrelated.parent.mkdir(parents=True, mode=0o700)
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    cfg = BridgeConfig()

    adapter = build_restricted_agent_adapter(
        cfg,
        StorageActivityGate(),
        tmp_path,
        timeout_seconds=10,
        max_output_chars=1_000,
    )
    workspace = Path(adapter.cfg.agent.default_workspace)
    sandbox_home = Path(adapter.cfg.agent.sandbox_home)
    (sandbox_home / ".config" / "cursor").mkdir(parents=True)
    auth_copy = sandbox_home / ".config" / "cursor" / "auth.json"
    auth_copy.write_text('{"token":"copied-secret"}', encoding="utf-8")

    adapter.dispose()
    adapter.dispose()

    assert not workspace.exists()
    assert not sandbox_home.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_restricted_adapter_builder_cleans_first_path_when_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        storage_gate_module,
        "build_agent_adapter",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("injected build failure")),
    )

    with pytest.raises(RuntimeError, match="injected build failure"):
        build_restricted_agent_adapter(
            BridgeConfig(),
            StorageActivityGate(),
            tmp_path,
            timeout_seconds=10,
            max_output_chars=1_000,
        )

    state_root = fake_home / ".local" / "state" / "qq-agent-bridge"
    assert list(state_root.glob("curator-workspace-*")) == []
    assert list(state_root.glob("curator-home-*")) == []
