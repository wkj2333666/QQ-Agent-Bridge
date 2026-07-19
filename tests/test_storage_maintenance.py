"""Safe storage maintenance inventory and deletion tests."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.storage_gate import StorageActivityGate
from qq_agent_bridge.storage_maintenance import StorageMaintainer
from qq_agent_bridge.storage_maintenance import MaintenanceSummary


def make_storage_cfg(tmp_path: Path) -> tuple[BridgeConfig, Path]:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    traces = tmp_path / "traces"
    home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    traces.mkdir(parents=True)

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


def test_delete_candidate_rechecks_identity_at_recursive_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    target = _write(Path(cfg.agent.trace_root) / "old.jsonl", b"old")
    maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
    candidate = maintainer.inventory(now=2_000_000.0).candidates["traces"][0]
    real_stat = os.stat
    checks = 0

    def racing_stat(path, *args, **kwargs):
        nonlocal checks
        if path == target.name and kwargs.get("dir_fd") is not None:
            checks += 1
            if checks == 2:
                target.unlink()
                target.write_bytes(b"replacement")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("qq_agent_bridge.storage_maintenance.os.stat", racing_stat)

    removed, released = maintainer.delete_candidate(candidate)

    assert (removed, released) == (0, 0)
    assert target.read_bytes() == b"replacement"


def test_protected_current_job_is_never_inventoried_or_deleted(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    job_dir = Path(cfg.agent.default_workspace) / cfg.resources.root / "outgoing" / "active"
    _write(job_dir / "result.pdf", b"active")
    maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
    maintainer.protect_path(job_dir)

    assert maintainer.is_protected(job_dir)
    assert maintainer.inventory().candidates["resources"] == []

    maintainer.unprotect_path(job_dir)
    candidate = maintainer.inventory().candidates["resources"][0]
    maintainer.protect_path(job_dir)
    assert maintainer.delete_candidate(candidate) == (0, 0)
    assert job_dir.exists()


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


def _summary(trigger: str) -> MaintenanceSummary:
    return MaintenanceSummary(trigger, 0.0, 0, 0, 0, 0, 100, 100)


def test_start_runs_startup_cleanup_before_returning(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        cfg, home = make_storage_cfg(tmp_path)
        maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
        calls: list[str] = []

        async def fake_run(trigger):
            calls.append(trigger)
            return _summary(trigger)

        maintainer.run = fake_run
        await maintainer.start()
        assert calls == ["startup"]
        assert maintainer.loop_task is not None
        await maintainer.stop()
        assert maintainer.loop_task is None

    asyncio.run(go())


def test_periodic_loop_runs_after_configured_interval(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        cfg, home = make_storage_cfg(tmp_path)
        cfg.storage_maintenance.interval_seconds = 0.01
        maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
        periodic = asyncio.Event()

        async def fake_run(trigger):
            if trigger == "periodic":
                periodic.set()
            return _summary(trigger)

        maintainer.run = fake_run
        await maintainer.start()
        await asyncio.wait_for(periodic.wait(), 0.5)
        await maintainer.stop()

    asyncio.run(go())


def test_repeated_pressure_requests_coalesce(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        cfg, home = make_storage_cfg(tmp_path)
        maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
        pressure_done = asyncio.Event()
        calls: list[str] = []

        async def fake_run(trigger):
            calls.append(trigger)
            if trigger == "pressure":
                pressure_done.set()
            return _summary(trigger)

        maintainer.run = fake_run
        maintainer.pressure_needed = lambda: True
        await maintainer.start()
        maintainer.request_pressure_check()
        maintainer.request_pressure_check()
        maintainer.request_pressure_check()
        await asyncio.wait_for(pressure_done.wait(), 0.5)
        await asyncio.sleep(0)
        await maintainer.stop()
        assert calls.count("pressure") == 1

    asyncio.run(go())


def test_reload_updates_policy_but_preserves_managed_roots(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        cfg, home = make_storage_cfg(tmp_path)
        maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
        original_roots = maintainer.inventory().roots
        updated, _ = make_storage_cfg(tmp_path / "new")
        updated.storage_maintenance.interval_seconds = 123
        updated.storage_maintenance.traces.max_bytes = 99

        restart_required = maintainer.reload_config(updated)

        assert restart_required
        assert maintainer.cfg.storage_maintenance.traces.max_bytes == 99
        assert maintainer.inventory().roots == original_roots
        assert maintainer.wake_event.is_set()

    asyncio.run(go())


def test_disabled_maintenance_does_not_run_or_create_loop(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        cfg, home = make_storage_cfg(tmp_path)
        cfg.storage_maintenance.enabled = False
        maintainer = StorageMaintainer(cfg, home=home, cwd=tmp_path)
        calls: list[str] = []

        async def fake_run(trigger):
            calls.append(trigger)
            return _summary(trigger)

        maintainer.run = fake_run
        await maintainer.start()
        maintainer.request_pressure_check()
        await asyncio.sleep(0)
        assert calls == []
        assert maintainer.loop_task is None
        await maintainer.stop()

    asyncio.run(go())


def test_pressure_check_only_uses_disk_usage(tmp_path: Path) -> None:
    cfg, home = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.min_free_bytes = 10
    calls: list[Path] = []

    def disk_usage(path: Path) -> SimpleNamespace:
        calls.append(path)
        return SimpleNamespace(free=0)

    maintainer = StorageMaintainer(
        cfg,
        home=home,
        cwd=tmp_path,
        disk_usage=disk_usage,
    )

    assert maintainer.pressure_needed()
    assert 1 <= len(calls) <= 3


def test_cancelled_run_waits_for_worker_exit_before_releasing_gate(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        cfg, home = make_storage_cfg(tmp_path)
        gate = StorageActivityGate()
        worker_started = asyncio.Event()
        worker_stopped = asyncio.Event()

        async def controlled_runner(_function, trigger, cancelled):
            worker_started.set()
            while not cancelled.is_set():
                await asyncio.sleep(0)
            worker_stopped.set()
            return _summary(trigger)

        maintainer = StorageMaintainer(
            cfg,
            gate,
            home=home,
            cwd=tmp_path,
            thread_runner=controlled_runner,
        )
        task = asyncio.create_task(maintainer.run("periodic"))
        await worker_started.wait()
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)

        assert isinstance(result[0], asyncio.CancelledError)
        assert worker_stopped.is_set()
        async with asyncio.timeout(0.2):
            async with gate.activity():
                assert gate.active_count == 1

    asyncio.run(go())
