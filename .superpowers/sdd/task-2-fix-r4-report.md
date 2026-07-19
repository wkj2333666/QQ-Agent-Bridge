# Task 2 Fourth Re-review Fix Report

## Status

Fixed the remaining Task 2 P1 on `codex/long-term-memory` with test-first
regressions. Changes are limited to curation normalization, store duplicate-revise
defenses, their focused tests, and this report.

## Files

- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_curation.py`
- `tests/test_long_term_memory.py`
- `.superpowers/sdd/task-2-fix-r4-report.md`

## RED Evidence

### Validator, staged batch, store, and alias behavior

- Command:
  `uv run pytest -q tests/test_memory_curation.py::test_validator_normalizes_self_duplicate_revision_to_reinforcement tests/test_memory_curation.py::test_validator_normalizes_duplicate_revision_to_audited_survivor_merge tests/test_memory_curation.py::test_duplicate_revision_retires_alias_target_from_later_staged_operations tests/test_long_term_memory.py::test_direct_revision_into_duplicate_reinforces_survivor_and_retires_target tests/test_long_term_memory.py::test_duplicate_revision_rolls_back_rows_fts_revisions_and_source_consumption`
- Initial result before production changes: `6 failed`.
- Failures showed validator acceptance of unchanged `revise` operations, later
  alias siblings remaining accepted, direct store revisions retaining the revised
  target instead of the existing duplicate, and no merge audit failure to exercise
  transaction rollback.
- A separate direct self-identity regression initially failed because store
  `revise` rewrote canonical-equivalent content instead of reinforcing the target.

## GREEN Evidence

- New duplicate-revision regression selection: `7 passed`.
- Complete curation and store modules:
  `uv run pytest -q tests/test_memory_curation.py tests/test_long_term_memory.py`
  completed with `139 passed in 1.79s`.
- Required focused suite:
  `uv run pytest -q tests/test_memory_curation.py tests/test_long_term_memory.py tests/test_config.py`
  completed with `172 passed in 0.82s`.
- Full suite outside the restricted network namespace:
  `uv run pytest -q`
  completed with `803 passed, 12 skipped in 13.40s`.
- `git diff --check` completed with no errors before this report was added.

## Finding Review

1. A canonical-equivalent self revision now becomes `reinforce` against the
   canonical full target ID. Content is not rewritten, source count increments,
   and confidence is max-preserved by the existing reinforcement path.
2. A revision matching a different same-sensitivity item now becomes one audited
   `merge` with the existing duplicate as survivor and the revised target as the
   related retired item. The accepted operation carries canonical full IDs,
   revision confidence, provenance, and actor class.
3. Merge staging retires related targets immediately and stages the survivor's
   reinforced confidence, score, status, provenance, and source count. Later
   siblings using either the retired target's full or short ID therefore receive
   `target_not_found`, matching commit behavior.
4. Direct store `revise` performs the same canonical checks defensively. A self
   match routes through reinforcement; a different match routes through merge.
   The merge keeps the existing duplicate's ID and content, increments support by
   each actually retired row, max-preserves confidence and score, reactivates a
   candidate or dormant survivor, and removes the revised target.
5. Target retirement, survivor update, FTS deletion/resynchronization, revision
   audit, source consumption, and review-run recording remain in one transaction.
   The injected merge-audit failure regression proves rollback restores both rows,
   both FTS entries, prior revisions, and the pending source.

## Concerns

- The restricted network namespace stalls in the unrelated existing
  `test_default_http_fetch_rejects_loopback_targets` resolver call. The full suite
  completed outside that namespace; the test only validates rejection of numeric
  loopback and does not contact a product endpoint.
- No unresolved in-scope findings remain.
