# Task 5 Review R4 Fix Report

## Scope

Fixed all findings in `task-5-review-r4.md` on top of `36767af`. Changes remain
inside Task 5 and are limited to the shared collector/validator policy and directly
related tests. Task 6 was not started.

## TDD Evidence

The initial R4 regressions were added before production changes and reproduced the
credential-label and phone-format gaps:

```text
55 failed, 325 passed in 10.40s
```

One exploratory negative assertion was removed because its malformed country prefix
still contained a complete valid local phone span, which should remain sensitive.
Adjacent leading-underscore phrase probes were then added; `_SEED_PHRASE` and
`__MNEMONIC_PHRASE` produced `4 failed` before the environment suffix grammar was
generalized.

## Fixes

- Extended the shared English natural-language secret grammar with private key,
  private-key, recovery/backup/seed phrase/key/code families, mnemonic, and mnemonic
  phrase. The existing `is|are|equals|=|:` assignment grammar applies consistently.
- Extended the Chinese grammar with `私钥`, `助记词`, `助记短语`, and recovery,
  backup, or seed phrase/key/code forms, using the existing Chinese assignment
  grammar.
- Kept environment-style and natural-language forms aligned, including leading
  underscore variants such as `_SEED_PHRASE` and `__MNEMONIC_PHRASE`.
- Because raw collection and validator-backed remember/correct share
  `_contains_secret`, all three ingress paths now reject the same forbidden material.
- Expanded the bounded phone candidate grammar for optional `+86`, a balanced
  parenthesized three-digit mobile prefix, dots, spaces, tabs, and hyphens.
- Formatting is removed only from the matched candidate span. The compact value must
  still match exactly one mainland mobile number before sensitivity is escalated.
- Added remember, correct, later benign correction, and ordinary retrieval exclusion
  tests for parenthesized, dotted, local-parenthesized, and mixed phone layouts.

## Changed Files

- `src/qq_agent_bridge/memory_curation.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_curation.py`
- `tests/test_memory_retrieval.py`

## Verification

- Affected memory suite: `423 passed in 2.27s`
- Review-focused seven-file suite: `587 passed in 6.50s`
- Full suite: `1142 passed, 13 skipped in 15.49s`
- `python -m compileall -q src/qq_agent_bridge tests`
- `git diff --check`

## Residual Risks

- Secret-label matching intentionally favors confidentiality and can reject benign
  prose that explicitly assigns a value to a recovery, seed, or mnemonic label.
- Phone recognition accepts only a bounded set of balanced common layouts. Arbitrary
  punctuation is not stripped globally, which limits false positives but deliberately
  leaves uncommon display conventions unsupported.
- Task 6 coordinator behavior remains untouched.
