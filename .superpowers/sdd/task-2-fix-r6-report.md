# Task 2 Sixth Re-review Fix Report

## Status

Fixed the Task 2 sixth re-review P1 on `codex/long-term-memory` with a
test-first candidate reinforcement path. Repeated identical low-confidence
`add` and `mark_candidate` proposals now reinforce one targetless candidate
instead of creating a candidate chain, including after a store restart.

## Files

- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_curation.py`
- `tests/test_long_term_memory.py`
- `.superpowers/sdd/task-2-fix-r6-report.md`

## RED Evidence

The new validator and direct-store regressions cover every first/second
permutation of low-confidence `add` and `mark_candidate`, both with no existing
fact and with an established matching fact. Each case closes and reopens the
store between proposals.

```text
.venv/bin/pytest -q \
  tests/test_memory_curation.py::test_repeated_low_confidence_candidate_permutations_survive_restart_without_chains \
  tests/test_long_term_memory.py::test_direct_repeated_low_confidence_candidate_permutations_do_not_chain
```

After correcting a test-only `sqlite3.Row` comparison, the pre-fix result was
`8 failed, 8 passed in 0.80s`. All eight targetless cases failed at the expected
boundary: validator proposals acquired the first candidate's ID as
`candidate_target_id`, while direct store calls persisted a second candidate.
All eight fact-backed cases already passed, proving the compatibility baseline.

## Implementation

1. Validator target selection now distinguishes a matching candidate from an
   established fact. A targetless candidate keeps its null target; active or
   dormant matching facts can still become deterministic candidate targets.
2. Direct store insertion mirrors the same status check before assigning a
   target. Its existing target-aware duplicate lookup then reinforces the
   targetless candidate in place.
3. Target-backed behavior is unchanged. Repeated low-confidence proposals for
   an established fact continue to reinforce the one candidate attached to that
   fact, without mutating the fact or creating a chain.
4. The restart regressions assert one candidate, stable ID, null or fact-backed
   target as appropriate, `source_count == 2`, max-preserved confidence, no
   candidate FTS row, consumed sources, candidate review accounting, and
   `candidate` followed by `reinforce` revision history.

## GREEN Evidence

- New regressions plus prior target-backed coverage: `21 passed in 0.43s`.
- Required focused suite:
  `.venv/bin/pytest -q tests/test_memory_curation.py tests/test_long_term_memory.py tests/test_config.py`
  completed with `206 passed in 1.41s`.
- Full suite outside the restricted network namespace:
  `.venv/bin/pytest -q` completed with
  `837 passed, 12 skipped in 14.47s`.
- `git diff --check` completed with no errors before the report was added.

## Concerns

- The sandboxed full suite again stalled in the unrelated late resolver safety
  tests after reaching 76%, matching the prior fix report. It was interrupted
  with exit 130; the complete suite then passed outside the restricted network
  namespace.
- No unresolved in-scope concern remains.
