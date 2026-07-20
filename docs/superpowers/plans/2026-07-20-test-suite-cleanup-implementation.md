# Test Suite Cleanup + Capability Tests + CI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove ~150-200 waste tests, add 3 capability tests that catch real bugs, add GitHub Actions CI.

**Architecture:** Nine waste patterns addressed in priority order. Capability tests go in existing test files. CI is a single-job workflow file.

**Tech Stack:** pytest, asyncio, PyYAML, GitHub Actions

## Global Constraints

- All existing tests must continue to pass after each task
- Run `uv run pytest -xq` after every task, fix before continuing
- Commit after each completed task
- Don't rewrite tests that work — only delete/merge waste ones

---

### Task 1: Delete always-skipped and doc-testing files

**Files:**
- Delete: `tests/test_agent_capability_eval.py` (168 lines, 2 tests, always skips unless env var set, 1 assertion depending on live LLM)
- Delete: `tests/test_visual_media_skill.py` (268 lines, 10 tests, tests markdown doc content, not code behavior)

**Interfaces:** None

- [ ] **Step 1: Delete the files**

```bash
rm tests/test_agent_capability_eval.py tests/test_visual_media_skill.py
```

- [ ] **Step 2: Run tests to verify nothing breaks**

Run: `uv run pytest -xq`
Expected: all remaining tests pass (these were always skipped or testing docs)

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m 'chore: delete always-skipped and doc-testing test files

Remove test_agent_capability_eval.py (always skipped, depends on live LLM)
and test_visual_media_skill.py (tests markdown doc content, not code)'
```

---

### Task 2: Delete mock-testing-mock tests from test_proactive.py

**Files:**
- Modify: `tests/test_proactive.py`

**Interfaces:** None (deletion only)

- [ ] **Step 1: Delete the 4 mock-testing-mock tests**

Remove these functions entirely from `tests/test_proactive.py`:

1. `test_proactive_ignores_messages_from_bot_self` (line ~560) — asserts FakeCursor.calls == 0
2. `test_proactive_reset_chat_clears_pending_batch_and_timer` (line ~618) — asserts private `_batches` and `_timers`
3. `test_proactive_skips_blacklisted_or_command_like_messages` (line ~959) — asserts FakeCursor.calls == 0
4. `test_proactive_drops_internal_prompt_echo_from_llm` (line ~1027) — asserts sent == [] based on hardcoded FakeCursor output

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_proactive.py -xq`
Expected: 32 tests pass

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m 'chore: delete mock-testing-mock tests from test_proactive.py

Remove 4 tests that only verify mock internal state (calls, private fields)
rather than observable system behavior'
```

---

### Task 3: Delete trivial tests from test_app_async.py, test_agent_runtime.py, test_redact.py, test_onebot.py

**Files:**
- Modify: `tests/test_app_async.py`
- Modify: `tests/test_agent_runtime.py`
- Modify: `tests/test_redact.py`
- Modify: `tests/test_onebot.py`

**Interfaces:** None (deletion only)

- [ ] **Step 1: Delete trivial tests from test_app_async.py**

Remove these functions (all have 0-1 assertions, test constructor tautologies or template content):

1. `test_handle_returns_before_ask_job_finishes` — 0 assertions on actual behavior
2. `test_app_uses_configured_custom_agent_runtime` — 1 isinstance assertion, constructor tautology
3. `test_agent_subsystems_use_gated_adapter` — 3 identity assertions on wiring, tautology
4. `test_help_lists_reset_command` — 1 string-containment check on help template
5. `test_reply_cleanup_requests_only_pressure_check` — asserts mock was called
6. `test_ask_does_not_pass_progress_callback` — asserts mock received None

- [ ] **Step 2: Delete trivial tests from test_agent_runtime.py**

Remove:
1. `test_agent_runtime_factory_does_not_default_to_cursor` — 1 isinstance assert, tautology
2. `test_disabled_agent_runtime_explains_missing_config` — 1 assertion on hardcoded error string

- [ ] **Step 3: Delete trivial tests from test_redact.py**

Remove:
1. `test_strip_ansi` — 1 assert testing single regex replacement
2. `test_extra_redaction_ignores_oversized_patterns_without_regex_failure` — tests Python re module behavior

- [ ] **Step 4: Delete trivial tests from test_onebot.py**

Remove:
1. `test_extract_array` — 1 assertion, redundant with normalize tests onward
2. `test_send_uses_exactly_one_connected_gateway` — asserts FakeConn internal state

- [ ] **Step 5: Run tests**

Run: `uv run pytest -xq`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m 'chore: delete trivial tests (constructor tautologies, mock assertions, error strings)

Removed 12 tests across test_app_async, test_agent_runtime, test_redact, test_onebot
that test constructor wiring, mock internal state, or cosmetic details'
```

---

### Task 4: Merge identical-structure tests in test_onebot.py

**Files:**
- Modify: `tests/test_onebot.py`

**Interfaces:**
- Produces: parametrized `test_normalize_event` combining 13 former tests

- [ ] **Step 1: Create parametrized test to replace 13 normalize tests**

Replace all 13 individual `test_normalize_*` tests with a single parametrized test. Each case is a tuple of `(raw_dict, expected_checks)` where `expected_checks` is a dict of field names to expected values:

```python
@pytest.mark.parametrize(
    "raw_dict,self_id,expected_checks",
    [
        # Private text message
        (
            {"type": "message", "sub_type": "friend", "message_id": 1001,
             "user_id": 12345, "raw_message": "hello",
             "message": [{"type": "text", "text": "hello"}]},
            "999",
            {"chat_id": "12345", "sender_id": "12345", "is_group": False,
             "text": "hello", "is_self": False},
        ),
        # Group @mention
        (
            {"type": "message", "sub_type": "normal", "message_id": 2001,
             "group_id": 555, "user_id": 111, "raw_message": "[CQ:at,qq=999] hi",
             "message": [{"type": "at", "qq": "999"}, {"type": "text", "text": "hi"}]},
            "999",
            {"chat_id": "555", "sender_id": "111", "is_group": True,
             "mentioned_bot": True, "text": "hi", "is_self": False},
        ),
        # Self message
        (
            {"type": "message_sent", "sub_type": "normal", "message_id": 3001,
             "group_id": 555, "user_id": 999, "raw_message": "bot reply",
             "message": [{"type": "text", "text": "bot reply"}]},
            "999",
            {"chat_id": "555", "sender_id": "999", "is_group": True,
             "is_self": True},
        ),
        # ... add all remaining cases from the 13 original tests
    ],
)
def test_normalize_event_parametrized(raw_dict, self_id, expected_checks):
    result = _normalize_event(raw_dict, self_id)
    for field, expected in expected_checks.items():
        actual = getattr(result, field)
        assert actual == expected, f"{field}: expected {expected}, got {actual}"
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_onebot.py -xq`
Expected: all tests pass with fewer test functions

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m 'refactor: merge 13 normalize tests into parametrized test in test_onebot.py'
```

---

### Task 5: Merge duplicate coverage tests in test_app_async.py

**Files:**
- Modify: `tests/test_app_async.py`

**Interfaces:** None (merge within same file)

- [ ] **Step 1: Merge echo_only tests**

Replace `test_echo_only_ignores_unmentioned_group_messages` and `test_echo_only_replies_to_mentioned_group_messages` with:

```python
@pytest.mark.parametrize(
    "mentioned,expect_reply",
    [
        (False, False),  # unmentioned → no reply
        (True, True),    # mentioned → reply
    ],
)
def test_echo_only_replies_only_to_mentioned(mentioned, expect_reply):
    adapter = FakeAdapter()
    cfg = make_cfg(echo_only=True)
    app = make_app(cfg, adapter)
    ev = make_ev(text="hello", is_group=True, mentioned_bot=mentioned)
    asyncio.run(_handle_and_wait(app, ev))
    assert bool(adapter.sent) == expect_reply
```

- [ ] **Step 2: Merge env-error tests in test_cursor_adapter.py**

Replace `test_cursor_run_fails_closed_when_required_env_is_disabled`, `_is_not_micromamba`, `_name_is_not_base` with:

```python
@pytest.mark.parametrize(
    "env_runner,env_name,expected_error",
    [
        ("", "base", "[error] 助手环境未配置"),        # disabled
        ("/bin/bash", "base", "[error] 助手环境未配置"), # not micromamba
        ("micromamba", "custom", "[error] 助手环境未配置"), # wrong env name
    ],
)
def test_cursor_run_fails_when_env_misconfigured(env_runner, env_name, expected_error):
    ...
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_app_async.py tests/test_cursor_adapter.py -xq`
Expected: all tests pass

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m 'refactor: merge duplicate-structure tests in test_app_async and test_cursor_adapter'
```

---

### Task 6: Add command registration integrity test

**Files:**
- Modify: `tests/test_policy.py`

**Interfaces:**
- Consumes: `BridgeConfig.load()`, `policy.COMMANDS`
- Produces: `test_all_config_commands_are_registered()`

- [ ] **Step 1: Write the test**

Add to `tests/test_policy.py`:

```python
def test_all_config_commands_are_registered() -> None:
    """Every command listed in config.yaml and config.example.yaml must
    be in the COMMANDS set so that /reboot-class bugs don't ship again."""
    from qq_agent_bridge.policy import COMMANDS

    root = Path(__file__).resolve().parents[1]
    missing: dict[str, set[str]] = {}
    for config_name in ("config.yaml", "config.example.yaml"):
        config_path = root / config_name
        if not config_path.exists():
            continue
        cfg = BridgeConfig.load(config_path)
        for name in cfg.commands:
            if name not in COMMANDS:
                missing.setdefault(config_name, set()).add(name)

    assert missing == {}, (
        f"Commands in config but not in policy.COMMANDS: {missing}"
    )
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_policy.py::test_all_config_commands_are_registered -v`
Expected: PASS (all config commands are now registered after our /reboot fix)

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m 'test: add command registration integrity check

Verifies every command in config.yaml and config.example.yaml
is in policy.COMMANDS to prevent /reboot-class bugs'
```

---

### Task 7: Add memory curator end-to-end capability test

**Files:**
- Modify: `tests/test_memory_review.py`

**Interfaces:**
- Consumes: `MemoryCurator`, `MemoryValidator`, `FakeAgent`, `LongTermMemoryStore`
- Produces: `test_curator_accepts_markdown_wrapped_json_output()`

- [ ] **Step 1: Write the test**

Add to `tests/test_memory_review.py`:

```python
def test_curator_accepts_markdown_wrapped_json_output(
    tmp_path: Path, cfg: BridgeConfig, store: LongTermMemoryStore,
) -> None:
    """Full pipeline: model returns markdown-wrapped JSON → parse → validate → commit."""
    scope = MemoryScope("group", "g1")
    store.initialize()
    store.enable_scope(scope, enabled=True)

    source = MemorySource(
        id=1, scope_kind="group", scope_id="g1",
        message_id="m1", sender_id="u1",
        text="我喜欢喝咖啡", message_timestamp=1000,
    )
    store.collect(source)

    # Model output wrapped in markdown fence with trailing prose
    model_output = (
        '```json\n'
        '{"operations":['
        '{"operation":"add","source_ids":[1],'
        '"subject_kind":"user","subject_id":"u1",'
        '"category":"preference","content":"喜欢喝咖啡",'
        '"confidence":0.91,"status":"active",'
        '"sensitivity":"normal","source_kind":"self_statement",'
        '"explicit_memory":false,"decay_exempt":false,"expires_at":null}'
        ']}\n'
        '```\n'
        '这是提取的记忆。'
    )

    curator = MemoryCurator(
        FakeAgent(model_output),
        MemoryValidator(cfg, store=store),
        cfg.long_term_memory.review,
        workspace=tmp_path,
    )

    outcome = asyncio.run(curator.review(scope, (source,), ()))

    assert outcome.error is None
    assert outcome.proposed_count == 1
    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].content == "喜欢喝咖啡"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_memory_review.py::test_curator_accepts_markdown_wrapped_json_output -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m 'test: add memory curator end-to-end capability test

Verifies full pipeline accepts markdown-wrapped JSON output from model,
catching the malformed_output bug that shipped previously'
```

---

### Task 8: Add GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Produces: CI job that runs on push + PR to main

- [ ] **Step 1: Create the workflow file**

```bash
mkdir -p .github/workflows
```

Write `.github/workflows/test.yml`:

```yaml
name: Test

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install Python
        run: uv python install 3.13

      - name: Install dependencies
        run: uv sync --frozen

      - name: Run tests
        run: uv run pytest -q --timeout 120
```

- [ ] **Step 2: Commit and push (CI triggers on push)**

```bash
git add -A && git commit -m 'ci: add GitHub Actions test workflow

Runs uv run pytest on push and PR to main, 10-min timeout'
git push
```

- [ ] **Step 3: Verify CI passes on GitHub**

Check: https://github.com/wkj2333666/QQ-Agent-Bridge/actions
Expected: workflow completes successfully, all tests green

---

### Task 9: Run final full test suite and verify

- [ ] **Step 1: Final test run**

Run: `uv run pytest -xq`
Expected: all tests pass, fewer total tests than before (cleanup removed ~30+)

- [ ] **Step 2: Check test count**

Run: `uv run pytest --co -q 2>&1 | tail -1`
Expected: ~1490 tests (down from 1526)

- [ ] **Step 3: Final commit if needed**

```bash
git status
# Should be clean if all previous commits were made
```
