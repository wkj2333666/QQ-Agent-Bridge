# Task 3 Fix Report: Harden Memory Review Isolation

## Status

All three Task 3 review findings are fixed on `codex/long-term-memory`.

## Isolation And Tool Policy

- The restricted production builder now creates a fresh `0700` curator workspace
  directly under `~/.local/state/qq-agent-bridge` and ignores the caller's task or
  project workspace.
- The curator workspace is empty at construction, is the only allowlisted/default
  workspace, and is passed to both the adapter and `MemoryCurator`.
- The restricted builder forces the Cursor adapter, outer bwrap,
  `share_network=False`, ask-only hardened mode, and inner Cursor
  `--sandbox enabled` even inside bwrap.
- The full command test verifies `--unshare-net`, a read-only curator workspace
  mount, no project/runtime-skill path, and no `--force`, `--trust`,
  `--auto-review`, or `--approve-mcps` flags.
- Normal task/code/ask behavior is unchanged because `hardened_read_only` defaults
  to `False` and is enabled only on the deep-copied curator configuration.

## Content-Free Failure Logs

- Added `AgentConfig.log_subprocess_output`, defaulting to `True` to preserve
  ordinary adapter diagnostics.
- The restricted curator sets it to `False`. Nonzero exits then log only a bounded
  error classification and exit code; exception paths log only the exception
  class, and usage-limit fallback does not log the selected model.
- Caplog coverage emits stdout/stderr containing exact QQ-derived text, a
  substring, normalized text, paraphrase-like text, model output, and prompt text.
  The restricted log is asserted to contain exactly:

```text
agent process failed: error_class=process_exit exit_code=42
```

- Curator traces remain disabled, and curator/coordinator logs retain their
  existing hashed/count-only metadata.

## Per-Source Failure Scheduling

- Failure deadlines are now derived from each source's own resulting attempt
  count. Fresh/retrying sources receive bounded exponential backoff; sources that
  reach `max_attempts` receive periodic-only deferral.
- `LongTermMemoryStore.mark_review_failures` validates all selected sources, then
  increments attempts, writes individual deadlines, and inserts one review audit
  row inside one `BEGIN IMMEDIATE` transaction.
- The legacy homogeneous `mark_review_failure` API delegates to the new atomic
  implementation.
- Tests cover homogeneous fresh and exhausted behavior, a mixed batch, restart
  persistence, source retention, and rollback of both source state and audit data
  when any selected source is invalid.

## TDD Evidence

Isolation RED:

```text
6 failed: missing hardened config, inner sandbox disabled, non-ask modes accepted,
and the production builder reused the project workspace.
```

Logging RED:

```text
1 failed: the warning contained stdout plus exact, substring, normalized, and
paraphrase-like sensitive stderr/model text.
```

Scheduling RED:

```text
3 failed: mixed outcomes used 1600 instead of 1060/1120, and no atomic per-source
store API existed.
```

Each focused red test was rerun green after its minimal production change.

## Verification

Requested focused suites:

```text
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q \
  tests/test_memory_review.py tests/test_memory_curation.py \
  tests/test_long_term_memory.py tests/test_config.py \
  tests/test_storage_gate.py tests/test_storage_maintenance.py \
  tests/test_cursor_adapter.py tests/test_agent_trace.py
325 passed in 2.34s
```

Full runnable suite:

```text
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q \
  -k 'not default_http_fetch_rejects_loopback_targets'
864 passed, 12 skipped, 1 deselected in 15.06s
```

Compilation:

```text
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run python -m compileall -q src tests
exit 0
```

## Concern

`tests/test_resources.py::test_default_http_fetch_rejects_loopback_targets`
still hangs in this restricted environment's hostname-resolution path. The
unfiltered full suite reached 82% with no failures before its 120-second bound;
the isolated test also produced no output before a 10-second timeout. The same
environmental blocker is documented in the original Task 3 report and is outside
this change.

## Commit

Requested commit message: `fix: harden memory review isolation`.
