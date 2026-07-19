# Built-in Storage Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded in-process cleanup for the Agent sandbox, Agent traces, and QQ resource storage without racing active Agent work.

**Architecture:** A re-entrant asynchronous `StorageActivityGate` separates normal Agent/resource activity from exclusive maintenance. A focused `StorageMaintainer` validates application-owned roots, inventories only allowlisted candidates, applies age/capacity/pressure policies, and exposes startup, periodic, reload, and shutdown hooks consumed by `App`.

**Tech Stack:** Python 3.13, asyncio, dataclasses, pathlib/os dir-fd filesystem APIs, PyYAML, pytest, uv.

## Global Constraints

- Do not depend on cron, systemd, or deployment shell scripts.
- Manage only `agent.sandbox_home`, `agent.trace_root`, and the configured QQ resource root.
- Never follow symbolic links or delete unknown top-level entries.
- Never clean mutable Agent/resource state concurrently with active work.
- Never terminate jobs to reclaim storage.
- Default interval is 21,600 seconds; minimum free space is 5 GiB.
- Default caps are 2 GiB sandbox, 512 MiB traces, and 5 GiB resources.
- Default retention is 14 days for sandbox/traces, 7 days for received resources, and 24 hours for outgoing/sending.
- Scan at most 100,000 candidates per area and spend at most 30 seconds per maintenance run.
- Logs must not contain QQ text, prompts, authentication values, candidate filenames, or full user-controlled paths.
- Use TDD for every behavior and keep each task independently reviewable.

## File Structure

- Create `src/qq_agent_bridge/storage_gate.py`: re-entrant shared activity lease, exclusive maintenance lease, and Agent adapter wrapper.
- Create `src/qq_agent_bridge/storage_maintenance.py`: safe inventory, deletion, retention/capacity policy, pressure checks, and periodic orchestration.
- Create `tests/test_storage_gate.py`: concurrency, re-entrancy, fairness, and cancellation tests.
- Create `tests/test_storage_maintenance.py`: root safety, cleanup policies, bounds, pressure, orchestration, and logging tests.
- Modify `src/qq_agent_bridge/config.py`: storage maintenance dataclasses, defaults, clamps, and `BridgeConfig` integration.
- Modify `src/qq_agent_bridge/main.py`: construct components, lease Agent/resource work, trigger maintenance, reload policy, and stop cleanly.
- Modify `tests/test_config.py`: default/example/load/clamp tests.
- Modify `tests/test_app_async.py`: App lifecycle and active-job integration tests.
- Modify `config.yaml` and `config.example.yaml`: enable balanced defaults.
- Modify `README.md` and `README.zh-CN.md`: document storage policy and managed roots.

---

### Task 1: Configuration Model and Safe Defaults

**Files:**
- Modify: `src/qq_agent_bridge/config.py`
- Modify: `tests/test_config.py`
- Modify: `config.example.yaml`
- Modify: `config.yaml`

**Interfaces:**
- Produces: `StorageAreaMaintenanceConfig`, `StorageResourceMaintenanceConfig`, `StorageMaintenanceConfig`.
- Produces: `BridgeConfig.storage_maintenance: StorageMaintenanceConfig`.
- Consumers: `StorageMaintainer` in Task 4 and `App._reload_config()` in Task 6.

- [ ] **Step 1: Write failing default and load/clamp tests**

Add these exact tests:

```python
def test_storage_maintenance_defaults_are_balanced() -> None:
    cfg = BridgeConfig()
    storage = cfg.storage_maintenance
    assert storage.enabled
    assert storage.interval_seconds == 21_600
    assert storage.min_free_bytes == 5 * 1024**3
    assert storage.sandbox.max_bytes == 2 * 1024**3
    assert storage.sandbox.retention_seconds == 14 * 86_400
    assert storage.traces.max_bytes == 512 * 1024**2
    assert storage.resources.max_bytes == 5 * 1024**3
    assert storage.resources.retention_seconds == 7 * 86_400
    assert storage.resources.transient_retention_seconds == 86_400


def test_storage_maintenance_loads_and_clamps_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
storage_maintenance:
  enabled: true
  interval_seconds: 1
  min_free_bytes: 99999999999999
  sandbox: {max_bytes: -1, retention_seconds: 999999999}
  traces: {max_bytes: 1234, retention_seconds: 0}
  resources:
    max_bytes: 5678
    retention_seconds: 9
    transient_retention_seconds: 10
""",
        encoding="utf-8",
    )
    cfg = BridgeConfig.load(path)
    assert cfg.storage_maintenance.interval_seconds == 60
    assert cfg.storage_maintenance.min_free_bytes == 1024**4
    assert cfg.storage_maintenance.sandbox.max_bytes == 0
    assert cfg.storage_maintenance.sandbox.retention_seconds == 365 * 86_400
    assert cfg.storage_maintenance.traces.retention_seconds == 0
    assert cfg.storage_maintenance.resources.transient_retention_seconds == 10
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_config.py -k storage_maintenance
```

Expected: FAIL because `BridgeConfig` has no `storage_maintenance` field.

- [ ] **Step 3: Implement dataclasses and bounded loading**

Add these dataclasses near the other configuration units:

```python
@dataclass
class StorageAreaMaintenanceConfig:
    max_bytes: int
    retention_seconds: int


@dataclass
class StorageResourceMaintenanceConfig(StorageAreaMaintenanceConfig):
    transient_retention_seconds: int = 86_400


@dataclass
class StorageMaintenanceConfig:
    enabled: bool = True
    interval_seconds: int = 21_600
    min_free_bytes: int = 5 * 1024**3
    sandbox: StorageAreaMaintenanceConfig = field(
        default_factory=lambda: StorageAreaMaintenanceConfig(2 * 1024**3, 14 * 86_400)
    )
    traces: StorageAreaMaintenanceConfig = field(
        default_factory=lambda: StorageAreaMaintenanceConfig(512 * 1024**2, 14 * 86_400)
    )
    resources: StorageResourceMaintenanceConfig = field(
        default_factory=lambda: StorageResourceMaintenanceConfig(
            5 * 1024**3, 7 * 86_400, 86_400
        )
    )
```

Add `storage_maintenance` to `BridgeConfig`, parse nested mappings explicitly, and clamp with:

```python
def _bounded_int(value: object, default: int, lower: int, upper: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(upper, max(lower, int(number)))
```

Use bounds `60..604800` for interval, `0..1024**4` for byte values, and `0..365*86400` for retention. Non-mapping nested values fall back to their dataclass defaults.

Add the exact balanced YAML block from the design to both configuration files.

- [ ] **Step 4: Run config tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_config.py
```

Expected: all config tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/config.py tests/test_config.py config.example.yaml
git commit -m "feat: configure bounded storage maintenance"
```

Keep the deployment's ignored `config.yaml` local; never force-add it because it may contain credentials.

---

### Task 2: Re-entrant Activity and Maintenance Gate

**Files:**
- Create: `src/qq_agent_bridge/storage_gate.py`
- Create: `tests/test_storage_gate.py`

**Interfaces:**
- Produces: `StorageActivityGate.activity() -> AsyncContextManager[None]`.
- Produces: `StorageActivityGate.maintenance() -> AsyncContextManager[None]`.
- Produces: `GatedAgentAdapter(delegate: Any, gate: StorageActivityGate)` with `run(prompt: str, workspace: str | None = None, mode: str = "ask", model: str | None = None, progress: ProgressCallback | None = None, trace_id: str | None = None, redact_extra: tuple[str, ...] | None = None) -> str` and a `cfg` proxy.
- Consumers: `StorageMaintainer.run()` and `App` in Tasks 5-6.

- [ ] **Step 1: Write failing mutual-exclusion and re-entrancy tests**

Cover all of these behaviors:

```python
def test_maintenance_waits_for_activity_and_blocks_new_activity() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        first_entered = asyncio.Event()
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
            async with gate.maintenance():
                order.append("maintenance")

        async def second() -> None:
            await first_entered.wait()
            await asyncio.sleep(0)
            async with gate.activity():
                order.append("second")

        tasks = [asyncio.create_task(first()), asyncio.create_task(maintain())]
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(second()))
        release_first.set()
        await asyncio.gather(*tasks)
        assert order == ["first-enter", "first-exit", "maintenance", "second"]

    asyncio.run(go())


def test_activity_is_reentrant_in_same_task() -> None:
    async def go() -> None:
        gate = StorageActivityGate()
        async with gate.activity():
            async with gate.activity():
                assert gate.active_count == 1
    asyncio.run(go())
```

Also test cancellation while waiting for maintenance and cancellation inside either lease; the next waiter must still enter.

- [ ] **Step 2: Run gate tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_gate.py
```

Expected: collection FAIL because `storage_gate` does not exist.

- [ ] **Step 3: Implement the gate and Agent wrapper**

Use an `asyncio.Condition`, a maintenance waiter count for maintenance priority, and a task-local `ContextVar[int]` depth. The public shape must be:

```python
class StorageActivityGate:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_count = 0
        self._maintenance_active = False
        self._maintenance_waiters = 0
        self._depth: ContextVar[int] = ContextVar("storage_activity_depth", default=0)

    @property
    def active_count(self) -> int:
        return self._active_count

    @asynccontextmanager
    async def activity(self):
        depth = self._depth.get()
        if depth:
            token = self._depth.set(depth + 1)
            try:
                yield
            finally:
                self._depth.reset(token)
            return
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._maintenance_active and self._maintenance_waiters == 0
            )
            self._active_count += 1
        token = self._depth.set(1)
        try:
            yield
        finally:
            self._depth.reset(token)
            async with self._condition:
                self._active_count -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def maintenance(self):
        async with self._condition:
            self._maintenance_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._maintenance_active and self._active_count == 0
                )
                self._maintenance_active = True
            finally:
                self._maintenance_waiters -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._maintenance_active = False
                self._condition.notify_all()
```

Implement `GatedAgentAdapter.run()` with the same keyword arguments as `DisabledAgentAdapter.run()`. Inside `async with gate.activity()`, delegate through `agent_runtime.run_agent()` so custom and old test adapters remain compatible. Expose `cfg` from the delegate.

- [ ] **Step 4: Run gate tests and the existing runtime tests**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_gate.py tests/test_agent_runtime.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/storage_gate.py tests/test_storage_gate.py
git commit -m "feat: coordinate agent activity with maintenance"
```

---

### Task 3: Safe Candidate Inventory and Dir-fd Deletion

**Files:**
- Create: `src/qq_agent_bridge/storage_maintenance.py`
- Create: `tests/test_storage_maintenance.py`

**Interfaces:**
- Produces: `CleanupCandidate(path: Path, area: str, kind: str, size: int, mtime: float)`.
- Produces: `CleanupStats(scanned, removed, released_bytes, skipped)`.
- Produces internal `_inventory(cfg, now, deadline) -> dict[str, list[CleanupCandidate]]`.
- Produces internal `_delete_candidate(candidate, validated_root) -> tuple[int, int]`.
- Consumers: cleanup policies in Task 4.

- [ ] **Step 1: Write failing root and candidate safety tests**

Create fixtures with a sandbox, trace root, workspace resource root, protected files, unknown files, and symlinks. Assert:

```python
def test_inventory_selects_only_allowlisted_entries(tmp_path: Path) -> None:
    cfg = make_storage_cfg(tmp_path)
    roots = create_managed_tree(cfg)
    inventory = inventory_for_test(cfg, now=2_000_000.0)
    names = {(item.area, item.kind, item.path.name) for items in inventory.values() for item in items}
    assert ("traces", "trace", "old.jsonl") in names
    assert ("resources", "received", "2026-01-01") in names
    assert ("resources", "outgoing", "job-old") in names
    assert all("auth.json" not in item for item in names)
    assert all("unknown" not in item for item in names)


def test_inventory_rejects_symlinked_root_and_does_not_follow_candidate_symlink(tmp_path: Path) -> None:
    cfg = make_storage_cfg(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep", encoding="utf-8")
    Path(cfg.agent.trace_root).symlink_to(outside, target_is_directory=True)
    summary = run_sync_maintenance_for_test(cfg)
    assert secret.read_text(encoding="utf-8") == "keep"
    assert summary.skipped_areas >= 1
```

Also test candidate-count exhaustion at exactly 100,000 entries using an injected scanner fixture rather than creating 100,000 files, and test a passed deadline stops inventory safely.

- [ ] **Step 2: Run safety tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_maintenance.py -k 'inventory or symlink or candidate_limit or deadline'
```

Expected: FAIL because inventory types/functions do not exist.

- [ ] **Step 3: Implement validated roots and bounded inventory**

Define constants and types:

```python
MAX_CANDIDATES_PER_AREA = 100_000
RUN_BUDGET_SECONDS = 30.0
DATED_RESOURCE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    area: str
    kind: str
    size: int
    mtime: float

@dataclass
class CleanupStats:
    scanned: int = 0
    removed: int = 0
    released_bytes: int = 0
    skipped: int = 0
```

Resolve roots from `agent.sandbox_home`, the AgentTrace relative-root rule, and `workspace / resources.root`. Reject root symlinks and roots outside their required boundary.

Inventory only these shapes:

- sandbox: immediate children below `.cache`, `.npm/_cacache`, `.npm/_logs`, `.npm/_npx`; chat directories at `.cursor/chats/*/*`; `worker.log*` and entries below `.cursor/projects/*/assets`;
- traces: immediate regular `*.jsonl` files;
- resources: dated immediate directories, and immediate job directories below `outgoing` and `sending`.

Use `os.scandir`, `entry.stat(follow_symlinks=False)`, and a recursive measurement helper that returns `None` if any traversed entry is a symlink, special file, root escape, candidate limit exhaustion, or deadline exhaustion. Do not log candidate names.

- [ ] **Step 4: Implement race-resistant deletion**

Open the validated root with `os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)`, then open every relative parent component with the same flags and `dir_fd=parent_fd`. Re-check the candidate with `os.stat(name, dir_fd=parent_fd, follow_symlinks=False)`. Recursively unlink regular files and remove directories using only `dir_fd` operations; abort on symlinks, special files, device/inode mismatch, or boundary changes. Return `(removed_entries, released_bytes)` and treat disappearance as a non-fatal skip.

Do not use `shutil.rmtree`, string-prefix boundary checks, or resolved paths as the final deletion authority.

- [ ] **Step 5: Run storage safety tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_maintenance.py -k 'inventory or symlink or candidate_limit or deadline or deletion'
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/storage_maintenance.py tests/test_storage_maintenance.py
git commit -m "feat: inventory storage cleanup candidates safely"
```

---

### Task 4: Age, Capacity, and Pressure Cleanup Policies

**Files:**
- Modify: `src/qq_agent_bridge/storage_maintenance.py`
- Modify: `tests/test_storage_maintenance.py`

**Interfaces:**
- Produces: `MaintenanceSummary(trigger, elapsed_seconds, scanned, removed, released_bytes, skipped_areas, free_before, free_after)`.
- Produces: `StorageMaintainer.run(trigger: Literal["startup", "periodic", "pressure"]) -> MaintenanceSummary`.
- Produces: `StorageMaintainer.pressure_needed() -> bool`.
- Consumers: periodic orchestration in Task 5 and App in Task 6.

- [ ] **Step 1: Write failing policy tests**

Use controlled mtimes and small byte caps. Required assertions:

```python
def test_normal_cleanup_applies_retention_then_oldest_first_cap(tmp_path: Path) -> None:
    cfg = make_storage_cfg(tmp_path)
    cfg.storage_maintenance.traces.retention_seconds = 100
    cfg.storage_maintenance.traces.max_bytes = 6
    old = write_trace(cfg, "old.jsonl", b"111", mtime=800)
    first = write_trace(cfg, "first.jsonl", b"2222", mtime=950)
    latest = write_trace(cfg, "latest.jsonl", b"33", mtime=990)
    summary = run_maintainer(cfg, now=1_000, trigger="periodic")
    assert not old.exists()
    assert not first.exists()
    assert latest.exists()
    assert summary.released_bytes == 7


def test_cleanup_preserves_auth_unknown_entries_and_runtime_skill_bundle(tmp_path: Path) -> None:
    cfg = make_storage_cfg(tmp_path)
    protected = create_protected_files(cfg)
    unknown = Path(cfg.agent.sandbox_home) / "unknown.data"
    unknown.write_bytes(b"x" * 100)
    runtime_skill = Path(cfg.agent.default_workspace) / cfg.resources.root / "runtime-skills"
    runtime_skill.mkdir(parents=True)
    run_maintainer(cfg, now=1_000_000, trigger="pressure", free_bytes=0)
    assert all(path.exists() for path in protected)
    assert unknown.exists()
    assert runtime_skill.exists()
```

Also test 7-day received retention, 24-hour outgoing/sending retention, pressure ordering, post-cleanup free-space measurement, missing roots, individual deletion errors, and privacy-safe logs.

- [ ] **Step 2: Run policy tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_maintenance.py -k 'retention or cap or pressure or protected or privacy'
```

Expected: FAIL because `StorageMaintainer.run()` is not implemented.

- [ ] **Step 3: Implement deterministic cleanup selection**

For each area:

1. Sort candidates by `(mtime, path.as_posix())` for deterministic behavior without logging paths.
2. Delete candidates older than their class retention when retention is non-zero.
3. Recalculate eligible remaining bytes.
4. If cap is non-zero, delete oldest candidates until eligible bytes are at or below the cap.
5. Under pressure, clear all disposable sandbox cache candidates first, then process chats/logs, traces, transient resources, and received resources in that order until free space reaches `min_free_bytes` or eligible candidates are exhausted.

Use an injected `now: Callable[[], float]`, `monotonic`, and `disk_usage` for deterministic tests. Check the 30-second deadline and a `threading.Event` cancellation flag before each scan and deletion. If the coroutine running `asyncio.to_thread` is cancelled, set the flag and await the worker's bounded exit before releasing the maintenance lease.

- [ ] **Step 4: Implement structured summary and privacy-safe logging**

Emit one start and one summary record using fixed labels only:

```python
logger.info("storage maintenance start trigger=%s", trigger)
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
```

Rate-limit unresolved pressure warnings to once per 3,600 seconds. Log exception class names and fixed area labels, never candidate paths.

- [ ] **Step 5: Run all storage maintenance tests**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_maintenance.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/storage_maintenance.py tests/test_storage_maintenance.py
git commit -m "feat: enforce storage retention and capacity limits"
```

---

### Task 5: Coalesced Periodic and Pressure Orchestration

**Files:**
- Modify: `src/qq_agent_bridge/storage_maintenance.py`
- Modify: `tests/test_storage_maintenance.py`

**Interfaces:**
- Produces: `StorageMaintainer.start() -> None`.
- Produces: `StorageMaintainer.stop() -> None`.
- Produces: `StorageMaintainer.request_pressure_check() -> None`.
- Produces: `StorageMaintainer.reload_config(cfg: BridgeConfig) -> bool`, returning whether a managed root changed and requires restart.
- Consumers: `App.run()`, `App._cleanup_reply_job()`, and `App._reload_config()` in Task 6.

- [ ] **Step 1: Write failing orchestration tests**

Test that:

- startup cleanup completes before `start()` returns;
- the background loop runs after the configured interval;
- repeated pressure requests coalesce into one run;
- `stop()` cancels and awaits the loop;
- interval reload wakes and reschedules the loop;
- limits reload immediately but root changes return `True` and preserve original roots until restart;
- disabled maintenance starts no loop and performs no deletion.

Use injected async `sleep`/events rather than real six-hour waits.

- [ ] **Step 2: Run orchestration tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_maintenance.py -k 'start or periodic or coalesce or reload or stop or disabled'
```

Expected: FAIL because lifecycle methods do not exist.

- [ ] **Step 3: Implement one-loop orchestration**

Maintain one `_loop_task`, one `_wake_event`, one `_pressure_requested` flag, and one `_run_lock`. `start()` first runs `await run("startup")`, then creates the loop. The loop waits for either timeout or `_wake_event`; timeout triggers `periodic`, while a pressure flag triggers `pressure`. Clear and re-check flags under the run lock so requests arriving during cleanup produce at most one follow-up run.

`request_pressure_check()` performs only fixed-count `shutil.disk_usage` calls for validated filesystems and sets the event when any free value is below the threshold. It must not scan directory contents.

`stop()` sets a stopping flag, wakes the loop, cancels it, awaits it with `return_exceptions=True`, and clears references.

- [ ] **Step 4: Run orchestration tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_storage_maintenance.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/storage_maintenance.py tests/test_storage_maintenance.py
git commit -m "feat: schedule coalesced storage maintenance"
```

---

### Task 6: App Lifecycle and Agent Integration

**Files:**
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `tests/test_app_async.py`

**Interfaces:**
- Consumes: `StorageActivityGate`, `GatedAgentAdapter`, and `StorageMaintainer`.
- Produces: all App Agent/resource operations covered by activity leases; startup/periodic/pressure/reload/shutdown maintenance lifecycle.

- [ ] **Step 1: Write failing App integration tests**

Add focused tests proving:

```python
def test_app_runs_startup_maintenance_before_onebot_start() -> None:
    async def go() -> None:
        app = App(make_cfg())
        order: list[str] = []
        app.storage_maintainer.start = AsyncMock(side_effect=lambda: order.append("cleanup"))
        app.adapter.start = AsyncMock(side_effect=lambda _handler: order.append("onebot"))
        task = asyncio.create_task(app.run())
        await wait_until(lambda: order == ["cleanup", "onebot"])
        task.cancel()
        await task
        assert order[:2] == ["cleanup", "onebot"]
    asyncio.run(go())
```

Also test:

- an active `_agent_runner` blocks maintenance until resource cleanup finishes;
- proactive calls use `GatedAgentAdapter`;
- artifact repair uses the gated adapter;
- `_cleanup_reply_job()` requests only a pressure check;
- reload updates limits/interval and reports root changes as restart-required;
- shutdown awaits maintainer stop even when other cleanup raises;
- maintenance exceptions do not suppress replies or stop the bridge.

- [ ] **Step 2: Run App integration tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_app_async.py -k storage_maintenance
```

Expected: FAIL because `App` has no storage maintainer.

- [ ] **Step 3: Construct raw and gated Agent adapters**

In `App.__init__`:

```python
self.storage_gate = StorageActivityGate()
self.agent = build_agent_adapter(cfg)
self.cursor = self.agent
self.gated_agent = GatedAgentAdapter(self.agent, self.storage_gate)
self.storage_maintainer = StorageMaintainer(cfg, self.storage_gate)
```

Keep `self.agent`/`self.cursor` as the raw adapter so existing extension and test compatibility remains intact. Pass `self.gated_agent` to `NaturalLanguageScheduleParser` and `ProactiveSpeaker`, and use it for artifact repair. On reload, rebuild both raw and gated adapters.

- [ ] **Step 4: Lease the full App job lifecycle**

Mechanically rename the existing `_agent_runner` implementation to `_agent_runner_inner` without editing its body. Add this wrapper under the original name:

```python
async def _agent_runner(self, job: Job) -> str:
    async with self.storage_gate.activity():
        return await self._agent_runner_inner(job)
```

This lease must include resource preparation, runtime-skill preparation, Agent invocation, and `resources.cleanup_prepared()`.

- [ ] **Step 5: Integrate startup, pressure, reload, and shutdown**

Before `adapter.start()` call `await storage_maintainer.start()`; this performs startup cleanup and creates the periodic loop. In `_cleanup_reply_job()`, call `storage_maintainer.request_pressure_check()` after policy cleanup. During reload, call `roots_changed = storage_maintainer.reload_config(cfg)` and include `存储根目录变更需要重启。` in the reply when true. During shutdown, cancel and await reply, heartbeat, schedule, artifact-repair, and proactive work first; then await `storage_maintainer.stop()` before stopping OneBot. `stop()` signals the synchronous worker and waits for it to exit before the maintenance lease is released.

Maintenance failures must be caught and logged by class name without blocking startup or user replies.

- [ ] **Step 6: Run App and related subsystem tests**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q \
  tests/test_app_async.py \
  tests/test_schedule_app.py \
  tests/test_proactive.py \
  tests/test_agent_runtime.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/qq_agent_bridge/main.py tests/test_app_async.py
git commit -m "feat: integrate storage maintenance lifecycle"
```

---

### Task 7: Documentation, Full Verification, and Adversarial Review

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `config.example.yaml` if review finds comments incomplete
- Test: all `tests/`

**Interfaces:**
- Consumes: completed storage maintenance behavior.
- Produces: operator documentation and release-quality verification evidence.

- [ ] **Step 1: Write documentation assertions first**

Extend `tests/test_deployment_docs.py` to assert both READMEs mention:

- the three managed areas;
- startup and six-hour cleanup;
- default limits and retention;
- active-job protection;
- how to disable maintenance with `storage_maintenance.enabled: false`.

- [ ] **Step 2: Run documentation tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q tests/test_deployment_docs.py -k storage
```

Expected: FAIL because the storage section is absent.

- [ ] **Step 3: Document operations and privacy behavior**

Add concise English and Chinese sections containing the exact defaults, managed roots, trigger behavior, non-fatal logging, symlink/unknown-entry preservation, and restart note for root changes. Do not document internal authentication values or the user's absolute local paths.

- [ ] **Step 4: Run focused and full test suites**

Run:

```bash
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q \
  tests/test_storage_gate.py \
  tests/test_storage_maintenance.py \
  tests/test_config.py \
  tests/test_app_async.py \
  tests/test_deployment_docs.py

UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q
git diff --check
```

Expected: all tests PASS and `git diff --check` exits 0. If the managed Codex sandbox hangs only in `test_default_http_fetch_rejects_loopback_targets`, rerun the full suite with that one test deselected and record the environment limitation; do not classify it as a storage-maintenance pass.

- [ ] **Step 5: Perform adversarial review**

Review the final diff specifically for:

- symlink and rename races between inventory and deletion;
- accidental cleanup outside validated roots;
- deletion of authentication, current job, or runtime-skill data;
- activity-gate starvation/deadlock/cancellation leaks;
- periodic-task leaks after reload/shutdown;
- unbounded scans or event-loop blocking;
- path, filename, prompt, or token leakage in logs;
- ignored configuration and unsafe zero-value semantics.

Fix every Critical/Important finding with a failing regression test first, then rerun the focused and full suites.

- [ ] **Step 6: Commit documentation and review fixes**

```bash
git add README.md README.zh-CN.md tests/test_deployment_docs.py config.example.yaml
git add src tests
git commit -m "docs: document automatic storage maintenance"
```

- [ ] **Step 7: Report operational result**

Report final test counts, any sandbox-only deselection, the configured cleanup defaults, whether local `config.yaml` changed, and the exact commits created. Do not push unless the user explicitly requests it.
