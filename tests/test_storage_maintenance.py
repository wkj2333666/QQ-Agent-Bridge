"""Safe storage maintenance inventory and deletion tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.storage_maintenance import StorageMaintainer


def make_storage_cfg(tmp_path: Path) -> tuple[BridgeConfig, Path]:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    traces = tmp_path / "traces"
    home.mkdir()
    workspace.mkdir()
    traces.mkdir()

    cfg = BridgeConfig()
    cfg.agent.default_workspace = str(workspace)
    cfg.agent.sandbox_home = str(
        home / ".local" / "state" / "qq-agent-bridge" / "cursor-home"
    )
    cfg.agent.trace_root = str(traces)
    cfg.resources.root = "downloads/qq-agent-bridge"
    cfg.workspaces = {str(workspace): True}
    return cfg, home


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _candidate_names(maintainer: StorageMaintainer) -> set[tuple[str, str, str]]:
    result = maintainer.inventory(now=2_000_000.0)
    return {
        (candidate.area, candidate.kind, candidate.path.name)
        for candidates in result.candidates.values()
        for candidate in candidates
    }


def test_inventory_selects_only_allowlisted_entries(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    sandbox = Path(cfg.agent.sandbox_home)
    traces = Path(cfg.agent.trace_root)
    resources = Path(cfg.agent.default_workspace) / cfg.resources.root

    _write(sandbox / ".cache" / "old-cache" / "payload")
    _write(sandbox / ".npm" / "_cacache" / "content-v2" / "payload")
    _write(sandbox / ".npm" / "_logs" / "old.log")
    _write(sandbox / ".npm" / "_npx" / "session" / "payload")
    _write(sandbox / ".cursor" / "chats" / "project" / "chat" / "state")
    _write(sandbox / ".cursor" / "projects" / "project" / "worker.log.1")
    _write(sandbox / ".cursor" / "projects" / "project" / "assets" / "asset")
    protected = _write(sandbox / ".cursor" / "auth.json", b"secret")
    unknown = _write(sandbox / "unknown.data")

    _write(traces / "old.jsonl")
    _write(traces / "ignore.log")
    _write(resources / "2026-01-01" / "event" / "received.jpg")
    _write(resources / "outgoing" / "job-old" / "result.pdf")
    _write(resources / "sending" / "job-send" / "result.pdf")
    runtime_skill = _write(resources / "runtime-skills" / "skill.md")
    resource_unknown = _write(resources / "misc" / "keep.txt")

    names = _candidate_names(StorageMaintainer(cfg, home=home, cwd=tmp_path))

    assert ("sandbox", "cache", "old-cache") in names
    assert ("sandbox", "cache", "content-v2") in names
    assert ("sandbox", "log", "old.log") in names
    assert ("sandbox", "cache", "session") in names
    assert ("sandbox", "chat", "chat") in names
    assert ("sandbox", "log", "worker.log.1") in names
    assert ("sandbox", "asset", "asset") in names
    assert ("traces", "trace", "old.jsonl") in names
    assert ("resources", "received", "2026-01-01") in names
    assert ("resources", "outgoing", "job-old") in names
    assert ("resources", "sending", "job-send") in names
    assert protected.exists()
    assert unknown.exists()
    assert runtime_skill.exists()
    assert resource_unknown.exists()
    assert all(name not in {"auth.json", "unknown.data", "skill.md", "misc"} for _, _, name in names)


def test_inventory_rejects_symlinked_root(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    traces = Path(cfg.agent.trace_root)
    traces.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write(outside / "old.jsonl", b"keep")
    traces.symlink_to(outside, target_is_directory=True)

    result = StorageMaintainer(cfg, home=home, cwd=tmp_path).inventory(now=2_000_000.0)

    assert result.candidates["traces"] == []
    assert result.skipped_areas == 1
    assert secret.read_bytes() == b"keep"


def test_inventory_does_not_follow_symlink_inside_candidate(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write(outside / "secret.txt", b"keep")
    candidate = Path(cfg.agent.sandbox_home) / ".cache" / "unsafe"
    candidate.mkdir(parents=True)
    (candidate / "link").symlink_to(outside, target_is_directory=True)

    result = StorageMaintainer(cfg, home=home, cwd=tmp_path).inventory(now=2_000_000.0)

    assert result.candidates["sandbox"] == []
    assert result.stats.skipped >= 1
    assert secret.read_bytes() == b"keep"


def test_inventory_honors_candidate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    traces = Path(cfg.agent.trace_root)
    for index in range(4):
        _write(traces / f"{index}.jsonl")
    monkeypatch.setattr("qq_agent_bridge.storage_maintenance.MAX_CANDIDATES_PER_AREA", 2)

    result = StorageMaintainer(cfg, home=home, cwd=tmp_path).inventory(now=2_000_000.0)

    assert len(result.candidates["traces"]) == 2
    assert result.stats.skipped >= 1


def test_inventory_stops_after_deadline(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    _write(Path(cfg.agent.trace_root) / "old.jsonl")

    result = StorageMaintainer(
        cfg,
        home=home,
        cwd=tmp_path,
        monotonic=lambda: 10.0,
    ).inventory(now=2_000_000.0, deadline=9.0)

    assert all(not candidates for candidates in result.candidates.values())
    assert result.stats.skipped >= 1


def test_delete_candidate_removes_regular_tree(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    target = Path(cfg.agent.sandbox_home) / ".cache" / "old-cache"
    _write(target / "nested" / "payload", b"1234")
    maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
    candidate = maintainer.inventory(now=2_000_000.0).candidates["sandbox"][0]

    removed, released = maintainer.delete_candidate(candidate)

    assert removed == 3
    assert released == 4
    assert not target.exists()


def test_delete_candidate_rejects_inode_replacement(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    target = _write(Path(cfg.agent.trace_root) / "old.jsonl", b"old")
    maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
    candidate = maintainer.inventory(now=2_000_000.0).candidates["traces"][0]
    target.unlink()
    target.write_bytes(b"replacement")
    assert os.lstat(target).st_ino != candidate.inode

    removed, released = maintainer.delete_candidate(candidate)

    assert (removed, released) == (0, 0)
    assert target.read_bytes() == b"replacement"
