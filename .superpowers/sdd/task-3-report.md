# Task 3 Report: Curator and Low-Priority Review Coordinator

## Status

Implemented on `codex/long-term-memory`. App routing remains intentionally deferred.

## Changed Files

- `src/qq_agent_bridge/memory_review.py`
  - Added the constrained JSON-only `MemoryCurator`, metadata-only outcomes/logging,
    bounded timeout/output/prompt handling, and deterministic validation handoff.
  - Added production construction through a dedicated restricted Agent config:
    bwrap required, network disabled, ask workspace allowlisted read-only, task tools
    disabled, resources/progress/traces disabled, and independent runtime/output caps.
  - Added the serialized `MemoryReviewCoordinator` with threshold+idle, periodic,
    explicit, retry/backoff, periodic-only max-attempt deferral, cancellation,
    TTL/decay maintenance, reload, restart recovery, and clean shutdown behavior.
- `src/qq_agent_bridge/storage_gate.py`
  - Added focused construction of a gated Agent adapter from a deep-copied,
    restricted `BridgeConfig`.
- `src/qq_agent_bridge/long_term_memory.py`
  - Added due-source/scope discovery helpers and periodic-deferred scope discovery.
  - Added optional review audit metadata while preserving atomic accepted-operation
    commit and consumed-source deletion.
- `tests/test_memory_review.py`
  - Added 19 curator, restriction, scheduling, retry, cancellation, atomicity,
    maintenance, reload, and shutdown tests.

## RED Evidence

Initial curator test run:

```text
uv run pytest tests/test_memory_review.py -q
ModuleNotFoundError: No module named 'qq_agent_bridge.memory_review'
```

Coordinator test run before coordinator implementation:

```text
ImportError: cannot import name 'MemoryReviewCoordinator'
```

Subsequent RED cycles caught and drove fixes for:

- periodic recovery below the ordinary minimum after `max_attempts`;
- authority-safe validation in explicit review fixtures;
- due-scope hot looping, trigger audit metadata, and explicit-review shutdown;
- cleanup/review exclusion and queued-review shutdown;
- reload of curator limits and maintenance timing;
- threshold review incorrectly reactivating periodic-only deferred rows.

Each cycle failed on the asserted missing behavior before the corresponding
production change.

## GREEN Evidence

Task 3 focused tests:

```text
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest tests/test_memory_review.py -q
19 passed in 0.30s
```

Task 3 plus Task 1/2, config, and storage-gate regressions:

```text
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest \
  tests/test_memory_review.py tests/test_memory_curation.py \
  tests/test_long_term_memory.py tests/test_config.py tests/test_storage_gate.py -q
231 passed in 1.40s
```

Full suite excluding the single pre-existing environment hang:

```text
UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q \
  -k 'not default_http_fetch_rejects_loopback_targets'
855 passed, 12 skipped, 1 deselected in 13.94s
```

The isolated blocker was bounded and stopped:

```text
timeout 5s env UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest \
  tests/test_resources.py::test_default_http_fetch_rejects_loopback_targets -q
exit 124, no test output
```

The same test hung in the clean pre-change baseline at the same point. No pytest
process remained after the timeout.

## Concerns

- `tests/test_resources.py::test_default_http_fetch_rejects_loopback_targets`
  hangs in this restricted environment's hostname-resolution path. It is unrelated
  to Task 3 and was left unchanged.
- App construction/routing is intentionally not integrated in this task. The
  `build_memory_review_coordinator` factory is ready for that later wiring and
  enforces the restricted production Agent configuration.
