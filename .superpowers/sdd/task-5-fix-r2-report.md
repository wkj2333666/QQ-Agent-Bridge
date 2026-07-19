# Task 5 Review R2 Fix Report

## Scope

Fixed all findings in `task-5-review-r2.md` on top of `e374cac`. The work remains
inside Task 5 and changes only the command parser, shared memory validator/store,
and directly related tests. Task 6 was not started.

## TDD Evidence

The new regressions were added before production changes. The first affected-suite
run reproduced all four findings:

```text
30 failed, 282 passed in 2.73s
```

Failures covered duplicate JSON keys, prefixed environment credentials, standalone
identifiers, and lost sensitivity on contradiction replacement.

## Fixes

- Added a dedicated underscore-delimited environment-variable secret suffix rule.
  It rejects service/vendor-prefixed API keys, secret access keys, generic tokens,
  and OAuth/session/client/access/refresh token or key forms in both add and revise.
- Added bounded structural classification for mainland Chinese mobile numbers,
  mainland identity-card-shaped values, and 16-19 digit financial identifiers.
  Explicit subject consent stores these as `sensitive`; ordinary retrieval excludes
  them, and later benign revisions cannot downgrade them.
- Added maximum-sensitivity propagation for `contradict` replacements at the store
  boundary. A sensitive proposal replacing a normal item stays sensitive, while a
  benign replacement of a sensitive item inherits sensitive status. Direct store
  calls enforce the same monotonic behavior.
- Added an `object_pairs_hook` that rejects duplicate JSON keys before intent schema
  validation. Duplicate `intent`, `content`, `reference`, and `target` envelopes now
  fail closed without state changes.

## Changed Files

- `src/qq_agent_bridge/memory_commands.py`
- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_curation.py`
- `tests/test_memory_retrieval.py`
- `tests/test_long_term_memory.py`

## Verification

- Affected memory suite: `315 passed in 1.70s`
- Review-focused seven-file suite: `479 passed in 5.99s`
- Full suite: `1034 passed, 13 skipped in 14.77s`
- `python -m compileall -q src/qq_agent_bridge tests`
- `git diff --check`

## Residual Risks

- Secret and sensitive-identifier detection intentionally favors confidentiality.
  Generic prefixed `_TOKEN` variables and standalone 16-19 digit strings may create
  false positives, but those values are rejected or hidden rather than exposed.
- Structural identity and card patterns are conservative format checks, not checksum
  or issuer validation. This is suitable for sensitivity escalation, not identity
  verification.
- Task 6 coordinator behavior remains untouched.
