"""Bounded, symlink-safe maintenance of application-owned storage."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable

from .config import BridgeConfig


MAX_CANDIDATES_PER_AREA = 100_000
RUN_BUDGET_SECONDS = 30.0
DATED_RESOURCE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AREAS = ("sandbox", "traces", "resources")
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.gate = gate
        self.home = (home or Path.home()).absolute()
        self.cwd = (cwd or Path.cwd()).absolute()
        self.monotonic = monotonic

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
            removed, released, _complete = self._delete_entry(parent_fd, name)
            return (removed, released)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            return (0, 0)
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            if root_fd >= 0:
                os.close(root_fd)

    def _resolve_roots(self) -> tuple[dict[str, ManagedRoot], int]:
        roots: dict[str, ManagedRoot] = {}
        skipped = 0
        workspace = self._configured_path(self.cfg.agent.default_workspace, self.cwd)

        sandbox_boundary = self.home / ".local" / "state" / "qq-agent-bridge"
        sandbox = self._configured_path(self.cfg.agent.sandbox_home, workspace)
        if self._strict_child(sandbox, sandbox_boundary) and self._safe_existing_chain(
            sandbox, self.home
        ):
            roots["sandbox"] = ManagedRoot("sandbox", sandbox, sandbox_boundary)
        else:
            skipped += 1

        traces = self._configured_path(self.cfg.agent.trace_root, self.cwd)
        if (
            traces != workspace
            and not self._is_relative_to(traces, workspace)
            and self._safe_existing_chain(traces, Path(traces.anchor))
        ):
            roots["traces"] = ManagedRoot("traces", traces, Path(traces.anchor))
        else:
            skipped += 1

        resources = self._configured_path(self.cfg.resources.root, workspace)
        if self._strict_child(resources, workspace) and self._safe_existing_chain(
            resources, workspace
        ):
            roots["resources"] = ManagedRoot("resources", resources, workspace)
        else:
            skipped += 1
        return roots, skipped

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

    def _delete_entry(self, parent_fd: int, name: str) -> tuple[int, int, bool]:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
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
                    child_fd, child_name
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
