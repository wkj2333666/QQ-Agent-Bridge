# Task 2 Third Re-review Fix Report

## Status

Fixed all Task 2 third re-review findings on `codex/long-term-memory` with
test-first regressions. Changes are limited to curation, store behavior, curation
tests, and this report.

## Files

- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_curation.py`
- `.superpowers/sdd/task-2-fix-r3-report.md`

## RED Evidence

### Shared secret assignment grammar

- Command:
  `.venv/bin/python -m pytest tests/test_memory_curation.py -q -k 'collector_rejects_ineligible_or_secret_material or validator_rejects_shared_secret_assignment_variants'`
- Initial result before production changes: `14 failed, 30 passed, 66 deselected`.
- Failures showed collector and validator acceptance of English recovery/backup
  code prose using `are`, `is`, and `equals`, plus Chinese recovery/backup code
  forms using `是`, `为`, `等于`, and full-width punctuation. All failing values
  were one character long.

### Cross-sensitivity content mutation

- Command:
  `.venv/bin/python -m pytest tests/test_memory_curation.py -q -k 'owner_confirmed_content_change or explicit_candidate_matching or staged_content_change or content_mutation_defensively'`
- Initial result before production changes: `6 failed, 1 passed, 110 deselected`.
- Failures covered owner-confirmed normal revise/contradict proposals matching a
  known sensitive fact, explicit candidate creation, staged revise and
  contradiction replacement content, and defensive store revision. The existing
  contradiction store path passed because replacement insertion already rolled
  back on collision.

### Full/short ID alias sequencing

- Command:
  `.venv/bin/python -m pytest tests/test_memory_curation.py -q -k 'forget_then_revise or merge_then_revise or contradict_then_revise'`
- Initial result before production changes: `6 failed, 114 deselected`.
- Both full-to-short and short-to-full variants failed across forget/revise,
  merge/revise, and contradict/revise. The failures demonstrated stale staged
  reads and accepted operations retaining short aliases.

## GREEN Evidence

- Secret grammar regressions: `44 passed, 66 deselected`.
- Sensitivity mutation regressions: `7 passed, 110 deselected`.
- Alias sequencing regressions: `6 passed, 114 deselected`.
- Required focused suite:
  `.venv/bin/python -m pytest tests/test_memory_curation.py tests/test_long_term_memory.py tests/test_config.py -q`
  completed with `165 passed in 0.78s`.
- Full suite outside the restricted network namespace:
  `.venv/bin/python -m pytest -q`
  completed with `796 passed, 12 skipped in 13.73s`.
- `git diff --check` completed with no errors before the report was added.

## Finding Review

1. English and Chinese credential labels now use one assignment grammar per
   language. Recovery and backup code labels share the same verbal and punctuation
   handling as the other credential labels, and detection begins at the first
   non-whitespace value character. Collector and validator continue to use the
   same `_contains_secret` boundary.
2. Validator collision policy covers every operation that creates or changes
   content: add, explicit candidate, revise, and contradict. Accepted candidate
   and contradiction replacement identities are staged, while staged revisions
   are represented in the canonical item overlay. Store insertion and revision
   share one defensive duplicate/collision helper; contradiction replacement
   continues through insertion inside the same transaction.
3. Every stateful proposal resolves its scoped primary ID and merge-related IDs
   through the store, then replaces aliases with full IDs before staged lookup or
   update. Merge survivor, removed targets, terminal targets, and accepted
   proposals therefore use one canonical key space. Invalid later operations are
   rejected individually and earlier accepted siblings commit.

## Concerns

- The restricted network namespace again stalled in the unrelated loopback HTTP
  guard near the end of the full suite. That run was interrupted cleanly, then the
  complete suite passed outside the restricted namespace. No product network
  endpoint was contacted by the guard test.
- No unresolved in-scope findings remain.
