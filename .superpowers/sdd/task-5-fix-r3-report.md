# Task 5 Review R3 Fix Report

## Scope

Fixed all findings in `task-5-review-r3.md` on top of `b0e7ec7`. Changes remain
inside Task 5 and are limited to the shared memory validator/store and directly
related tests. Task 6 was not started.

## TDD Evidence

The first R3 regression run, before production changes, reproduced the credential,
candidate-sensitivity, and formatted-identifier findings:

```text
40 failed, 316 passed in 3.43s
```

An adjacent leading-underscore case was then added before generalizing the prefix
grammar; `_PASSWORD=...` failed in both command and validator paths (`2 failed,
2 passed`) before the final expression change.

## Fixes

- Replaced fixture-oriented environment suffix matching with a general forbidden
  credential grammar. It supports optional leading underscores and arbitrary
  underscore-delimited prefixes for API/OAuth/session/client/access/refresh tokens,
  keys and secrets, passwords, passwd, cookies, private keys, bearer/auth values,
  and recovery/backup codes.
- Reused the full `is|are|equals|=|:` assignment grammar for prefixed and unprefixed
  credential labels. Deterministic remember and correct both route through this
  validator and now reject every tested variant.
- Applied maximum sensitivity while converting low-confidence revise/contradict
  operations into candidates and again when inserting any candidate with a target.
  Target sensitivity may escalate from normal to sensitive but can never downgrade.
- Verified validated and direct-store low-confidence contradiction paths in both
  sensitivity directions. Confirming a sensitive candidate keeps it sensitive and
  ordinary retrieval continues to exclude it.
- Added bounded candidate extraction for formatted mainland mobile numbers, mainland
  identity-card-shaped values, and 16-19 digit financial identifiers. Only matched
  candidate spans have spaces/hyphens removed; arbitrary surrounding text is not
  globally punctuation-stripped.
- Added support for `+86`, spaces, and hyphens and verified add, revise, later benign
  correction, and retrieval exclusion.

## Changed Files

- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_curation.py`
- `tests/test_memory_retrieval.py`
- `tests/test_long_term_memory.py`

## Verification

- Affected memory suite: `360 passed in 1.98s`
- Review-focused seven-file suite: `524 passed in 6.08s`
- Full suite: `1079 passed, 13 skipped in 28.63s`
- `python -m compileall -q src/qq_agent_bridge tests`
- `git diff --check`

## Residual Risks

- Credential and identifier detection intentionally prioritizes confidentiality.
  Generic secret-like environment suffixes and structurally plausible formatted
  identifiers can produce false positives, but those values are rejected or hidden
  instead of being exposed to later Agent prompts.
- Identity/card recognition validates bounded structure and date shape, not official
  checksums or issuer ownership. It is a sensitivity classifier, not an identity
  verifier.
- Task 6 coordinator behavior remains untouched.
