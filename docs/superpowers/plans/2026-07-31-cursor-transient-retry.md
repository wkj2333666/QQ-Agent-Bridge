# Cursor Transient Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make recoverable Cursor provider startup failures retry automatically while task jobs prefer `composer-2.5`.

**Architecture:** `CursorAdapter` classifies only two narrowly defined provider failures as transient, then recursively reruns the same request with a bounded retry counter and increasing delay. Existing usage-limit fallback remains the owner of explicit-model-to-Auto switching. The private production configuration selects `composer-2.5`; repository defaults remain unchanged.

**Tech Stack:** Python 3.13, asyncio subprocesses, pytest, YAML configuration.

## Global Constraints

- Preserve all existing uncommitted sandbox authentication and minimal `/dev` binding changes.
- Retry only the empty Auto model registry response and TLS pre-handshake socket disconnect.
- Permit two retries after the initial attempt.
- Do not retry invalid explicit models, authentication failures, storage errors, timeouts, cancellations, or arbitrary process exits.
- Do not restart the production bridge automatically.

---

### Task 1: Transient Provider Error Classification

**Files:**
- Modify: `tests/test_cursor_adapter.py`
- Modify: `src/qq_agent_bridge/cursor_adapter.py`

**Interfaces:**
- Produces: `CursorAdapter._is_transient_provider_error(text: str) -> bool`
- Consumes: cleaned Cursor subprocess stderr text.

- [ ] **Step 1: Write the failing classification test**

Add a table-driven test with literal expectations:

```python
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Cannot use this model: auto. Available models: \n", True),
        (
            "Error: [aborted] Client network socket disconnected before "
            "secure TLS connection was established",
            True,
        ),
        (
            "Cannot use this model: bad-id. Available models: auto, composer-2.5",
            False,
        ),
        ("authentication required", False),
    ],
)
def test_transient_provider_error_detection(message: str, expected: bool) -> None:
    assert CursorAdapter._is_transient_provider_error(message) is expected
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/test_cursor_adapter.py::test_transient_provider_error_detection -q
```

Expected: failure because `_is_transient_provider_error` does not exist.

- [ ] **Step 3: Implement the minimal classifier**

Normalize whitespace and lowercase text. Return true for the exact TLS
pre-handshake phrase. For model errors, match only `auto` and require the
normalized message to end at `available models:` so a non-empty model list is
not treated as transient.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
uv run pytest tests/test_cursor_adapter.py::test_transient_provider_error_detection -q
```

Expected: four passing cases.

### Task 2: Bounded Retry Behavior

**Files:**
- Modify: `tests/test_cursor_adapter.py`
- Modify: `src/qq_agent_bridge/cursor_adapter.py`

**Interfaces:**
- Extends: `CursorAdapter.run(..., _transient_attempt: int = 0) -> str`
- Consumes: `_is_transient_provider_error(cleaned)`.
- Produces: at most three total subprocess attempts, retry trace suffixes, and progress callbacks.

- [ ] **Step 1: Write a failing empty-model retry integration test**

Create an executable fake Cursor shell script that stores its attempt count in
the temporary workspace, emits the empty Auto model error on the first call,
then prints `recovered result`. Disable bwrap and micromamba as existing adapter
tests do. Assert that `run()` returns `recovered result` and the count is `2`.
Monkeypatch `asyncio.sleep` to an async recorder so the test does not wait.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/test_cursor_adapter.py::test_transient_auto_model_failure_retries_then_succeeds -q
```

Expected: result is `[error] 助手执行失败` and only one process attempt occurs.

- [ ] **Step 3: Implement one recursive retry path**

Add `_transient_attempt` as an optional keyword-only internal argument to
`run()`. On a classified transient process exit while the counter is below
`2`:

```python
delay = 2**_transient_attempt
await asyncio.sleep(delay)
return await self.run(
    prompt,
    ws,
    mode,
    model=model,
    progress=progress,
    trace_id=retry_trace_id,
    redact_extra=redact_extra,
    _transient_attempt=_transient_attempt + 1,
)
```

Log the retry without exposing the prompt. If a progress callback exists, emit
`助手连接暂时不稳定，正在重试（N/2）。`.

- [ ] **Step 4: Run the first retry test and verify GREEN**

Run:

```bash
uv run pytest tests/test_cursor_adapter.py::test_transient_auto_model_failure_retries_then_succeeds -q
```

Expected: pass with two total process attempts.

- [ ] **Step 5: Write a failing retry-bound test**

Create a fake Cursor script that always emits the TLS startup error and appends
one line to an attempt file. Assert the final result remains
`[error] 助手执行失败`, the attempt file has exactly three lines, and recorded
sleep delays are `[1, 2]`.

- [ ] **Step 6: Run the retry-bound test**

Run:

```bash
uv run pytest tests/test_cursor_adapter.py::test_transient_tls_failure_stops_after_two_retries -q
```

Expected after the minimal implementation: pass. If it fails, correct only the
counter, delay, or recursive argument propagation.

- [ ] **Step 7: Protect usage-limit fallback composition**

Extend the existing usage-limit fake so the explicit model reports exhausted
usage, the first Auto attempt reports an empty model list, and the second Auto
attempt succeeds. Assert the successful result. This proves fallback resets
the transient retry budget for Auto.

- [ ] **Step 8: Run all Cursor adapter tests**

Run:

```bash
uv run pytest tests/test_cursor_adapter.py -q
```

Expected: all adapter tests pass, including the pre-existing uncommitted
sandbox tests.

### Task 3: Production Configuration and Verification

**Files:**
- Modify: `config.yaml`
- Verify: `src/qq_agent_bridge/cursor_adapter.py`
- Verify: `tests/test_cursor_adapter.py`

**Interfaces:**
- Consumes: Cursor model ID `composer-2.5`.
- Preserves: repository example/default runtime settings.

- [ ] **Step 1: Change only the private production task model**

Set:

```yaml
agent:
  chat_model: "auto"
  task_model: "composer-2.5"
```

Do not change `config.example.yaml` for this deployment-specific model choice.

- [ ] **Step 2: Run formatting and focused tests**

Run:

```bash
uv run ruff check src/qq_agent_bridge/cursor_adapter.py tests/test_cursor_adapter.py
uv run pytest tests/test_cursor_adapter.py tests/test_config.py -q
```

Expected: clean lint output and all focused tests passing.

- [ ] **Step 3: Run the full suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass. Environment-marked tests may skip according to their
existing markers; no new failures are acceptable.

- [ ] **Step 4: Review the final diff**

Confirm that only the retry implementation/tests, private model setting, and
the already-present user sandbox changes are modified. Confirm no tokens,
prompts, QQ messages, or trace payloads entered tracked files.

- [ ] **Step 5: Report the operational handoff**

State that code and configuration are verified but the running bridge still
uses its startup configuration until the user executes `/reload` or restarts
the service. Do not restart it automatically.
