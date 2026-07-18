# Transactional Harness Artifact Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bridge, rather than agent prose, decide whether a generated artifact exists and was delivered successfully to QQ.

**Architecture:** Extend outgoing-resource inspection with a structured result and deterministic recovery limited to the current job outbox. Add a focused coordinator for one bounded repair turn, then integrate it into `App` as a resource-first delivery transaction that suppresses false success text and records only verified outcomes in memory.

**Tech Stack:** Python 3.13, asyncio, pathlib/stat filesystem validation, pytest, existing OneBot adapter and agent runtime abstraction.

## Global Constraints

- Agent output and resource directives are untrusted input.
- Never weaken the current token, outbox inode, workspace containment, regular-file, hard-link, size, stable-copy, or voice-duration checks.
- Deterministic discovery is limited to one eligible top-level regular file in the current job outbox; never recurse or guess among multiple candidates.
- Agent repair runs at most once and cannot exceed 90 seconds or the parent job's remaining runtime, whichever is smaller.
- Resource sends complete before any agent-authored final text is released.
- Failed delivery never exposes resource tokens, internal paths, stack traces, or agent-authored success claims.
- No new runtime dependency.

---

## File Structure

- Modify `src/qq_agent_bridge/outgoing_resources.py`: structured inspection result, malformed-directive recovery, unique top-level candidate discovery, and existing filesystem validation.
- Create `src/qq_agent_bridge/artifact_delivery.py`: bounded repair resolution and delivery outcome types; no OneBot-specific imports.
- Modify `src/qq_agent_bridge/main.py`: construct repair prompts, invoke the configured runtime once, send resources before text, and store verified memory.
- Modify `tests/test_outgoing_resources.py`: parser/recovery and security regression tests.
- Create `tests/test_artifact_delivery.py`: coordinator unit tests for repair limits and merge behavior.
- Modify `tests/test_app_async.py`: observable message ordering, adapter failure, and memory behavior.

### Task 1: Structured Artifact Inspection and Glued-Line Recovery

**Files:**
- Modify: `src/qq_agent_bridge/outgoing_resources.py`
- Modify: `tests/test_outgoing_resources.py`

**Interfaces:**
- Extends: `OutgoingResource` with `source_path: Path` and `size_bytes: int` so repaired results can be deduplicated and budgeted without trusting staged filenames.
- Produces: `ArtifactInspection(clean_text: str, resources: tuple[OutgoingResource, ...], warnings: tuple[str, ...], attempted: int, unresolved: int, recovered: int)`.
- Produces: `inspect_outgoing_resources(...) -> ArtifactInspection`.
- Preserves: `collect_outgoing_resources(...) -> tuple[str, tuple[OutgoingResource, ...], list[str]]` as a compatibility wrapper.

- [ ] **Step 1: Write failing tests for structured inspection and the production malformed line**

```python
from qq_agent_bridge.outgoing_resources import (
    collect_outgoing_resources,
    inspect_outgoing_resources,
)


def test_recovers_existing_file_when_prose_is_glued_to_directive_path(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "视频总结.md"
    report.write_text("summary", encoding="utf-8")
    rel = report.relative_to(tmp_path).as_posix()

    result = inspect_outgoing_resources(
        f"文件发你啦\nQQBOT_SEND_FILE: send-token {rel}主人，已经整理好了",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.clean_text == "文件发你啦"
    assert len(result.resources) == 1
    assert result.resources[0].path.read_text(encoding="utf-8") == "summary"
    assert result.warnings == ()
    assert (result.attempted, result.unresolved, result.recovered) == (1, 0, 1)


def test_structured_inspection_reports_unresolved_missing_directive(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    result = inspect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token downloads/qq-agent-bridge/outgoing/job-1/missing.pdf",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.attempted == 1
    assert result.unresolved == 1
    assert result.warnings == ("无法发送资源：文件不存在或不是普通文件",)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_outgoing_resources.py::test_recovers_existing_file_when_prose_is_glued_to_directive_path tests/test_outgoing_resources.py::test_structured_inspection_reports_unresolved_missing_directive -q
```

Expected: collection fails because `inspect_outgoing_resources` and `ArtifactInspection` do not exist.

- [ ] **Step 3: Add the structured result and compatibility wrapper**

Add the public result type and move the current parser body behind the new function:

```python
@dataclass(frozen=True)
class ArtifactInspection:
    clean_text: str
    resources: tuple[OutgoingResource, ...]
    warnings: tuple[str, ...]
    attempted: int
    unresolved: int
    recovered: int


@dataclass(frozen=True)
class OutgoingResource:
    kind: str
    path: Path
    name: str
    duration_seconds: int | None = None
    source_path: Path | None = None
    size_bytes: int = 0


def collect_outgoing_resources(...):
    result = inspect_outgoing_resources(
        text,
        cfg,
        outbox_dir=outbox_dir,
        token=token,
        job_id=job_id,
        expected_outbox=expected_outbox,
    )
    return result.clean_text, result.resources, list(result.warnings)
```

Within `inspect_outgoing_resources`, increment `attempted` for every matched directive, `unresolved` whenever a matched directive cannot stage a resource, and `recovered` when malformed-path recovery succeeds.

- [ ] **Step 4: Implement recovery against actual current-outbox candidates**

Add a helper that compares the malformed path token with real top-level files and returns only a unique longest prefix:

```python
def _recover_glued_path(raw_path: str, workspace: Path, outbox: Path) -> str | None:
    matches: list[str] = []
    for candidate in _eligible_top_level_files(outbox):
        absolute = candidate.as_posix()
        relative = candidate.relative_to(workspace).as_posix()
        for shown in (absolute, relative):
            if raw_path.startswith(shown) and raw_path != shown:
                matches.append(shown)
    if not matches:
        return None
    longest = max(len(value) for value in matches)
    winners = sorted({value for value in matches if len(value) == longest})
    return winners[0] if len(winners) == 1 else None
```

`_eligible_top_level_files` must use `iterdir()`, `lstat()`, `stat.S_ISREG`, `st_nlink == 1`, and reject hidden names. The recovered path then passes through the existing `_resolve_workspace_path`, outbox containment, size, voice, and `_copy_for_sending` validation exactly like a normal directive. Do not preserve the unparseable glued suffix in user text.

- [ ] **Step 5: Run focused and existing parser tests**

Run:

```bash
uv run pytest tests/test_outgoing_resources.py -q
```

Expected: all outgoing-resource tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/qq_agent_bridge/outgoing_resources.py tests/test_outgoing_resources.py
git commit -m "Harden outgoing artifact directive parsing"
```

### Task 2: Unique Outbox Recovery Without Unsafe Guessing

**Files:**
- Modify: `src/qq_agent_bridge/outgoing_resources.py`
- Modify: `tests/test_outgoing_resources.py`

**Interfaces:**
- Consumes: `ArtifactInspection` and `_eligible_top_level_files(outbox: Path)` from Task 1.
- Extends: `inspect_outgoing_resources(..., discover_unique: bool = True) -> ArtifactInspection`.

- [ ] **Step 1: Write failing discovery and ambiguity tests**

```python
def test_recovers_unique_top_level_outbox_file_after_broken_directive(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token missing.pdf",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert len(result.resources) == 1
    assert result.resources[0].path.read_bytes() == b"pdf"
    assert (result.unresolved, result.recovered) == (0, 1)


def test_does_not_guess_when_multiple_top_level_files_exist(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "notes.md").write_text("notes", encoding="utf-8")
    (outbox / "report.pdf").write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token missing.pdf",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.unresolved == 1


def test_unique_discovery_ignores_nested_temporary_files(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "tmp").mkdir()
    (outbox / "tmp" / "frame.png").write_bytes(b"frame")

    result = inspect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token missing.pdf",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_outgoing_resources.py::test_recovers_unique_top_level_outbox_file_after_broken_directive tests/test_outgoing_resources.py::test_does_not_guess_when_multiple_top_level_files_exist tests/test_outgoing_resources.py::test_unique_discovery_ignores_nested_temporary_files -q
```

Expected: the unique-file case fails because no fallback discovery exists; security cases document the required boundary.

- [ ] **Step 3: Implement one-candidate discovery through normal validation**

After directive parsing, inspect `_eligible_top_level_files(outbox)` when no resource was selected. If and only if it returns one candidate, infer `image` for configured image suffixes and `file` for every other suffix, then pass the candidate to the same staging helper used by directives. On success, remove a superseded missing-file warning if present, set `unresolved = 0`, and increment `recovered`. This applies both to a broken directive and to an omitted directive, because the unique current-job top-level file is the harness evidence of intended delivery.

Do not infer `voice`; WAV, MP3, FLAC, and other audio discovered this way are sent as files.

- [ ] **Step 4: Add omitted-directive, symlink, hard-link, hidden, and oversized regressions**

```python
def test_recovers_unique_top_level_file_when_agent_omits_directive(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"pdf")
    result = inspect_outgoing_resources(
        "文件已经整理好",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )
    assert result.clean_text == "文件已经整理好"
    assert len(result.resources) == 1
    assert result.resources[0].path.read_bytes() == b"pdf"
    assert result.attempted == 0
    assert result.recovered == 1


def test_text_only_output_without_outbox_file_stays_text_only(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )
    assert result.clean_text == "普通文本回答"
    assert result.resources == ()
    assert result.recovered == 0
```

Add four explicit tests named `test_unique_discovery_rejects_symlink`, `test_unique_discovery_rejects_hard_link`, `test_unique_discovery_ignores_hidden_file`, and `test_unique_discovery_rejects_oversized_file`. Each creates exactly that candidate type, calls `inspect_outgoing_resources`, and asserts `resources == ()` and `recovered == 0`.

- [ ] **Step 5: Run resource security tests**

Run:

```bash
uv run pytest tests/test_outgoing_resources.py tests/test_policy.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/qq_agent_bridge/outgoing_resources.py tests/test_outgoing_resources.py
git commit -m "Recover unique verified task artifacts"
```

### Task 3: One-Shot Artifact Repair Coordinator

**Files:**
- Create: `src/qq_agent_bridge/artifact_delivery.py`
- Create: `tests/test_artifact_delivery.py`

**Interfaces:**
- Consumes: `ArtifactInspection` from Task 1.
- Produces: `ArtifactResolution(text: str, resources: tuple[OutgoingResource, ...], warnings: tuple[str, ...], repair_attempted: bool, verified: bool)`.
- Produces: `resolve_artifacts(initial_text: str, inspect: Callable[[str], ArtifactInspection], repair: Callable[[tuple[str, ...]], Awaitable[str]] | None, max_items: int, max_total_bytes: int) -> Awaitable[ArtifactResolution]`.

- [ ] **Step 1: Write failing coordinator tests**

```python
def make_resource(tmp_path: Path, name: str = "report.pdf", payload: bytes = b"pdf"):
    source = tmp_path / name
    source.write_bytes(payload)
    return OutgoingResource(
        kind="file",
        path=source,
        name=name,
        source_path=source,
        size_bytes=len(payload),
    )


def inspection(*, text: str = "", resources=(), warnings=(), attempted=0, unresolved=0):
    return ArtifactInspection(text, tuple(resources), tuple(warnings), attempted, unresolved, 0)


def test_resolution_skips_repair_for_verified_artifact(tmp_path: Path) -> None:
    async def go() -> None:
        calls = 0

        async def repair(_warnings: tuple[str, ...]) -> str:
            nonlocal calls
            calls += 1
            return "unused"

        result = await resolve_artifacts(
            "initial",
            inspect=lambda _text: inspection(
                text="完成", resources=(make_resource(tmp_path),), attempted=1
            ),
            repair=repair,
            max_items=4,
            max_total_bytes=1024,
        )
        assert result.verified is True
        assert result.repair_attempted is False
        assert calls == 0

    asyncio.run(go())


def test_resolution_repairs_unresolved_artifact_once(tmp_path: Path) -> None:
    async def go() -> None:
        inspected: list[str] = []

        def inspect(text: str) -> ArtifactInspection:
            inspected.append(text)
            if text == "repair-output":
                return inspection(resources=(make_resource(tmp_path),), attempted=1)
            return inspection(warnings=("missing",), attempted=1, unresolved=1)

        async def repair(warnings: tuple[str, ...]) -> str:
            assert warnings == ("missing",)
            return "repair-output"

        result = await resolve_artifacts(
            "initial", inspect=inspect, repair=repair, max_items=4, max_total_bytes=1024
        )
        assert result.verified is True
        assert result.repair_attempted is True
        assert inspected == ["initial", "repair-output"]

    asyncio.run(go())


def test_resolution_never_repairs_failed_repair_twice() -> None:
    async def go() -> None:
        calls = 0

        async def repair(_warnings: tuple[str, ...]) -> str:
            nonlocal calls
            calls += 1
            return "still-missing"

        result = await resolve_artifacts(
            "initial",
            inspect=lambda _text: inspection(warnings=("missing",), attempted=1, unresolved=1),
            repair=repair,
            max_items=4,
            max_total_bytes=1024,
        )
        assert result.verified is False
        assert result.repair_attempted is True
        assert calls == 1

    asyncio.run(go())
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_artifact_delivery.py -q
```

Expected: import fails because `artifact_delivery.py` does not exist.

- [ ] **Step 3: Implement the minimal coordinator**

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .outgoing_resources import ArtifactInspection, OutgoingResource

InspectArtifacts = Callable[[str], ArtifactInspection]
RepairArtifacts = Callable[[tuple[str, ...]], Awaitable[str]]


@dataclass(frozen=True)
class ArtifactResolution:
    text: str
    resources: tuple[OutgoingResource, ...]
    warnings: tuple[str, ...]
    repair_attempted: bool
    verified: bool


async def resolve_artifacts(
    initial_text: str,
    *,
    inspect: InspectArtifacts,
    repair: RepairArtifacts | None = None,
    max_items: int,
    max_total_bytes: int,
) -> ArtifactResolution:
    first = inspect(initial_text)
    if first.attempted == 0 or first.unresolved == 0:
        return ArtifactResolution(
            first.clean_text, first.resources, first.warnings, False, first.unresolved == 0
        )
    if repair is None:
        return ArtifactResolution(first.clean_text, first.resources, first.warnings, False, False)
    repaired = inspect(await repair(first.warnings))
    merged, budget_ok = _merge_resources(
        first.resources,
        repaired.resources,
        max_items=max_items,
        max_total_bytes=max_total_bytes,
    )
    verified = repaired.unresolved == 0 and bool(merged) and budget_ok
    return ArtifactResolution(
        first.clean_text,
        merged,
        repaired.warnings,
        True,
        verified,
    )


def _merge_resources(
    first: tuple[OutgoingResource, ...],
    repaired: tuple[OutgoingResource, ...],
    *,
    max_items: int,
    max_total_bytes: int,
) -> tuple[tuple[OutgoingResource, ...], bool]:
    merged: list[OutgoingResource] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    for resource in first + repaired:
        source = resource.source_path or resource.path
        key = (resource.kind, str(source.resolve(strict=False)))
        if key in seen:
            continue
        if len(merged) >= max(0, max_items) or total + resource.size_bytes > max_total_bytes:
            return tuple(merged), False
        seen.add(key)
        merged.append(resource)
        total += resource.size_bytes
    return tuple(merged), True
```

Add `test_resolution_deduplicates_repair_resources` and `test_resolution_rejects_merged_total_size_over_budget`, using `make_resource` and asserting first-seen ordering, one copy of a repeated source, and `verified is False` when the merged budget is exceeded.

- [ ] **Step 4: Add timeout/cancellation behavior at the callback boundary**

The coordinator does not own wall-clock policy. Add a test proving `CancelledError` from `repair` propagates and is not transformed into a second attempt. The `App` integration in Task 4 owns the 90-second/remaining-runtime `asyncio.wait_for` bound.

- [ ] **Step 5: Run coordinator tests**

Run:

```bash
uv run pytest tests/test_artifact_delivery.py tests/test_outgoing_resources.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/qq_agent_bridge/artifact_delivery.py tests/test_artifact_delivery.py
git commit -m "Add bounded artifact repair coordinator"
```

### Task 4: Transactional App Delivery, Repair Prompt, and Verified Memory

**Files:**
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `tests/test_app_async.py`

**Interfaces:**
- Consumes: `resolve_artifacts`, `ArtifactResolution`, and `inspect_outgoing_resources` from Tasks 1 and 3.
- Produces: `App._repair_outgoing_artifacts(job: Job, warnings: tuple[str, ...]) -> str`.
- Produces: `App._send_outgoing_resource(job: Job, resource: OutgoingResource, index: int) -> None`.

- [ ] **Step 1: Write a failing end-to-end regression for the production glued-line case and ordering**

Extend `FakeAdapter` with an `events: list[tuple[str, str]]`; append `("text", text)` in `send` and `("file", path.name)` in `send_file`.

```python
def test_malformed_file_directive_sends_real_file_before_success_text(tmp_path: Path) -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.workspaces = {str(tmp_path): True}
        cfg.agent.default_workspace = str(tmp_path)

        async def fake_agent(prompt: str, workspace=None, mode="ask", model=None, progress=None):
            outbox_rel, token = extract_outgoing_prompt_context(prompt)
            report = tmp_path / outbox_rel / "视频总结.md"
            report.write_text("summary", encoding="utf-8")
            return f"文件发你啦\nQQBOT_SEND_FILE: {token} {outbox_rel}/视频总结.md主人，整理好了"

        app = App(cfg)
        app.adapter = adapter
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_agent

        await app._handle(make_ev("/task 总结视频写成文件", group="group", mid="artifact-glued"))
        await wait_until_sent(adapter, "文件发你啦")

        delivery_events = [event for event in adapter.events if event[1] != "收到，我处理一下。"]
        assert delivery_events[0][0] == "file"
        assert delivery_events[1] == ("text", "文件发你啦")

    asyncio.run(go())
```

- [ ] **Step 2: Write failing tests for one repair, send failure, and memory truth**

Add `test_missing_artifact_invokes_one_repair_and_sends_result`, `test_adapter_file_failure_suppresses_agent_success_text`, and `test_delivery_failure_memory_records_bridge_outcome`. Their core assertions are:

```python
assert agent_calls == 2  # original + one repair
assert len(adapter.sent_files) == 1
assert all("文件发你啦" not in item[2] for item in adapter.sent if "发送到 QQ 失败" in item[2])
assert any("文件已经生成，但发送到 QQ 失败" in item[2] for item in adapter.sent)
assert "本次未确认交付" in app.memory.format_history(event)
assert "文件发你啦" not in app.memory.format_history(event)
```

For the send-failure test, subclass `FakeAdapter.send_file` to raise `RuntimeError("upload failed")`. For the repair test, have the first agent call return a missing directive and the second call create a file in the same outbox and return only a valid directive.

- [ ] **Step 3: Run the new App tests and verify RED**

Run:

```bash
uv run pytest tests/test_app_async.py -k "artifact and (malformed or repair or failure or memory)" -q
```

Expected: ordering and repair tests fail because the current app sends text before files and has no repair transaction.

- [ ] **Step 4: Add the bounded repair callback**

In `App._repair_outgoing_artifacts`, calculate remaining time and refuse repair when exhausted:

```python
elapsed = max(0.0, time.time() - job.started)
remaining = max(0.0, self.cfg.effective_max_runtime() - elapsed)
timeout = min(90.0, remaining)
if timeout <= 0:
    return ""
```

Build a dedicated prompt that includes the original task, sanitized warning reasons, current `_format_outgoing_resource_context(job)`, and these requirements: repair only the missing artifact declaration or file; reuse existing work; output only valid `QQBOT_SEND_*` directives; do not claim success. Invoke:

```python
return await asyncio.wait_for(
    run_agent(
        self.agent,
        prompt,
        self.cfg.agent.default_workspace,
        "task",
        model=self._agent_model_for("task"),
        progress=self._progress_callback_for(job),
        trace_id=f"{job.id}-artifact-repair",
    ),
    timeout=timeout,
)
```

Catch `asyncio.TimeoutError` and ordinary runtime failures, log the job id and failure class without the token, and return an empty repair output. Propagate cancellation.

- [ ] **Step 5: Replace `_reply_when_done_inner` resource handling with the transaction**

Create an inspection closure bound to the job's outbox/token/inode and pass it plus the repair callback to `resolve_artifacts`. Then:

1. If resolution is unverified, send only `文件没有成功生成或无法验证，本次未发送。` and store that outcome in memory.
2. If resources exist, await `_send_outgoing_resource` for each resource before sending cleaned agent text.
3. Catch each adapter exception, count failures, and do not release cleaned agent text unless every selected resource succeeds.
4. On total send failure, send `文件已经生成，但发送到 QQ 失败，本次未确认交付。`.
5. On partial failure, send `已发送 N 个资源，另有 M 个发送失败。`.
6. Append only the final verified text or deterministic failure outcome to memory.

Keep text-only jobs (`attempted == 0`) on the existing reply path. Preserve group `reply_ats`, reply chunk delay, image/voice/file dispatch, and proactive send accounting.

- [ ] **Step 6: Run focused App and resource tests**

Run:

```bash
uv run pytest tests/test_app_async.py tests/test_artifact_delivery.py tests/test_outgoing_resources.py -q
```

Expected: all focused tests pass.

- [ ] **Step 7: Run the complete verification suite**

Run:

```bash
uv run pytest
git diff --check
```

Expected: all tests pass or explicitly configured live-agent tests skip; `git diff --check` exits 0.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/qq_agent_bridge/main.py tests/test_app_async.py
git commit -m "Make artifact delivery transactional"
```

### Task 5: Adversarial Review and Final Verification

**Files:**
- Modify only files implicated by concrete review findings.

**Interfaces:**
- No new public interface; this task verifies the design contract and security invariants.

- [ ] **Step 1: Review adversarial cases**

Inspect the final diff for token leakage, outbox replacement races, symlink/hard-link escape, duplicate repair recursion, total-size bypass across merged repairs, cancellation swallowing, adapter partial failure, false memory claims, and text-only task regression.

- [ ] **Step 2: Add a failing regression test for every confirmed finding**

Place parser/security findings in `tests/test_outgoing_resources.py`, coordinator findings in `tests/test_artifact_delivery.py`, and observable delivery findings in `tests/test_app_async.py`. Run each new test once and confirm the expected failure before patching production code.

- [ ] **Step 3: Implement only confirmed fixes and rerun focused tests**

Run:

```bash
uv run pytest tests/test_outgoing_resources.py tests/test_artifact_delivery.py tests/test_app_async.py -q
```

Expected: all focused tests pass.

- [ ] **Step 4: Run final verification**

Run:

```bash
uv run pytest
git diff --check
git status --short
```

Expected: full suite passes, whitespace check exits 0, and status contains only intended files if review fixes are not yet committed.

- [ ] **Step 5: Commit review fixes if any**

```bash
git add src/qq_agent_bridge/outgoing_resources.py src/qq_agent_bridge/artifact_delivery.py src/qq_agent_bridge/main.py tests/test_outgoing_resources.py tests/test_artifact_delivery.py tests/test_app_async.py
git commit -m "Harden transactional artifact delivery"
```

Skip this commit when the adversarial review finds no code changes.
