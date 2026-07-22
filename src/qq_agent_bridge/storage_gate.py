"""Coordinate shared Agent activity with exclusive storage maintenance."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
import logging
import os
from pathlib import Path
import shutil

logger = logging.getLogger(__name__)
import stat
import tempfile
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

    def __init__(
        self,
        delegate: Any,
        gate: StorageActivityGate,
        *,
        owned_paths: tuple[Path, ...] = (),
    ) -> None:
        self.delegate = delegate
        self.gate = gate
        self._owned_paths = owned_paths

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

    def dispose(self) -> None:
        """Remove only private paths created for this adapter."""
        pending = self._owned_paths
        failed: list[Path] = []
        failure: Exception | None = None
        for path in reversed(pending):
            try:
                _remove_private_curator_path(path)
            except Exception as exc:  # noqa: BLE001 - attempt every owned path
                failed.append(path)
                failure = failure or exc
        self._owned_paths = tuple(reversed(failed))
        if failure is not None:
            raise RuntimeError("restricted Agent path cleanup failed") from failure


def build_restricted_agent_adapter(
    cfg: BridgeConfig,
    gate: StorageActivityGate,
    workspace: Path | str,
    *,
    timeout_seconds: int,
    max_output_chars: int,
) -> GatedAgentAdapter:
    """Build an isolated ask-only adapter configuration for background analysis."""
    owned_paths: list[Path] = []
    try:
        restricted = deepcopy(cfg)
        curator_workspace = _create_private_curator_path("curator-workspace-")
        owned_paths.append(curator_workspace)
        curator_home = _create_private_curator_path("curator-home-")
        owned_paths.append(curator_home)
        resolved_workspace = str(curator_workspace)
        restricted.workspaces = {resolved_workspace: True}
        restricted.agent.runtime = "cursor-cli"
        restricted.agent.command = {}
        restricted.max_runtime_seconds = max(1, int(timeout_seconds))
        restricted.max_output_chars = max(1, int(max_output_chars))
        restricted.agent.default_workspace = resolved_workspace
        restricted.agent.use_bwrap = True
        restricted.agent.share_network = cfg.agent.share_network
        restricted.agent.force_task_tools = False
        restricted.agent.hardened_read_only = True
        restricted.agent.log_subprocess_output = cfg.agent.log_subprocess_output
        restricted.agent.sandbox_home = str(curator_home)
        restricted.agent.max_runtime_seconds = restricted.max_runtime_seconds
        restricted.agent.max_output_chars = restricted.max_output_chars
        restricted.agent.trace_enabled = cfg.agent.trace_enabled
        logger.info(
            "restricted agent config: trace_enabled=%s log_subprocess=%s share_network=%s",
            restricted.agent.trace_enabled,
            restricted.agent.log_subprocess_output,
            restricted.agent.share_network,
        )
        restricted.progress.enabled = False
        restricted.resources.enabled = False
        return GatedAgentAdapter(
            build_agent_adapter(restricted),
            gate,
            owned_paths=tuple(owned_paths),
        )
    except BaseException:
        for path in reversed(owned_paths):
            try:
                _remove_private_curator_path(path)
            except Exception:
                pass
        raise


def _create_private_curator_path(prefix: str) -> Path:
    home, root = _private_state_root()
    current = home
    for part in root.relative_to(home).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("private application state path must contain directories only")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ValueError("private application state path is not safely owned")
    root.chmod(0o700)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    path.chmod(0o700)
    return path


def _private_state_root() -> tuple[Path, Path]:
    home = Path.home().resolve(strict=True)
    return home, home / ".local" / "state" / "qq-agent-bridge"


def _remove_private_curator_path(path: Path) -> None:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return
    _home, root = _private_state_root()
    resolved_root = root.resolve(strict=True)
    if candidate.parent.resolve(strict=True) != resolved_root or not candidate.name.startswith(
        ("curator-workspace-", "curator-home-")
    ):
        raise ValueError("refusing to remove an unowned restricted Agent path")
    if metadata.st_uid != os.getuid():
        raise ValueError("refusing to remove a foreign-owned restricted Agent path")
    if stat.S_ISLNK(metadata.st_mode):
        candidate.unlink()
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("restricted Agent path is not a directory")
    shutil.rmtree(candidate)
