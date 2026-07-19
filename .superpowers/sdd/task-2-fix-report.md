# Task 2 Fix Report

## Status

Fixed all four Task 2 review findings on `codex/long-term-memory` using test-first
regressions. No app routing or unrelated modules were changed.

## Files

- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `src/qq_agent_bridge/long_term_memory_models.py`
- `tests/test_memory_curation.py`

## RED Evidence

- Command: `UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest tests/test_memory_curation.py -q`
- Result before production changes: `22 failed, 56 passed in 1.79s`.
- Expected failures covered English natural-language credential assignments at
  collection and validation, attacker-controlled target metadata for revise and
  contradict with and without an actor, mismatch rejection for all target metadata,
  missing target resolvers, sibling commit isolation, and source-free group memory.
- The initial command without `UV_CACHE_DIR` did not run tests because the managed
  environment exposes `/home/wkj/.cache/uv` read-only; it is not counted as RED.

## GREEN Evidence

- New regression file: `78 passed in 0.31s`.
- Required focused suite:
  `UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest tests/test_memory_curation.py tests/test_long_term_memory.py tests/test_config.py -q`
  completed with `123 passed in 0.71s`.
- Full suite: `UV_CACHE_DIR=/tmp/qq-bot-uv-cache uv run pytest -q` completed with
  `754 passed, 12 skipped in 13.19s`.
- `git diff --check` completed with no errors before the final review.

## Finding Review

1. Secret detection remains one shared `_contains_secret` policy used by collection
   and proposal validation. It now rejects spaced, underscored, and hyphenated
   English credential labels with natural `is` assignments while retaining Chinese
   and punctuation forms. Rejection paths do not log source or proposal content.
2. Every stateful proposal resolves its scoped store target before authorization.
   Supplied subject kind, subject ID, category, or sensitivity must match exactly;
   omitted values are copied from the target. Source and actor authority therefore
   apply to the target subject. Store revise/contradict writes also preserve target
   metadata defensively.
3. Stateful proposals without a store/resolver are rejected with
   `target_resolver_required`; missing targets are rejected individually. A mixed
   add/missing-revise regression confirms the add remains accepted and commits.
4. Group-subject additions and mutations require nonempty eligible evidence from
   the validated scope. A source-free group proposal is rejected with
   `source_evidence_required`.

## Concerns

- `MemoryProposal.sensitivity` is now optional only to distinguish omitted mutation
  metadata from an explicitly supplied `normal` mismatch. Accepted additions are
  normalized to `normal`, and stateful proposals are normalized from their target.
- The managed network sandbox stalls in the unrelated loopback-resolution guard
  test. The isolated guard test and full suite passed outside that restriction; no
  network service was contacted by the guard test.
