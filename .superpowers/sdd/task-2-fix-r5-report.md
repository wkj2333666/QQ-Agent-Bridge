# Task 2 Fifth Re-review Fix Report

## Status

Fixed the Task 2 fifth re-review P1 on `codex/long-term-memory` with a
test-first candidate-isolation path. Low-confidence content operations now persist
as inspectable candidates without mutating existing active, candidate, or dormant
memories.

## Files

- `src/qq_agent_bridge/long_term_memory_models.py`
- `src/qq_agent_bridge/long_term_memory_schema.py`
- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_curation.py`
- `tests/test_long_term_memory.py`
- `.superpowers/sdd/task-2-fix-r5-report.md`

## RED Evidence

The initial regression selection covered schema migration, direct store behavior,
restart, hard delete, validator normalization, aliases, staged siblings, and FTS:

```text
.venv/bin/pytest -q \
  tests/test_long_term_memory.py::test_v1_migration_adds_candidate_target_and_survives_restart \
  tests/test_long_term_memory.py::test_direct_low_confidence_content_operation_isolates_candidate_and_fts \
  tests/test_long_term_memory.py::test_repeated_candidate_proposal_reinforces_only_candidate_after_restart \
  tests/test_long_term_memory.py::test_hard_delete_nulls_candidate_target_without_cross_scope_effects \
  tests/test_memory_curation.py::test_low_confidence_self_duplicate_revision_is_isolated_candidate \
  tests/test_memory_curation.py::test_low_confidence_distinct_duplicate_change_preserves_every_existing_row \
  tests/test_memory_curation.py::test_low_confidence_duplicate_add_targets_fact_without_reinforcing_it \
  tests/test_memory_curation.py::test_low_confidence_duplicate_revision_keeps_alias_target_for_staged_sibling
```

Initial result: `17 failed`. Failures showed low-confidence revisions becoming
`reinforce` or `merge`, direct low-confidence operations mutating facts, staged
aliases being retired, no persisted target representation, and candidate loss on
hard delete.

A later review-accounting assertion failed `3` parameter cases because direct
demotion persisted a candidate while `review_runs.candidate_count` remained zero.

## Implementation

1. Schema version 2 adds nullable `memory_items.candidate_target_id` with an
   indexed same-table foreign key using `ON DELETE SET NULL`. The v1-to-v2
   migration preserves existing rows, and model row mapping exposes the field.
2. The shared confidence threshold moved to the model module so validator and
   store use one definition.
3. Validator confidence demotion now runs before duplicate normalization.
   Uncertain revisions and contradictions become `mark_candidate` proposals tied
   to their canonical target; uncertain duplicate adds target the matching fact.
4. Duplicate identity is target-aware. Existing facts and isolated proposals no
   longer deduplicate into each other. Repeated content proposals with the same
   candidate target reinforce only the candidate row.
5. Store transactions defensively mirror demotion for direct calls, canonicalize
   short/full candidate-target aliases in the exact scope, validate target
   metadata, and count normalized candidates in `review_runs`.
6. Candidate inserts never enter FTS. Deleting a target scrubs and removes that
   target normally while the foreign key detaches the still-inspectable candidate.
   Subject clear still deletes all subject rows, including candidates.
7. `candidate_target_id` is not part of curator JSON and raw validator proposals
   carrying it are rejected; only deterministic normalization may set it.

## GREEN Evidence

- New regression selection: `17 passed in 0.20s`.
- Review accounting and exact-scope selection: `4 passed in 0.07s`.
- Required focused suite:
  `.venv/bin/pytest -q tests/test_memory_curation.py tests/test_long_term_memory.py tests/test_config.py`
  completed with `190 passed in 0.89s`.
- Full suite outside the restricted network namespace:
  `.venv/bin/pytest -q` completed with
  `821 passed, 12 skipped in 20.47s`.
- `git diff --check` completed with no errors.

## Coverage Added

- v1 migration and repeated restart initialization;
- validator self-duplicate revisions for active, candidate, and dormant targets;
- distinct-duplicate revisions and contradictions;
- low-confidence duplicate adds;
- direct-store low-confidence add, revise, and contradict defenses;
- canonical full/short aliases and subsequent staged siblings;
- unchanged existing rows, confidence, source counts, revisions, and FTS entries;
- repeated proposal reinforcement after restart;
- exact-scope candidate-target rejection and transactional source rollback;
- hard-delete detachment without cross-scope effects;
- normalized candidate review-run accounting.

## Concerns

- The restricted network namespace again stalled in the unrelated late resolver
  safety tests, matching the r4 report. That run was interrupted with exit 130;
  the complete suite passed outside the restricted namespace.
- Candidate confirmation/resolution is intentionally deferred to the later command
  task. This change persists enough explicit target state for that workflow without
  adding confirmation behavior to Task 2.
- No unresolved in-scope concern remains.
