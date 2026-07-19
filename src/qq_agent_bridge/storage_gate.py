"""Coordinate shared Agent activity with exclusive storage maintenance."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Any

from .agent_runtime import ProgressCallback, build_agent_adapter, run_agent
from .config import BridgeConfig


class StorageActivityGate:
    """A fair, task-reentrant activity/exclusive-maintenance gate."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_count = 0
        self._maintenance_active = False
        self._maintenance_waiters = 0
        self._owner: ContextVar[asyncio.Task[Any] | None] = ContextVar(
            "storage_activity_owner",
            default=None,
        )
        self._depth: ContextVar[int] = ContextVar("storage_activity_depth", default=0)

    @property
    def active_count(self) -> int:
        return self._active_count

    @asynccontextmanager
    async def activity(self):
        task = asyncio.current_task()
        if task is not None and self._owner.get() is task and self._depth.get() > 0:
            depth_token = self._depth.set(self._depth.get() + 1)
            try:
                yield
            finally:
                self._depth.reset(depth_token)
            return

        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._maintenance_active and self._maintenance_waiters == 0
            )
            self._active_count += 1
        owner_token = self._owner.set(task)
        depth_token = self._depth.set(1)
        try:
            yield
        finally:
            self._depth.reset(depth_token)
            self._owner.reset(owner_token)
            async with self._condition:
                self._active_count -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def maintenance(self):
        if self._owner.get() is asyncio.current_task() and self._depth.get() > 0:
            raise RuntimeError("maintenance cannot start from active storage work")

        async with self._condition:
            self._maintenance_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._maintenance_active and self._active_count == 0
                )
            except BaseException:
                self._maintenance_waiters -= 1
                self._condition.notify_all()
                raise
            self._maintenance_waiters -= 1
            self._maintenance_active = True
        try:
            yield
        finally:
            async with self._condition:
                self._maintenance_active = False
                self._condition.notify_all()


class GatedAgentAdapter:
    """Apply an activity lease to every call made through an Agent adapter."""

    def __init__(self, delegate: Any, gate: StorageActivityGate) -> None:
        self.delegate = delegate
        self.gate = gate

    @property
    def cfg(self) -> Any:
        return self.delegate.cfg

    async def run(
        self,
        prompt: str,
        workspace: str | None = None,
        mode: str = "ask",
        model: str | None = None,
        progress: ProgressCallback | None = None,
        trace_id: str | None = None,
        redact_extra: tuple[str, ...] | None = None,
    ) -> str:
        ws = workspace or self.cfg.agent.default_workspace
        async with self.gate.activity():
            return await run_agent(
                self.delegate,
                prompt,
                ws,
                mode,
                model=model,
                progress=progress,
                trace_id=trace_id,
                redact_extra=redact_extra,
            )


def build_restricted_agent_adapter(
    cfg: BridgeConfig,
    gate: StorageActivityGate,
    workspace: Path | str,
    *,
    timeout_seconds: int,
    max_output_chars: int,
) -> GatedAgentAdapter:
    """Build an isolated ask-only adapter configuration for background analysis."""
    restricted = deepcopy(cfg)
    resolved_workspace = str(Path(workspace).expanduser().resolve(strict=False))
    restricted.workspaces = {resolved_workspace: True}
    restricted.max_runtime_seconds = max(1, int(timeout_seconds))
    restricted.max_output_chars = max(1, int(max_output_chars))
    restricted.agent.default_workspace = resolved_workspace
    restricted.agent.use_bwrap = True
    restricted.agent.share_network = False
    restricted.agent.force_task_tools = False
    restricted.agent.max_runtime_seconds = restricted.max_runtime_seconds
    restricted.agent.max_output_chars = restricted.max_output_chars
    restricted.agent.trace_enabled = False
    restricted.progress.enabled = False
    restricted.resources.enabled = False
    return GatedAgentAdapter(build_agent_adapter(restricted), gate)
