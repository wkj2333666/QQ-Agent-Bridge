# Task 5 Review R5 Fix Report

## Scope

Fixed both findings in `task-5-review-r5.md` on branch
`codex/long-term-memory`. The change remains inside Task 5: one shared
collector/validator policy module and its command, curation, and retrieval tests.
Task 6 was not started.

## TDD Evidence

The R5 regressions were added before production changes. They reproduced both gaps
through deterministic commands, raw collection, validator-backed mutation, sensitivity
monotonicity, and ordinary retrieval:

```text
69 failed, 390 passed in 4.67s
```

After the shared grammar and bounded phone normalization were implemented, the affected
suite passed. Additional adjacent probes were then added for bare and prefixed
`SECRET_KEY`, nested key passphrases, horizontal/small dash forms, non-breaking spaces,
and negative slash/full-width-digit cases:

```text
474 passed in 2.54s
```

## Fixes

- Added explicit natural-language `secret key`/`secret-key` and environment-style
  `SECRET_KEY` families, including arbitrary underscore-delimited vendor prefixes.
- Added mnemonic, recovery, backup, and seed `word`/`words` families to both natural
  and environment grammars.
- Added `passphrase`, `pass phrase`, key/private/secret passphrase, and prefixed
  environment passphrase forms without inferring unlabelled high-entropy text.
- Retained the assignment requirement (`is`, `are`, `equals`, `=`, `:`, or full-width
  colon). Unassigned prose about secret keys, mnemonic words, or passphrase policy is
  still collectable.
- Defined one bounded phone-separator set covering ASCII and full-width plus,
  parentheses, hyphen, dot and spaces; en/em and related dash characters; full-width
  dot/hyphen; ideographic, non-breaking, and narrow non-breaking spaces.
- Candidate matching uses only ASCII phone digits, fixed digit counts, digit boundaries,
  and at most three approved separators between digits. Translation is applied only to
  the matched candidate, followed by exact mainland mobile validation.
- Confirmed Unicode-formatted mobile numbers become sensitive on remember/correct,
  cannot be downgraded by a later benign correction, and remain excluded from ordinary
  retrieval. Slash-separated and full-width-digit lookalikes are not normalized.

## Changed Files

- `src/qq_agent_bridge/memory_curation.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_curation.py`
- `tests/test_memory_retrieval.py`
- `.superpowers/sdd/task-5-fix-r5-report.md`

## Verification

- Affected memory suite: `474 passed in 2.54s`
- Review-focused seven-file suite: `676 passed in 6.65s`
- Full suite: `1231 passed, 13 skipped in 15.48s`
- `python -m compileall -q src/qq_agent_bridge tests`
- `git diff --check`

## Residual Risks

- Explicit assigned credential labels are rejected conservatively even when a message
  is discussing a harmless example; this is intentional for memory confidentiality.
- Phone normalization deliberately accepts only the enumerated punctuation family and
  ASCII digits. Other Unicode digits or arbitrary separators are not treated as phone
  numbers, avoiding broad text normalization and substring false positives.
- Task 6 coordinator behavior remains untouched.
