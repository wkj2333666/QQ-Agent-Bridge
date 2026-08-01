# Final adversarial fix report

## Status

Implemented the validator, coordinator, and dead-code fixes. Commit `430350c`
uses the message `fix: preserve rejected memory evidence`; this report also
includes commit `5e6cfdd` and the final fail-closed provenance follow-up, which
supersedes that commit's local lexical classifier. The latest review follow-up
also restores the ordinary-chat question guard. A subsequent production check
identified and corrected an over-constraint in recurring-topic provenance.

## P1 A: question and task evidence

The validator now classifies command evidence at the source-aware evidence
boundary, before general affirmative support can create or downgrade a memory.

- `plan` and `task` command provenance is always non-affirmative, so it cannot
  create active or wrong-category memory. That same non-affirmative evidence
  may still use the narrow repeated-topic candidate exception.
- `ask` provenance is non-affirmative by default. It can count as affirmative
  only when the proposal is labeled `self_statement`, the proposed content is a
  direct assertion from the cited source, and that content matches a small,
  bounded first-person assertion grammar.
- The positive grammar admits anchored forms such as `我喜欢...`, `我知道...`,
  `我的...是...`, and the narrow suffix forms `...是我写的...` and
  `...是我常用的...`. Before applying that grammar, a bounded fail-closed
  guard rejects any occurrence of `不`, `没`, `未`, `是否`, `能否`, `可否`,
  `吗`, `么`, `呢`, `吧`, `嘛`, `?`, or `？`. This blocks negation,
  uncertainty, A-not-A forms, quoted punctuation, and suffix disguises even
  when the model labels them `self_statement`.
- The lexical question/request marker list and 120-character clause scanner were
  removed. Clean direct ask assertions remain covered by positive controls.
- Ordinary non-command sources now have a bounded whole-message guard for
  terminal question punctuation/particles, A-not-A forms, and high-confidence
  request prefixes. The exact old `command_class=None` fixture
  `星露谷怎么玩？` is again rejected as a one-source preference candidate;
  ordinary quoted-question content remains affirmative when the whole message
  itself is an assertion. Terminal normalization strips only bounded trailing
  `.。!！` punctuation before checking the exposed tail, so compound sequences
  such as `？！`, `?!`, `!?`, repeated mixes, and particles followed by
  punctuation remain non-affirmative without losing the question mark.
- Repeated non-affirmative evidence remains restricted to `recurring_topic`, at
  least two distinct exact-support citations, and normalized
  `mark_candidate`/`candidate` output under the existing provenance and safety
  checks. Repeated `ask`, `plan`, `task`, and ordinary non-affirmative sources
  are eligible when every existing source-count, exact-support, same-subject,
  sensitivity, and normalization constraint passes.

Red evidence:

- Initial focused run: `4 failed, 1 passed`; one-source question/task candidates
  were accepted and repeated questions missed the recurring-topic gate.
- Mixed occurrence regression: `1 failed`; a question followed by an
  affirmative occurrence was rejected too broadly.
- Trusted task provenance regression: `1 failed`; an exact task source could be
  mislabeled as a self-statement and activated.
- Quoted-literal `ask` regression: `1 failed, 1 passed`; `ask` mode incorrectly
  suppressed a direct affirmative statement containing a quoted question mark.
- Local-clause follow-up: `4 failed, 2 passed`; two unpunctuated `ask` probes
  were activated, repeated unpunctuated questions missed the recurring-topic
  gate, and a question in a later comma clause hid earlier affirmative support.
- Final provenance follow-up: `8 failed, 9 passed`; `QQ机器人在哪安装`,
  `星露谷好玩吗`, `帮忙做一份...`, first-person question forms, and a
  question-particle form activated or bypassed recurring-topic normalization,
  while both allowed suffix assertions were rejected.
- Final review follow-up: `21 failed, 13 passed`; forbidden tokens and suffix
  disguises activated ask proposals, and ordinary
  terminal/particle/A-not-A/request evidence bypassed the one-source restriction
  or missed recurring normalization. That follow-up also introduced the later
  corrected `plan`/`task` candidate exclusion.
- Compound-terminal follow-up: `5 failed, 7 passed`; question sequences ending
  in exclamation punctuation bypassed the ordinary guard, while direct `!?`,
  particle-plus-punctuation cases, and the exclamation-only control already
  behaved correctly.
- Production verification showed the over-constraint directly: the hardened
  curator produced six recurring-topic candidates, and the validator rejected
  five because their cited evidence used task provenance. No source content or
  identifiers are included here.
- Recurring command-evidence correction: `2 failed, 4 passed`; repeated `plan`
  and `task` topics were rejected while one-source and wrong-category controls
  already remained closed.

Green evidence:

- Final focused review set: `34 passed`.
- Final focused compound-terminal set: `12 passed`.
- Final focused recurring command-evidence set: `6 passed`.
- Full validator/store modules: `592 passed`.

## P1 B: accepted-only source consumption

`MemoryReviewCoordinator` now derives reviewed source IDs only from accepted
proposal citations. Fully rejected citations and rejected citations in mixed
batches remain pending with their existing attempt count; accepted proposals
are still committed atomically and consume their own citations.

Red evidence:

- Fully rejected third-party, mixed accepted/rejected, and rejected owner claim
  regressions: `3 failed`; all rejected citations were deleted.

Green evidence:

- Full review module: `45 passed`.
- Review plus memory E2E modules: `47 passed, 5 skipped`.
- Mechanical failure, cancellation, and retry/backoff tests remain in the review
  suite and passed.

## Owner explicit review coverage

Existing owner-explicit scheduling tests continue to call `actor=OWNER`. Their
successful-review fixture now creates owner-authored evidence and derives the
proposal subject from the cited source sender, making the proposal valid under
the existing owner authorization rules.

A dedicated regression documents why the old fixture was invalid: an explicit
owner review does not authorize a different user's curator-produced
`self_statement`; it is rejected with `actor_not_authorized`, creates no item,
and leaves its source pending. Explicit forget remains in the separate command
authorization path and its focused coverage passes.

## P3 cleanup

Removed dead `CursorAdapter._runtime_is_read_only_mount` code and four obsolete
test monkeypatches. Runtime ownership trust continues to depend on UID namespace
proof, overflow UID, path location, ownership, and permissions, not mount status.

## Verification

- Combined bounded memory and cursor suite: `906 passed, 5 skipped`.
- `tests/test_memory_curation.py tests/test_long_term_memory.py`: `592 passed`.
- `tests/test_memory_review.py tests/test_memory_e2e.py`: `47 passed, 5 skipped`.
- `tests/test_cursor_adapter.py`: `89 passed`.
- `tests/test_memory_commands.py`: `178 passed`.
- Python compile checks and `git diff --check` passed. Ruff was not installed in
  the available project virtual environment, so no Ruff result is claimed.

The repository-wide suite was intentionally not used as the completion gate;
the controller will run it. A started broad run was stopped at the user's
request and is not reported as verification.

## Concerns

The five real-agent/systemd E2E cases remain opt-in and were skipped in the
bounded local run. No implementation concern remains from the focused suites.
