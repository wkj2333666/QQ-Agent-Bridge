# Task 2 Re-review Fix Report

## Status

Fixed all three Task 2 re-review findings on `codex/long-term-memory` with
test-first regressions. Changes remain limited to curation, store/model identity,
and memory curation tests.

## Files

- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `src/qq_agent_bridge/long_term_memory_models.py`
- `tests/test_memory_curation.py`

## RED Evidence

- Initial regression command:
  `.venv/bin/python -m pytest tests/test_memory_curation.py -q -k 'collector_rejects_ineligible_or_secret_material or validator_rejects_shared_secret_assignment_variants or cross_sensitivity_content_collision or forget_then_revise or merge_then_revise or contradict_then_revise'`
- Initial result before production changes: `13 failed, 20 passed, 58 deselected`.
- Failures demonstrated collector and validator acceptance of `password is 1234`,
  `password equals swordfish`, short Chinese assignments, cross-sensitivity
  activation at both validator and store boundaries, and stale validation after
  forget, merge, and contradiction.
- Self-review RED for low-confidence conversion:
  `1 failed, 1 passed`; the candidate-form add bypassed collision validation.
- Self-review RED for expired duplicate visibility:
  `2 failed, 2 passed`; validator retrieval omitted rows that the store duplicate
  check still considered.

## GREEN Evidence

- Required focused suite:
  `.venv/bin/python -m pytest tests/test_memory_curation.py tests/test_long_term_memory.py tests/test_config.py -q`
  completed with `139 passed in 1.23s`.
- Final full suite outside the restricted network namespace:
  `.venv/bin/python -m pytest -q` completed with
  `770 passed, 12 skipped in 13.40s`.
- `git diff --check` completed with no errors.

## Finding Review

1. The shared secret detector now rejects credential-label assignments using
   `is`, `equals`, ASCII/full-width punctuation, and Chinese `是`, `为`, and
   `等于` forms as soon as a non-whitespace value begins. Value length is no
   longer part of the credential-label policy. Neither rejection path logs content.
2. Validator and store now use one canonical identity key covering subject,
   category, normalized content, and sensitivity. Cross-sensitivity matches are
   rejected by the validator and defensively raise inside the store transaction,
   preserving the sensitive candidate and pending source. Candidate conversion and
   expired-row visibility use the same collision policy.
3. Validation maintains a staged item overlay for accepted stateful operations.
   Forget and merge remove staged targets; contradiction marks a staged terminal
   status; revise and reinforce stage their resulting status/content. Later
   operations are rejected individually, while earlier adds, forgets, merges, and
   contradictions remain committable.

## Self-review

- Confirmed duplicate identity defaults match store insertion defaults for category
  and sensitivity.
- Confirmed staged low-confidence revise/contradict conversions do not mutate their
  original targets.
- Confirmed cross-sensitivity rejection rolls back item mutation and source
  consumption at the defensive store boundary.
- No unresolved in-scope findings remain.

## Concerns

- The managed network sandbox stalls in the unrelated loopback resolver guard test.
  A sandboxed run excluding that test passed `766 passed, 12 skipped, 1 deselected`,
  the isolated guard passed outside the restriction, and the final unrestricted
  full suite passed. No product network endpoint was contacted by the guard test.
