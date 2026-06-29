# Long Task Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add long-task multi-message progress for QQ `/task` and `/code` jobs using agent progress directives plus bridge heartbeats.

**Architecture:** Add focused progress modules for directive parsing and per-job reporting, then wire them into `App`, `Policy`, and `CursorAdapter`. Jobs should create reporters before starting Cursor so early progress is not lost; final replies still use the existing `_reply_when_done()` path.

**Tech Stack:** Python `asyncio`, existing pytest suite, Cursor CLI `--output-format stream-json`, existing OneBot adapter, existing `BridgeConfig` YAML loading.

---

## File Map

- Create `src/qq_agent_bridge/progress_directives.py`: parse/strip `QQBOT_PROGRESS:` lines from text and streaming chunks.
- Create `src/qq_agent_bridge/progress.py`: `ProgressReporter` for rate-limited progress sends and heartbeat loop.
- Modify `src/qq_agent_bridge/config.py`: add `ProgressConfig` and load `progress:` YAML.
- Modify `src/qq_agent_bridge/policy.py`: split job creation from task start and pass `Job` to the runner.
- Modify `src/qq_agent_bridge/main.py`: create/store reporters, pass progress callback to Cursor, clean up reporters.
- Modify `src/qq_agent_bridge/cursor_adapter.py`: support optional progress callback and stream-json mode.
- Modify `src/qq_agent_bridge/prompting.py`: expose progress directive syntax in `/task` and `/code` prompts.
- Modify `src/qq_agent_bridge/runtime_skill.py` and `skills/cursor-qq-runtime/SKILL.md`: teach Cursor when/how to emit `QQBOT_PROGRESS:`.
- Modify `config.example.yaml` and `config.test.yaml`: add default `progress:` config.
- Test files: add `tests/test_progress_directives.py`, add `tests/test_progress.py`, extend `tests/test_config.py`, `tests/test_cursor_adapter.py`, `tests/test_app_async.py`, `tests/test_prompting.py`, and `tests/test_runtime_skill.py`.

---

### Task 1: Progress Config

**Files:**
- Modify: `src/qq_agent_bridge/config.py`
- Modify: `config.example.yaml`
- Modify: `config.test.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing config test**

Add this to `tests/test_config.py`:

```python
def test_example_config_enables_long_task_progress() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.progress.enabled
    assert cfg.progress.first_heartbeat_seconds == 30
    assert cfg.progress.heartbeat_seconds == 45
    assert cfg.progress.min_progress_interval_seconds == 8
    assert cfg.progress.max_progress_messages == 8
    assert cfg.progress.max_progress_chars == 240
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
.venv/bin/pytest -q tests/test_config.py::test_example_config_enables_long_task_progress
```

Expected: FAIL with `AttributeError` for missing `progress`.

- [ ] **Step 3: Add config implementation**

In `src/qq_agent_bridge/config.py`, add:

```python
@dataclass
class ProgressConfig:
    enabled: bool = True
    first_heartbeat_seconds: int = 30
    heartbeat_seconds: int = 45
    min_progress_interval_seconds: int = 8
    max_progress_messages: int = 8
    max_progress_chars: int = 240
```

Add `progress: ProgressConfig = field(default_factory=ProgressConfig)` to `BridgeConfig`.

Inside `BridgeConfig.load()`:

```python
progress = ProgressConfig(**raw.get("progress", {}))
```

and pass `progress=progress` into `cls(...)`.

Add to both `config.example.yaml` and `config.test.yaml`:

```yaml
progress:
  enabled: true
  first_heartbeat_seconds: 30
  heartbeat_seconds: 45
  min_progress_interval_seconds: 8
  max_progress_messages: 8
  max_progress_chars: 240
```

- [ ] **Step 4: Verify config test passes**

Run:

```bash
.venv/bin/pytest -q tests/test_config.py
```

Expected: all tests in `tests/test_config.py` pass.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/config.py config.example.yaml config.test.yaml tests/test_config.py
git commit -m "Add long task progress config"
```

---

### Task 2: Progress Directive Parser

**Files:**
- Create: `src/qq_agent_bridge/progress_directives.py`
- Test: `tests/test_progress_directives.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_progress_directives.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.progress_directives import ProgressLineBuffer, strip_progress_directives  # type: ignore


def test_strip_progress_directives_removes_lines_and_returns_payloads() -> None:
    clean, progress = strip_progress_directives(
        "hello\nQQBOT_PROGRESS: 解析链接中\nworld\nQQBOT_PROGRESS: 抽帧完成\n"
    )

    assert clean == "hello\nworld"
    assert progress == ("解析链接中", "抽帧完成")


def test_strip_progress_directives_ignores_empty_payloads() -> None:
    clean, progress = strip_progress_directives("a\nQQBOT_PROGRESS:   \nb")

    assert clean == "a\nb"
    assert progress == ()


def test_progress_line_buffer_handles_split_chunks() -> None:
    buffer = ProgressLineBuffer()

    assert buffer.feed("hello\nQQBOT_PRO") == (("hello",), ())
    lines, progress = buffer.feed("GRESS: 处理中\nfinal")
    assert lines == ()
    assert progress == ("处理中",)
    assert buffer.finish() == (("final",), ())
```

- [ ] **Step 2: Run the failing parser tests**

Run:

```bash
.venv/bin/pytest -q tests/test_progress_directives.py
```

Expected: FAIL with import error for missing module.

- [ ] **Step 3: Implement parser**

Create `src/qq_agent_bridge/progress_directives.py`:

```python
"""QQ progress directive parsing."""
from __future__ import annotations

PROGRESS_PREFIX = "QQBOT_PROGRESS:"


def split_progress_line(line: str) -> tuple[str | None, str | None]:
    if not line.startswith(PROGRESS_PREFIX):
        return line, None
    payload = line[len(PROGRESS_PREFIX) :].strip()
    if not payload:
        return None, None
    return None, payload


def strip_progress_directives(text: str) -> tuple[str, tuple[str, ...]]:
    clean_lines: list[str] = []
    progress: list[str] = []
    for line in text.splitlines():
        clean, payload = split_progress_line(line)
        if clean is not None:
            clean_lines.append(clean)
        if payload:
            progress.append(payload)
    return "\n".join(clean_lines).strip(), tuple(progress)


class ProgressLineBuffer:
    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        self._pending += chunk
        if "\n" not in self._pending:
            return (), ()
        parts = self._pending.split("\n")
        self._pending = parts.pop()
        return self._process_lines(parts)

    def finish(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not self._pending:
            return (), ()
        pending = self._pending
        self._pending = ""
        return self._process_lines([pending])

    def _process_lines(self, lines: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        clean_lines: list[str] = []
        progress: list[str] = []
        for line in lines:
            clean, payload = split_progress_line(line)
            if clean is not None and clean:
                clean_lines.append(clean)
            if payload:
                progress.append(payload)
        return tuple(clean_lines), tuple(progress)
```

- [ ] **Step 4: Verify parser tests pass**

Run:

```bash
.venv/bin/pytest -q tests/test_progress_directives.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/progress_directives.py tests/test_progress_directives.py
git commit -m "Parse QQ progress directives"
```

---

### Task 3: Progress Reporter

**Files:**
- Create: `src/qq_agent_bridge/progress.py`
- Test: `tests/test_progress.py`

- [ ] **Step 1: Write failing reporter tests**

Create `tests/test_progress.py`:

```python
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.progress import ProgressReporter  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


def make_ev() -> ChatEvent:
    return ChatEvent(
        id="m1",
        platform="qq",
        chat_id="group",
        sender_id="reader",
        is_group=True,
        mentioned_bot=True,
        text="/task long",
        timestamp=1,
    )


def test_progress_reporter_rate_limits_and_caps_messages() -> None:
    async def go() -> None:
        sent: list[str] = []

        async def send(text: str, echo: str) -> None:
            sent.append(text)

        cfg = BridgeConfig()
        cfg.progress.min_progress_interval_seconds = 10
        cfg.progress.max_progress_messages = 2
        cfg.progress.max_progress_chars = 8
        now = [100.0]
        reporter = ProgressReporter("j1", make_ev(), cfg.progress, send, now=lambda: now[0])

        await reporter.send_progress("第一条很长很长")
        await reporter.send_progress("太快")
        now[0] += 11
        await reporter.send_progress("第二条")
        now[0] += 11
        await reporter.send_progress("第三条")

        assert sent == ["第一条很长很", "第二条"]

    asyncio.run(go())


def test_progress_reporter_sends_heartbeat_after_silence() -> None:
    async def go() -> None:
        sent: list[str] = []

        async def send(text: str, echo: str) -> None:
            sent.append(text)

        cfg = BridgeConfig()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        reporter = ProgressReporter("j1", make_ev(), cfg.progress, send)
        task = asyncio.create_task(reporter.run_heartbeat(lambda: False))
        await asyncio.sleep(1.2)
        reporter.stop()
        await task

        assert any("还在处理" in item for item in sent)

    asyncio.run(go())
```

- [ ] **Step 2: Run failing reporter tests**

Run:

```bash
.venv/bin/pytest -q tests/test_progress.py
```

Expected: FAIL with import error for missing `progress`.

- [ ] **Step 3: Implement `ProgressReporter`**

Create `src/qq_agent_bridge/progress.py`:

```python
"""Per-job progress reporting for QQ long tasks."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .config import ProgressConfig
from .redactor import redact
from .types import ChatEvent

ProgressSend = Callable[[str, str], Awaitable[None]]
DonePredicate = Callable[[], bool]


class ProgressReporter:
    def __init__(
        self,
        job_id: str,
        event: ChatEvent,
        cfg: ProgressConfig,
        send: ProgressSend,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.job_id = job_id
        self.event = event
        self.cfg = cfg
        self.send = send
        self.now = now or time.monotonic
        self._count = 0
        self._stopped = False
        self._last_sent_at = self.now()

    async def send_progress(self, text: str) -> None:
        if self._stopped or not self.cfg.enabled:
            return
        if self._count >= max(0, self.cfg.max_progress_messages):
            return
        current = self.now()
        if self._count and current - self._last_sent_at < self.cfg.min_progress_interval_seconds:
            return
        cleaned = redact(text).strip()
        if not cleaned:
            return
        cleaned = cleaned[: max(0, self.cfg.max_progress_chars)]
        await self.send(cleaned, f"{self.event.id}-progress-{self._count}")
        self._count += 1
        self._last_sent_at = current

    async def run_heartbeat(self, done: DonePredicate) -> None:
        if not self.cfg.enabled:
            return
        first = max(0, self.cfg.first_heartbeat_seconds)
        interval = max(1, self.cfg.heartbeat_seconds)
        await asyncio.sleep(first)
        while not self._stopped and not done():
            current = self.now()
            if current - self._last_sent_at >= interval:
                elapsed = int(current - self._last_sent_at + interval)
                await self.send(f"还在处理，已经跑了一会儿。", f"{self.event.id}-heartbeat")
                self._last_sent_at = self.now()
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._stopped = True
```

- [ ] **Step 4: Verify reporter tests pass**

Run:

```bash
.venv/bin/pytest -q tests/test_progress.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/progress.py tests/test_progress.py
git commit -m "Add long task progress reporter"
```

---

### Task 4: Policy Job Start Refactor

**Files:**
- Modify: `src/qq_agent_bridge/policy.py`
- Test: `tests/test_app_async.py`

This task is a prerequisite for Task 5 and is committed together with Task 5 after the app reporter wiring makes the new lifecycle test pass. Do not commit while this task's lifecycle test is red.

- [ ] **Step 1: Write failing lifecycle test**

Add to `tests/test_app_async.py`:

```python
def test_task_progress_reporter_exists_before_cursor_starts() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        seen_reporter = False

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]

        async def fake_agent_runner(job: Any) -> str:
            nonlocal seen_reporter
            seen_reporter = job.id in app._progress_reporters
            return "done"

        app.policy = Policy(cfg, fake_agent_runner)

        await app._handle(make_ev("/task long", group="group", mid="progress-race"))
        await wait_until_sent(adapter, "done")

        assert seen_reporter

    asyncio.run(go())
```

- [ ] **Step 2: Run failing lifecycle test**

Run:

```bash
.venv/bin/pytest -q tests/test_app_async.py::test_task_progress_reporter_exists_before_cursor_starts
```

Expected: FAIL because runner still receives `(cmd, args, ev)` and no reporter dictionary exists.

- [ ] **Step 3: Refactor `Policy` runner API**

In `src/qq_agent_bridge/policy.py`:

```python
JobRunner = Callable[[Job], Awaitable[str]]
```

Move `Job` above `JobRunner`.

Change `start_job()` so non-confirmed jobs do not auto-start:

```python
self.jobs[jid] = job
if not (self.cfg.dangerous_requires_confirm and cmd.name in ("code", "shell")):
    job.state = "queued"
return jid, None
```

Make `start_job_task()` public:

```python
def start_job_task(self, job: Job) -> None:
    self._start_job_task(job)
```

Change `_run()`:

```python
result = await asyncio.wait_for(
    self.runner(job),
    timeout=self.cfg.effective_max_runtime(),
)
```

Change `approve()` to clear nonce but not start the task:

```python
job.confirm_nonce = None
job.state = "queued"
return jid
```

- [ ] **Step 4: Update app runner signature minimally**

In `src/qq_agent_bridge/main.py`, change `_agent_runner(self, cmd, args, ev)` to `_agent_runner(self, job: Job)`, then inside:

```python
cmd = job.cmd
args = job.args
ev = job.event
```

After `self._configure_outgoing_resources(job)` and before scheduling reply, call:

```python
self.policy.start_job_task(job)
```

Do the same after approve before `_schedule_reply(job)`.

- [ ] **Step 5: Verify lifecycle test passes**

Run:

```bash
.venv/bin/pytest -q tests/test_app_async.py::test_task_progress_reporter_exists_before_cursor_starts
```

Expected: PASS after Task 5 creates `_progress_reporters`. Continue directly into Task 5 before committing.

- [ ] **Step 6: Commit after Task 5**

Do not commit this task alone if tests are red. Commit together with Task 5 after wiring reporters.

---

### Task 5: App Progress Reporter Wiring

**Files:**
- Modify: `src/qq_agent_bridge/main.py`
- Test: `tests/test_app_async.py`

- [ ] **Step 1: Write failing app progress tests**

Add to `tests/test_app_async.py`:

```python
def test_task_progress_directive_sends_intermediate_message() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            assert progress is not None
            await progress("已解析链接")
            await progress("已抽帧")
            return "最终结果"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task long", group="group", mid="long-progress"))
        await wait_until_sent(adapter, "已解析链接")
        await wait_until_sent(adapter, "最终结果")

        texts = [item[2] for item in adapter.sent]
        assert "QQBOT_PROGRESS" not in "\n".join(texts)
        assert texts.index("已解析链接") < texts.index("最终结果")

    asyncio.run(go())


def test_ask_does_not_pass_progress_callback() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        progress_values: list[Any] = []

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            progress_values.append(progress)
            return "ask ok"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/ask hi", mid="ask-progress"))
        await wait_until_sent(adapter, "ask ok")

        assert progress_values == [None]

    asyncio.run(go())
```

- [ ] **Step 2: Run failing app progress tests**

Run:

```bash
.venv/bin/pytest -q tests/test_app_async.py::test_task_progress_directive_sends_intermediate_message tests/test_app_async.py::test_ask_does_not_pass_progress_callback
```

Expected: FAIL because `cursor.run()` does not receive `progress`.

- [ ] **Step 3: Wire reporters in `App`**

In `src/qq_agent_bridge/main.py`, import:

```python
from .progress import ProgressReporter
```

Add in `App.__init__`:

```python
self._progress_reporters: dict[str, ProgressReporter] = {}
self._heartbeat_tasks: set[asyncio.Task[None]] = set()
```

Add helpers:

```python
def _progress_enabled_for(self, job: Job) -> bool:
    return self.cfg.progress.enabled and job.cmd in {"task", "code"}

def _create_progress_reporter(self, job: Job) -> None:
    if not self._progress_enabled_for(job):
        return

    async def send(text: str, echo: str) -> None:
        await self.adapter.send(job.event.chat_id, job.event.is_group, text, echo)

    self._progress_reporters[job.id] = ProgressReporter(job.id, job.event, self.cfg.progress, send)

def _start_heartbeat(self, job: Job) -> None:
    reporter = self._progress_reporters.get(job.id)
    if not reporter:
        return
    task = asyncio.create_task(reporter.run_heartbeat(lambda: job.state in {"done", "cancelled"}))
    self._heartbeat_tasks.add(task)
    task.add_done_callback(self._heartbeat_tasks.discard)

def _progress_callback_for(self, job: Job):
    reporter = self._progress_reporters.get(job.id)
    return reporter.send_progress if reporter else None
```

In `_handle()`, after `_configure_outgoing_resources(job)`, call `_create_progress_reporter(job)`, then `self.policy.start_job_task(job)`, then `_schedule_reply(job)`.

In approve handling, after `res`, get job, call `_create_progress_reporter(job)`, `self.policy.start_job_task(job)`, then `_schedule_reply(job)`.

In `_schedule_reply(job)`, call `_start_heartbeat(job)` before creating `_reply_when_done()`.

In `_reply_when_done()`, in a `finally`-like cleanup section after sending:

```python
reporter = self._progress_reporters.pop(job.id, None)
if reporter:
    reporter.stop()
```

In `_agent_runner(job)`, pass progress to Cursor:

```python
progress = self._progress_callback_for(job) if cmd in {"task", "code"} else None
return await self.cursor.run(prompt, ws, cursor_mode, model=model, progress=progress)
```

- [ ] **Step 4: Update shutdown cleanup**

In `App.run()` finally block, cancel and gather `_heartbeat_tasks` as already done for `_reply_tasks`.

- [ ] **Step 5: Verify app progress tests pass**

Run:

```bash
.venv/bin/pytest -q tests/test_app_async.py::test_task_progress_reporter_exists_before_cursor_starts tests/test_app_async.py::test_task_progress_directive_sends_intermediate_message tests/test_app_async.py::test_ask_does_not_pass_progress_callback
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4 and Task 5 together**

```bash
git add src/qq_agent_bridge/main.py src/qq_agent_bridge/policy.py tests/test_app_async.py
git commit -m "Wire progress reporters into QQ jobs"
```

---

### Task 6: Cursor Streaming And Progress Stripping

**Files:**
- Modify: `src/qq_agent_bridge/cursor_adapter.py`
- Test: `tests/test_cursor_adapter.py`

- [ ] **Step 1: Write failing adapter tests**

Add to `tests/test_cursor_adapter.py`:

```python
def test_task_command_uses_stream_json_when_progress_callback_present() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", "/tmp", "task", model="composer", stream=True)

    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_progress_directives_are_stripped_from_final_text() -> None:
    from qq_agent_bridge.progress_directives import strip_progress_directives

    clean, progress = strip_progress_directives("QQBOT_PROGRESS: one\nfinal\nQQBOT_PROGRESS: two")

    assert clean == "final"
    assert progress == ("one", "two")
```

- [ ] **Step 2: Run failing adapter tests**

Run:

```bash
.venv/bin/pytest -q tests/test_cursor_adapter.py::test_task_command_uses_stream_json_when_progress_callback_present tests/test_cursor_adapter.py::test_progress_directives_are_stripped_from_final_text
```

Expected: FAIL because `_build_cmd()` has no `stream` parameter.

- [ ] **Step 3: Add stream flag to `_build_cmd()`**

Change signature:

```python
def _build_cmd(
    self,
    prompt: str,
    workspace: str,
    mode: str,
    model: str | None,
    stream: bool = False,
) -> list[str]:
```

After model handling:

```python
if stream:
    cursor_cmd.extend(["--output-format", "stream-json"])
```

Update call sites in `run()`:

```python
cmd = self._build_cmd(prompt, ws, mode, model, stream=progress is not None)
```

- [ ] **Step 4: Implement streaming read path**

In `cursor_adapter.py`, import:

```python
from collections.abc import Awaitable, Callable
from .progress_directives import ProgressLineBuffer, strip_progress_directives
```

Add alias:

```python
ProgressCallback = Callable[[str], Awaitable[None]]
```

Change `run()` signature:

```python
async def run(..., progress: ProgressCallback | None = None) -> str:
```

After process creation:

```python
if progress:
    stdout_text, stderr_text = await asyncio.wait_for(
        self._communicate_streaming(proc, progress),
        timeout=self.cfg.agent.max_runtime_seconds,
    )
else:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.agent.max_runtime_seconds)
    stdout_text = (stdout or b"").decode("utf-8", "replace")
    stderr_text = (stderr or b"").decode("utf-8", "replace")
```

Add helper:

```python
async def _communicate_streaming(
    self,
    proc: asyncio.subprocess.Process,
    progress: ProgressCallback,
) -> tuple[str, str]:
    assert proc.stdout is not None
    stderr_task = asyncio.create_task(proc.stderr.read() if proc.stderr else asyncio.sleep(0, result=b""))
    buffer = ProgressLineBuffer()
    output_parts: list[str] = []
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        clean_lines, progress_lines = buffer.feed(text)
        output_parts.extend(clean_lines)
        for item in progress_lines:
            await progress(item)
    clean_lines, progress_lines = buffer.finish()
    output_parts.extend(clean_lines)
    for item in progress_lines:
        await progress(item)
    await proc.wait()
    stderr = await stderr_task
    return "\n".join(output_parts), (stderr or b"").decode("utf-8", "replace")
```

Before final return, strip any directives left in combined text:

```python
combined = (out + "\n" + err).strip()
cleaned = strip_ansi(combined)
cleaned, extra_progress = strip_progress_directives(cleaned)
if progress:
    for item in extra_progress:
        await progress(item)
```

- [ ] **Step 5: Verify adapter tests pass**

Run:

```bash
.venv/bin/pytest -q tests/test_cursor_adapter.py tests/test_progress_directives.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/cursor_adapter.py tests/test_cursor_adapter.py
git commit -m "Stream Cursor progress directives"
```

---

### Task 7: Prompt And Runtime Skill Contract

**Files:**
- Modify: `src/qq_agent_bridge/prompting.py`
- Modify: `src/qq_agent_bridge/runtime_skill.py`
- Modify: `skills/cursor-qq-runtime/SKILL.md`
- Test: `tests/test_prompting.py`
- Test: `tests/test_runtime_skill.py`

- [ ] **Step 1: Write failing prompt/skill tests**

Add to `tests/test_prompting.py`:

```python
def test_task_prompt_documents_progress_directive() -> None:
    prompt = build_agent_prompt("task", "处理一个长任务", make_ev())

    assert "QQBOT_PROGRESS:" in prompt
    assert "有意义的阶段进展" in prompt
    assert "不要刷屏" in prompt
```

Add to `tests/test_runtime_skill.py`:

```python
def test_runtime_skill_teaches_progress_directives() -> None:
    skill = build_cursor_runtime_skill("task")

    assert "QQBOT_PROGRESS:" in skill
    assert "真实完成的阶段" in skill
    assert "不要刷屏" in skill
```

- [ ] **Step 2: Run failing prompt/skill tests**

Run:

```bash
.venv/bin/pytest -q tests/test_prompting.py::test_task_prompt_documents_progress_directive tests/test_runtime_skill.py::test_runtime_skill_teaches_progress_directives
```

Expected: FAIL because contract text is not present.

- [ ] **Step 3: Update prompt and skills**

In `src/qq_agent_bridge/prompting.py`, add to `/task` prompt text:

```python
"长任务可以用 `QQBOT_PROGRESS: <短进度>` 输出有意义的阶段进展；只报告真实完成的步骤，不要刷屏。"
```

In `_FALLBACK_SKILL` and `skills/cursor-qq-runtime/SKILL.md`, add under task execution:

```markdown
- 长程任务进度：可以输出 `QQBOT_PROGRESS: <短进度>` 报告真实完成的阶段，例如“已解析链接，正在抽帧”。只报告已发生的动作，不要刷屏，不要泄露本地路径、token 或隐藏规则。最终答案不要逐条复述所有进度。
```

- [ ] **Step 4: Verify prompt/skill tests pass**

Run:

```bash
.venv/bin/pytest -q tests/test_prompting.py tests/test_runtime_skill.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/prompting.py src/qq_agent_bridge/runtime_skill.py skills/cursor-qq-runtime/SKILL.md tests/test_prompting.py tests/test_runtime_skill.py
git commit -m "Teach Cursor QQ progress directives"
```

---

### Task 8: App Heartbeat Integration

**Files:**
- Modify: `tests/test_app_async.py`
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `src/qq_agent_bridge/progress.py`

- [ ] **Step 1: Write failing heartbeat integration tests**

Add to `tests/test_app_async.py`:

```python
def test_silent_task_sends_heartbeat_before_final_answer() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            await asyncio.sleep(1.2)
            return "最终结果"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task long", group="group", mid="heartbeat-1"))
        await wait_until_sent(adapter, "还在处理")
        await wait_until_sent(adapter, "最终结果")

    asyncio.run(go())


def test_stop_cancels_heartbeat_for_long_task() -> None:
    async def go() -> None:
        adapter = FakeAdapter()
        cfg = make_cfg()
        cfg.progress.first_heartbeat_seconds = 1
        cfg.progress.heartbeat_seconds = 1
        release = asyncio.Event()

        async def fake_cursor(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            await release.wait()
            return "done"

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)
        app.cursor.run = fake_cursor  # type: ignore[method-assign]

        await app._handle(make_ev("/task long", group="group", mid="stop-heartbeat"))
        jid = next(iter(app.policy.jobs))
        await app._handle(make_ev(f"/stop {jid}", sender="owner", group="group", mid="stop-heartbeat-2"))
        await asyncio.sleep(1.2)

        assert not any("还在处理" in item[2] for item in adapter.sent)

    asyncio.run(go())
```

- [ ] **Step 2: Run failing heartbeat integration tests**

Run:

```bash
.venv/bin/pytest -q tests/test_app_async.py::test_silent_task_sends_heartbeat_before_final_answer tests/test_app_async.py::test_stop_cancels_heartbeat_for_long_task
```

Expected: FAIL if heartbeat timing or cancellation is not wired.

- [ ] **Step 3: Fix heartbeat behavior**

Adjust `ProgressReporter.run_heartbeat()` to:

- check `done()` immediately after waking,
- call `send()` inside `try/except Exception` and continue,
- update `last_sent_at` after successful heartbeat,
- exit when `stop()` is called.

Adjust `_reply_when_done()` cleanup so `reporter.stop()` runs for normal result, timeout result, errors, and cancellation.

- [ ] **Step 4: Verify heartbeat tests pass**

Run:

```bash
.venv/bin/pytest -q tests/test_progress.py tests/test_app_async.py::test_silent_task_sends_heartbeat_before_final_answer tests/test_app_async.py::test_stop_cancels_heartbeat_for_long_task
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/main.py src/qq_agent_bridge/progress.py tests/test_app_async.py tests/test_progress.py
git commit -m "Send heartbeat updates for silent long tasks"
```

---

### Task 9: Regression And Documentation

**Files:**
- Modify: `README.md`
- Test: full suite

- [ ] **Step 1: Update README**

Add under the runtime notes:

```markdown
- Long `/task` and `/code` jobs can send intermediate progress with `QQBOT_PROGRESS: <message>`. The bridge strips these directives from the final answer and rate-limits messages.
- If a long job is silent, the bridge sends low-frequency heartbeat messages so QQ users know the task is still running.
```

- [ ] **Step 2: Run full verification**

Run:

```bash
.venv/bin/pytest -q
git diff --check
```

Expected: pytest reports all tests passing and `git diff --check` prints nothing.

- [ ] **Step 3: Commit docs/regression**

```bash
git add README.md
git commit -m "Document long task progress messages"
```

- [ ] **Step 4: Final status check**

Run:

```bash
git status --short
```

Expected: no output.

---

## Self-Review

Spec coverage:

- Agent progress directives: Task 2, Task 5, Task 6, Task 7.
- Bridge heartbeat: Task 3, Task 5, Task 8.
- Reporter-before-Cursor race: Task 4 and Task 5.
- Config defaults: Task 1.
- Prompt/skill contract: Task 7.
- Cancellation and stop behavior: Task 8.
- Final output stripping: Task 2 and Task 6.
- Existing outgoing resources and `/status` regressions: Task 9 full suite covers current tests; add targeted tests during implementation if a regression appears.

Placeholder scan:

- No placeholder markers.
- Each code-changing task includes concrete snippets and commands.

Type consistency:

- `ProgressCallback` uses `Callable[[str], Awaitable[None]]`.
- `ProgressReporter.send_progress()` matches that callback shape.
- `Policy` runner changes from `(cmd, args, ev)` to `Job`; `App._agent_runner(job)` unpacks `cmd`, `args`, and `event`.
