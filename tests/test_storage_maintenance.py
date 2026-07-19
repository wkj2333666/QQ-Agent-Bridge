"""Safe storage maintenance inventory and deletion tests."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

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


def _mtime(path: Path, value: float) -> Path:
    os.utime(path, (value, value))
    return path


def _run(maintainer: StorageMaintainer, trigger: str = "periodic"):
    import asyncio

    async def inline(function, *args):
        return function(*args)

    maintainer.thread_runner = inline
    return asyncio.run(maintainer.run(trigger))


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


def test_normal_cleanup_applies_retention_then_oldest_first_cap(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.traces.retention_seconds = 100
    cfg.storage_maintenance.traces.max_bytes = 5
    traces = Path(cfg.agent.trace_root)
    old = _mtime(_write(traces / "old.jsonl", b"111"), 800)
    first = _mtime(_write(traces / "first.jsonl", b"2222"), 950)
    latest = _mtime(_write(traces / "latest.jsonl", b"33"), 990)

    summary = _run(
        StorageMaintainer(cfg, home=home, cwd=tmp_path, now=lambda: 1_000.0)
    )

    assert not old.exists()
    assert not first.exists()
    assert latest.exists()
    assert summary.released_bytes == 7


def test_resource_retention_distinguishes_received_and_transient(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.resources.retention_seconds = 700
    cfg.storage_maintenance.resources.transient_retention_seconds = 100
    cfg.storage_maintenance.resources.max_bytes = 0
    root = Path(cfg.agent.default_workspace) / cfg.resources.root
    received_old = _write(root / "2026-01-01" / "event" / "image.jpg")
    outgoing_old = _write(root / "outgoing" / "old-job" / "result.pdf")
    outgoing_new = _write(root / "outgoing" / "new-job" / "result.pdf")
    for path, mtime in ((received_old.parents[1], 200), (outgoing_old.parent, 850), (outgoing_new.parent, 950)):
        _mtime(path, mtime)

    _run(StorageMaintainer(cfg, home=home, cwd=tmp_path, now=lambda: 1_000.0))

    assert not received_old.exists()
    assert not outgoing_old.exists()
    assert outgoing_new.exists()


def test_pressure_cleanup_uses_fixed_order_and_stops_at_threshold(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.min_free_bytes = 10
    sandbox = Path(cfg.agent.sandbox_home)
    cache = _write(sandbox / ".cache" / "cache-item" / "payload", b"123")
    chat = _write(sandbox / ".cursor" / "chats" / "project" / "chat" / "state", b"456")
    trace = _write(Path(cfg.agent.trace_root) / "trace.jsonl", b"789")

    def disk_usage(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(free=20 if not cache.exists() else 0)

    summary = _run(
        StorageMaintainer(
            cfg,
            home=home,
            cwd=tmp_path,
            now=lambda: 1_000.0,
            disk_usage=disk_usage,
        ),
        "pressure",
    )

    assert not cache.exists()
    assert chat.exists()
    assert trace.exists()
    assert summary.free_before == 0
    assert summary.free_after == 20


def test_cleanup_preserves_auth_unknown_entries_and_runtime_skill_bundle(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.min_free_bytes = 10
    sandbox = Path(cfg.agent.sandbox_home)
    protected = _write(sandbox / ".config" / "cursor" / "auth.json", b"secret")
    cursor_config = _write(sandbox / ".cursor" / "cli-config.json", b"config")
    unknown = _write(sandbox / "unknown.data", b"unknown")
    root = Path(cfg.agent.default_workspace) / cfg.resources.root
    runtime_skill = _write(root / "runtime-skills" / "skill.md", b"skill")
    resource_unknown = _write(root / "misc" / "keep.txt", b"keep")

    _run(
        StorageMaintainer(
            cfg,
            home=home,
            cwd=tmp_path,
            disk_usage=lambda _path: SimpleNamespace(free=0),
        ),
        "pressure",
    )

    assert protected.exists()
    assert cursor_config.exists()
    assert unknown.exists()
    assert runtime_skill.exists()
    assert resource_unknown.exists()


def test_cleanup_continues_after_individual_deletion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.traces.retention_seconds = 1
    cfg.storage_maintenance.traces.max_bytes = 0
    traces = Path(cfg.agent.trace_root)
    first = _mtime(_write(traces / "first.jsonl"), 1)
    second = _mtime(_write(traces / "second.jsonl"), 2)
    maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path, now=lambda: 100.0)
    original = maintainer.delete_candidate

    def flaky(candidate):
        if candidate.path == first:
            return (0, 0)
        return original(candidate)

    monkeypatch.setattr(maintainer, "delete_candidate", flaky)
    summary = _run(maintainer)

    assert first.exists()
    assert not second.exists()
    assert summary.removed == 1


def test_missing_roots_are_empty_not_errors(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    Path(cfg.agent.trace_root).rmdir()

    summary = _run(StorageMaintainer(cfg, home=home, cwd=tmp_path))

    assert summary.removed == 0
    assert summary.skipped_areas == 0


def test_storage_logs_do_not_include_candidate_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.traces.retention_seconds = 1
    secret_name = "qq-user-secret-name.jsonl"
    _mtime(_write(Path(cfg.agent.trace_root) / secret_name), 1)

    with caplog.at_level("INFO", logger="qq_agent_bridge.storage_maintenance"):
        _run(StorageMaintainer(cfg, home=home, cwd=tmp_path, now=lambda: 100.0))

    assert "storage maintenance start trigger=periodic" in caplog.text
    assert "storage maintenance done trigger=periodic" in caplog.text
    assert secret_name not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_unresolved_pressure_warning_is_rate_limited(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.min_free_bytes = 10
    clock = [100.0]
    maintainer = StorageMaintainer(
        cfg,
        home=home,
        cwd=tmp_path,
        monotonic=lambda: clock[0],
        disk_usage=lambda _path: SimpleNamespace(free=0),
    )

    with caplog.at_level("WARNING", logger="qq_agent_bridge.storage_maintenance"):
        _run(maintainer, "pressure")
        _run(maintainer, "pressure")
        clock[0] += 3_601
        _run(maintainer, "pressure")

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 2
    assert all("free_bytes=0" in record.getMessage() for record in warnings)
