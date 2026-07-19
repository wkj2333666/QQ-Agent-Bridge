"""Bounded, symlink-safe maintenance of application-owned storage."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from typing import Any, Awaitable, Callable, Literal

from .config import BridgeConfig


MAX_CANDIDATES_PER_AREA = 100_000
RUN_BUDGET_SECONDS = 30.0
DATED_RESOURCE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AREAS = ("sandbox", "traces", "resources")
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
logger = logging.getLogger(__name__)
MaintenanceTrigger = Literal["startup", "periodic", "pressure"]


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    area: str
    kind: str
    size: int
    mtime: float
    root: Path
    relative_parts: tuple[str, ...]
    device: int
    inode: int
    mode: int
    entries: int


@dataclass
class CleanupStats:
    scanned: int = 0
    removed: int = 0
    released_bytes: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ManagedRoot:
    area: str
    path: Path
    boundary: Path


@dataclass
class InventoryResult:
    candidates: dict[str, list[CleanupCandidate]] = field(
        default_factory=lambda: {area: [] for area in _AREAS}
    )
    stats: CleanupStats = field(default_factory=CleanupStats)
    skipped_areas: int = 0
    roots: dict[str, ManagedRoot] = field(default_factory=dict)


@dataclass(frozen=True)
class MaintenanceSummary:
    trigger: MaintenanceTrigger
    elapsed_seconds: float
    scanned: int
    removed: int
    released_bytes: int
    skipped_areas: int
    free_before: int | None
    free_after: int | None


@dataclass
class _ScanBudget:
    monotonic: Callable[[], float]
    deadline: float
    limit: int
    scanned: int = 0
    skipped: int = 0
    exhausted: bool = False

    def take(self) -> bool:
        if self.monotonic() >= self.deadline or self.scanned >= self.limit:
            self.exhausted = True
            return False
        self.scanned += 1
        return True

    def available(self) -> bool:
        if self.monotonic() >= self.deadline or self.scanned >= self.limit:
            self.exhausted = True
            return False
        return True


class StorageMaintainer:
    """Inventory and remove only explicitly disposable application data."""

    def __init__(
        self,
        cfg: BridgeConfig,
        gate: object | None = None,
        *,
        home: Path | None = None,
        cwd: Path | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        thread_runner: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ) -> None:
        self.cfg = cfg
        self.gate = gate
        self.home = (home or Path.home()).absolute()
        self.cwd = (cwd or Path.cwd()).absolute()
        self.now = now
        self.monotonic = monotonic
        self.disk_usage = disk_usage
        self.thread_runner = thread_runner
        self._last_pressure_warning_at: float | None = None
        self._workspace_setting = cfg.agent.default_workspace
        self._sandbox_setting = cfg.agent.sandbox_home
        self._trace_setting = cfg.agent.trace_root
        self._resource_setting = cfg.resources.root
        self._wake_event = asyncio.Event()
        self._pressure_requested = False
        self._run_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False
        self._protected_lock = threading.Lock()
        self._protected_paths: set[Path] = set()

    @property
    def loop_task(self) -> asyncio.Task[None] | None:
        return self._loop_task

    @property
    def wake_event(self) -> asyncio.Event:
        return self._wake_event

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        if not self.cfg.storage_maintenance.enabled:
            return
        async with self._run_lock:
            await self.run("startup")
        if not self._stopping and self.cfg.storage_maintenance.enabled:
            self._loop_task = asyncio.create_task(
                self._maintenance_loop(),
                name="storage-maintenance",
            )

    async def stop(self) -> None:
        self._stopping = True
        self._started = False
        self._wake_event.set()
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._pressure_requested = False

    def pressure_needed(self) -> bool:
        if not self.cfg.storage_maintenance.enabled:
            return False
        roots, _skipped = self._resolve_roots()
        free = self._free_bytes(roots)
        return free is not None and free < self.cfg.storage_maintenance.min_free_bytes

    def protect_path(self, path: Path | str) -> None:
        protected = Path(os.path.abspath(Path(path).expanduser()))
        with self._protected_lock:
            self._protected_paths.add(protected)

    def unprotect_path(self, path: Path | str) -> None:
        protected = Path(os.path.abspath(Path(path).expanduser()))
        with self._protected_lock:
            self._protected_paths.discard(protected)

    def is_protected(self, path: Path | str) -> bool:
        protected = Path(os.path.abspath(Path(path).expanduser()))
        with self._protected_lock:
            return protected in self._protected_paths

    def request_pressure_check(self) -> None:
        if not self.cfg.storage_maintenance.enabled or not self.pressure_needed():
            return
        self._pressure_requested = True
        self._wake_event.set()

    def reload_config(self, cfg: BridgeConfig) -> bool:
        roots_changed = self._root_signature(cfg) != self._root_signature_from_snapshot()
        self.cfg = cfg
        self._wake_event.set()
        if (
            self._started
            and cfg.storage_maintenance.enabled
            and (self._loop_task is None or self._loop_task.done())
        ):
            self._stopping = False
            self._loop_task = asyncio.create_task(
                self._maintenance_loop(),
                name="storage-maintenance",
            )
        return roots_changed

    async def run(self, trigger: MaintenanceTrigger) -> MaintenanceSummary:
        """Run one bounded maintenance pass outside the event-loop thread."""
        logger.info("storage maintenance start trigger=%s", trigger)
        cancelled = threading.Event()
        lease = self.gate.maintenance() if self.gate is not None else _null_lease()
        async with lease:
            worker = asyncio.create_task(self.thread_runner(self._run_sync, trigger, cancelled))
            try:
                summary = await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled.set()
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    pass
                raise
        if (
            trigger == "pressure"
            and summary.free_after is not None
            and summary.free_after < self.cfg.storage_maintenance.min_free_bytes
        ):
            warning_now = self.monotonic()
            if (
                self._last_pressure_warning_at is None
                or warning_now - self._last_pressure_warning_at >= 3_600
            ):
                logger.warning(
                    "storage pressure remains free_bytes=%d min_free_bytes=%d",
                    summary.free_after,
                    self.cfg.storage_maintenance.min_free_bytes,
                )
                self._last_pressure_warning_at = warning_now
        logger.info(
            "storage maintenance done trigger=%s scanned=%d removed=%d released_bytes=%d "
            "skipped_areas=%d elapsed_ms=%d free_before=%s free_after=%s",
            trigger,
            summary.scanned,
            summary.removed,
            summary.released_bytes,
            summary.skipped_areas,
            round(summary.elapsed_seconds * 1000),
            summary.free_before,
            summary.free_after,
        )
        return summary

    async def _maintenance_loop(self) -> None:
        try:
            while not self._stopping:
                interval = max(0.001, float(self.cfg.storage_maintenance.interval_seconds))
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
                except TimeoutError:
                    trigger: MaintenanceTrigger = "periodic"
                else:
                    self._wake_event.clear()
                    if self._stopping or not self.cfg.storage_maintenance.enabled:
                        return
                    if not self._pressure_requested:
                        continue
                    self._pressure_requested = False
                    trigger = "pressure"

                async with self._run_lock:
                    if self._stopping or not self.cfg.storage_maintenance.enabled:
                        return
                    try:
                        await self.run(trigger)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - maintenance must not kill the bridge
                        logger.warning(
                            "storage maintenance failed trigger=%s error=%s",
                            trigger,
                            type(exc).__name__,
                        )
        finally:
            if self._loop_task is asyncio.current_task():
                self._loop_task = None

    def inventory(
        self,
        *,
        now: float | None = None,
        deadline: float | None = None,
    ) -> InventoryResult:
        del now  # Policy uses wall time; inventory only records filesystem mtimes.
        result = InventoryResult()
        roots, skipped = self._resolve_roots()
        result.roots = roots
        result.skipped_areas = skipped
        run_deadline = deadline if deadline is not None else self.monotonic() + RUN_BUDGET_SECONDS

        for area in _AREAS:
            root = roots.get(area)
            if root is None:
                continue
            budget = _ScanBudget(self.monotonic, run_deadline, MAX_CANDIDATES_PER_AREA)
            try:
                if area == "sandbox":
                    self._inventory_sandbox(root, result.candidates[area], budget)
                elif area == "traces":
                    self._inventory_traces(root, result.candidates[area], budget)
                else:
                    self._inventory_resources(root, result.candidates[area], budget)
            except OSError:
                result.skipped_areas += 1
            result.stats.scanned += budget.scanned
            result.stats.skipped += budget.skipped
            if budget.exhausted:
                result.stats.skipped += 1
        return result

    def delete_candidate(self, candidate: CleanupCandidate) -> tuple[int, int]:
        """Delete a still-identical candidate using root-relative file descriptors."""
        if self.is_protected(candidate.path):
            return (0, 0)
        root = self._resolve_roots()[0].get(candidate.area)
        if root is None or root.path != candidate.root or not candidate.relative_parts:
            return (0, 0)

        root_fd = -1
        parent_fd = -1
        try:
            root_fd = os.open(root.path, _OPEN_DIR_FLAGS)
            parent_fd = os.dup(root_fd)
            for component in candidate.relative_parts[:-1]:
                next_fd = os.open(component, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd

            name = candidate.relative_parts[-1]
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                current.st_dev != candidate.device
                or current.st_ino != candidate.inode
                or stat.S_IFMT(current.st_mode) != stat.S_IFMT(candidate.mode)
            ):
                return (0, 0)
            if self.is_protected(candidate.path):
                return (0, 0)
            removed, released, _complete = self._delete_entry(
                parent_fd,
                name,
                expected=(candidate.device, candidate.inode, candidate.mode),
            )
            return (removed, released)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            return (0, 0)
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            if root_fd >= 0:
                os.close(root_fd)

    def _run_sync(
        self,
        trigger: MaintenanceTrigger,
        cancelled: threading.Event,
    ) -> MaintenanceSummary:
        started = self.monotonic()
        deadline = started + RUN_BUDGET_SECONDS
        inventory = self.inventory(now=self.now(), deadline=deadline)
        free_before = self._free_bytes(inventory.roots)
        removed = 0
        released = 0

        if trigger == "pressure":
            ordered = sorted(
                self._all_candidates(inventory),
                key=lambda item: (self._pressure_rank(item), item.mtime, item.path.as_posix()),
            )
            for candidate in ordered:
                if self._must_stop(cancelled, deadline):
                    break
                current_free = self._free_bytes(inventory.roots)
                if current_free is not None and current_free >= self.cfg.storage_maintenance.min_free_bytes:
                    break
                count, size = self.delete_candidate(candidate)
                removed += count
                released += size
        else:
            for area in _AREAS:
                candidates = sorted(
                    inventory.candidates[area],
                    key=lambda item: (item.mtime, item.path.as_posix()),
                )
                remaining = list(candidates)
                for candidate in candidates:
                    if self._must_stop(cancelled, deadline):
                        break
                    retention = self._retention_seconds(candidate)
                    if retention <= 0 or self.now() - candidate.mtime <= retention:
                        continue
                    count, size = self.delete_candidate(candidate)
                    removed += count
                    released += size
                    if self._candidate_is_gone(candidate):
                        remaining.remove(candidate)

                maximum = self._max_bytes(area)
                remaining_size = sum(item.size for item in remaining)
                if maximum > 0 and remaining_size > maximum:
                    for candidate in remaining:
                        if self._must_stop(cancelled, deadline) or remaining_size <= maximum:
                            break
                        count, size = self.delete_candidate(candidate)
                        removed += count
                        released += size
                        if self._candidate_is_gone(candidate):
                            remaining_size -= candidate.size

        free_after = self._free_bytes(inventory.roots)
        return MaintenanceSummary(
            trigger=trigger,
            elapsed_seconds=max(0.0, self.monotonic() - started),
            scanned=inventory.stats.scanned,
            removed=removed,
            released_bytes=released,
            skipped_areas=inventory.skipped_areas,
            free_before=free_before,
            free_after=free_after,
        )

    def _free_bytes(self, roots: dict[str, ManagedRoot]) -> int | None:
        values: list[int] = []
        for root in roots.values():
            target = root.path
            while not target.exists() and target != target.parent:
                target = target.parent
            try:
                values.append(int(getattr(self.disk_usage(target), "free")))
            except (OSError, TypeError, ValueError, AttributeError):
                continue
        return min(values) if values else None

    @staticmethod
    def _all_candidates(inventory: InventoryResult) -> list[CleanupCandidate]:
        return [
            candidate
            for area in _AREAS
            for candidate in inventory.candidates.get(area, ())
        ]

    @staticmethod
    def _pressure_rank(candidate: CleanupCandidate) -> int:
        if candidate.area == "sandbox" and candidate.kind == "cache":
            return 0
        if candidate.area == "sandbox":
            return 1
        if candidate.area == "traces":
            return 2
        if candidate.kind in {"outgoing", "sending"}:
            return 3
        return 4

    def _retention_seconds(self, candidate: CleanupCandidate) -> int:
        storage = self.cfg.storage_maintenance
        if candidate.area == "sandbox":
            return storage.sandbox.retention_seconds
        if candidate.area == "traces":
            return storage.traces.retention_seconds
        if candidate.kind in {"outgoing", "sending"}:
            return storage.resources.transient_retention_seconds
        return storage.resources.retention_seconds

    def _max_bytes(self, area: str) -> int:
        storage = self.cfg.storage_maintenance
        if area == "sandbox":
            return storage.sandbox.max_bytes
        if area == "traces":
            return storage.traces.max_bytes
        return storage.resources.max_bytes

    def _must_stop(self, cancelled: threading.Event, deadline: float) -> bool:
        return cancelled.is_set() or self.monotonic() >= deadline

    @staticmethod
    def _candidate_is_gone(candidate: CleanupCandidate) -> bool:
        try:
            candidate.path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _resolve_roots(self) -> tuple[dict[str, ManagedRoot], int]:
        roots: dict[str, ManagedRoot] = {}
        skipped = 0
        workspace = self._configured_path(self._workspace_setting, self.cwd)

        sandbox_boundary = self.home / ".local" / "state" / "qq-agent-bridge"
        sandbox = self._configured_path(self._sandbox_setting, workspace)
        if self._strict_child(sandbox, sandbox_boundary) and self._safe_existing_chain(
            sandbox, self.home
        ):
            roots["sandbox"] = ManagedRoot("sandbox", sandbox, sandbox_boundary)
        else:
            skipped += 1

        traces = self._configured_path(self._trace_setting, self.cwd)
        if (
            traces != workspace
            and not self._is_relative_to(traces, workspace)
            and self._safe_existing_chain(traces, Path(traces.anchor))
        ):
            roots["traces"] = ManagedRoot("traces", traces, Path(traces.anchor))
        else:
            skipped += 1

        resources = self._configured_path(self._resource_setting, workspace)
        if self._strict_child(resources, workspace) and self._safe_existing_chain(
            resources, workspace
        ):
            roots["resources"] = ManagedRoot("resources", resources, workspace)
        else:
            skipped += 1
        return roots, skipped

    @staticmethod
    def _root_signature(cfg: BridgeConfig) -> tuple[str, str, str, str]:
        return (
            cfg.agent.default_workspace,
            cfg.agent.sandbox_home,
            cfg.agent.trace_root,
            cfg.resources.root,
        )

    def _root_signature_from_snapshot(self) -> tuple[str, str, str, str]:
        return (
            self._workspace_setting,
            self._sandbox_setting,
            self._trace_setting,
            self._resource_setting,
        )

    def _inventory_sandbox(
        self,
        root: ManagedRoot,
        output: list[CleanupCandidate],
        budget: _ScanBudget,
    ) -> None:
        for relative, kind in (
            ((".cache",), "cache"),
            ((".npm", "_cacache"), "cache"),
            ((".npm", "_logs"), "log"),
            ((".npm", "_npx"), "cache"),
        ):
            self._add_immediate(root, relative, kind, output, budget)

        chats = root.path / ".cursor" / "chats"
        for project in self._safe_entries(chats, budget):
            if self._entry_is_directory(project):
                self._add_immediate(
                    root,
                    Path(project.path).relative_to(root.path).parts,
                    "chat",
                    output,
                    budget,
                )

        projects = root.path / ".cursor" / "projects"
        for project in self._safe_entries(projects, budget):
            if not self._entry_is_directory(project):
                continue
            for entry in self._safe_entries(Path(project.path), budget):
                if entry.name.startswith("worker.log") and self._entry_is_regular(entry):
                    self._add_candidate(root, Path(entry.path), "log", output, budget)
                elif entry.name == "assets" and self._entry_is_directory(entry):
                    self._add_immediate(
                        root,
                        Path(entry.path).relative_to(root.path).parts,
                        "asset",
                        output,
                        budget,
                    )

    def _inventory_traces(
        self,
        root: ManagedRoot,
        output: list[CleanupCandidate],
        budget: _ScanBudget,
    ) -> None:
        for entry in self._safe_entries(root.path, budget):
            if entry.name.endswith(".jsonl") and self._entry_is_regular(entry):
                self._add_candidate(root, Path(entry.path), "trace", output, budget)

    def _inventory_resources(
        self,
        root: ManagedRoot,
        output: list[CleanupCandidate],
        budget: _ScanBudget,
    ) -> None:
        for entry in self._safe_entries(root.path, budget):
            if DATED_RESOURCE_DIR.fullmatch(entry.name) and self._entry_is_directory(entry):
                self._add_candidate(root, Path(entry.path), "received", output, budget)
        self._add_immediate(root, ("outgoing",), "outgoing", output, budget, directories_only=True)
        self._add_immediate(root, ("sending",), "sending", output, budget, directories_only=True)

    def _add_immediate(
        self,
        root: ManagedRoot,
        relative: tuple[str, ...],
        kind: str,
        output: list[CleanupCandidate],
        budget: _ScanBudget,
        *,
        directories_only: bool = False,
    ) -> None:
        parent = root.path.joinpath(*relative)
        if not self._safe_existing_chain(parent, root.path):
            return
        for entry in self._safe_entries(parent, budget):
            if directories_only and not self._entry_is_directory(entry):
                continue
            self._add_candidate(root, Path(entry.path), kind, output, budget)

    def _add_candidate(
        self,
        root: ManagedRoot,
        path: Path,
        kind: str,
        output: list[CleanupCandidate],
        budget: _ScanBudget,
    ) -> None:
        if self.is_protected(path):
            return
        measured = self._measure(path, budget, count_root=False)
        if measured is None:
            budget.skipped += 1
            return
        size, entries, root_stat = measured
        try:
            relative = path.relative_to(root.path).parts
        except ValueError:
            return
        output.append(
            CleanupCandidate(
                path=path,
                area=root.area,
                kind=kind,
                size=size,
                mtime=root_stat.st_mtime,
                root=root.path,
                relative_parts=relative,
                device=root_stat.st_dev,
                inode=root_stat.st_ino,
                mode=root_stat.st_mode,
                entries=entries,
            )
        )

    def _measure(
        self,
        path: Path,
        budget: _ScanBudget,
        *,
        count_root: bool,
    ) -> tuple[int, int, os.stat_result] | None:
        if count_root and not budget.take():
            return None
        if budget.monotonic() >= budget.deadline:
            budget.exhausted = True
            return None
        try:
            root_stat = path.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(root_stat.st_mode):
            return None
        if stat.S_ISREG(root_stat.st_mode):
            return (root_stat.st_size, 1, root_stat)
        if not stat.S_ISDIR(root_stat.st_mode):
            return None

        total = 0
        entries = 1
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    child = self._measure(Path(entry.path), budget, count_root=True)
                    if child is None:
                        return None
                    total += child[0]
                    entries += child[1]
        except OSError:
            return None
        return (total, entries, root_stat)

    def _safe_entries(self, path: Path, budget: _ScanBudget) -> list[os.DirEntry[str]]:
        if not budget.available():
            return []
        try:
            path_stat = path.lstat()
            if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
                return []
            with os.scandir(path) as iterator:
                entries: list[os.DirEntry[str]] = []
                for entry in iterator:
                    if not budget.take():
                        break
                    entries.append(entry)
                return entries
        except OSError:
            return []

    def _delete_entry(
        self,
        parent_fd: int,
        name: str,
        *,
        expected: tuple[int, int, int] | None = None,
    ) -> tuple[int, int, bool]:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return (0, 0, False)
        if expected is not None and (
            current.st_dev != expected[0]
            or current.st_ino != expected[1]
            or stat.S_IFMT(current.st_mode) != stat.S_IFMT(expected[2])
        ):
            return (0, 0, False)
        if stat.S_ISLNK(current.st_mode):
            return (0, 0, False)
        if stat.S_ISREG(current.st_mode):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                return (0, 0, False)
            return (1, current.st_size, True)
        if not stat.S_ISDIR(current.st_mode):
            return (0, 0, False)

        child_fd = -1
        removed = 0
        released = 0
        complete = True
        try:
            child_fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(child_fd)
            if opened.st_dev != current.st_dev or opened.st_ino != current.st_ino:
                return (0, 0, False)
            for child_name in os.listdir(child_fd):
                child_removed, child_released, child_complete = self._delete_entry(
                    child_fd,
                    child_name,
                )
                removed += child_removed
                released += child_released
                complete = complete and child_complete
        except OSError:
            return (removed, released, False)
        finally:
            if child_fd >= 0:
                os.close(child_fd)
        if not complete:
            return (removed, released, False)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            return (removed, released, False)
        return (removed + 1, released, True)

    def _configured_path(self, configured: str, relative_to: Path) -> Path:
        raw = configured or "."
        if raw == "~":
            path = self.home
        elif raw.startswith("~/"):
            path = self.home / raw[2:]
        else:
            path = Path(raw)
        if not path.is_absolute():
            path = relative_to / path
        return Path(os.path.abspath(path))

    @staticmethod
    def _entry_is_directory(entry: os.DirEntry[str]) -> bool:
        try:
            return entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
        except OSError:
            return False

    @staticmethod
    def _entry_is_regular(entry: os.DirEntry[str]) -> bool:
        try:
            return entry.is_file(follow_symlinks=False) and not entry.is_symlink()
        except OSError:
            return False

    @classmethod
    def _strict_child(cls, path: Path, boundary: Path) -> bool:
        return path != boundary and cls._is_relative_to(path, boundary)

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @classmethod
    def _safe_existing_chain(cls, path: Path, boundary: Path) -> bool:
        if path != boundary and not cls._is_relative_to(path, boundary):
            return False
        current = boundary
        try:
            boundary_stat = boundary.lstat()
            if stat.S_ISLNK(boundary_stat.st_mode):
                return False
        except FileNotFoundError:
            return True
        except OSError:
            return False
        for component in path.relative_to(boundary).parts:
            current = current / component
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                return True
            except OSError:
                return False
            if stat.S_ISLNK(current_stat.st_mode):
                return False
        return True


@asynccontextmanager
async def _null_lease():
    yield
