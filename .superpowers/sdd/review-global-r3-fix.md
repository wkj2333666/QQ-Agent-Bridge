# Scoped Long-Term Memory Global Review R3 Fix

## Resolution

- Automatic curation no longer has any hard-delete authority. The curator prompt does
  not advertise `forget`, validation rejects actorless or curator-provenance forgets,
  and `commit_review()` independently refuses forget operations unless they have
  deterministic user-command provenance. `/memory forget`, `/memory clear`, and
  store-owned expiry maintenance remain the only hard-delete paths.
- Evidence binding now conservatively rejects occurrence support inside examples,
  hypotheticals, conditionals, quotations, negations, and output/opt-out instructions.
  This includes English contractions and both compact and spaced Chinese example or
  hypothetical markers. Model confidence cannot override this deterministic gate.
- Secret checks use one security normal form: Unicode NFKC, removal of every `Cf`
  format character, whitespace normalization, and case folding. Multilingual labels
  and assignment forms cover Chinese changed-to wording, Spanish, Japanese, French,
  German, Portuguese, Russian, Korean, and common secret environment names. Secret
  material is rejected at both collection and proposal validation regardless of
  consent or claimed sensitivity.
- Legal-name labels such as `姓名：` and precise home-address labels such as
  `家庭地址：` now tolerate punctuation and token spacing. They are classified as
  sensitive and background activation still requires explicit consent from the
  subject.
- English, Chinese, and design documentation now state that automatic review uses
  validated revise, contradict, or merge semantics and never hard-deletes records.

## Deterministic Coverage

- Exact unrelated same-subject/category `u1 works in finance` / `u1 lives in Paris`
  delete probe, claimed elapsed-expiry authority, review-actor bypass, forged curator
  provenance at commit, atomic source retention, and deterministic user-forget control.
- Exact `For example`, `It isn't true`, and `例如：` probes plus curly contractions,
  `For instance`, conditionals, hypotheticals, and spaced Chinese marker mutations;
  direct affirmative controls remain accepted.
- Exact Chinese changed-to, Spanish, Japanese, and zero-width `API key` probes plus
  multiple `Cf`, full-width, separator, multilingual, and environment-name mutations
  at ingress and validation; unassigned credential discussions remain allowed.
- Exact `我的姓名：张三` and `家庭地址：北京市海淀区中关村大街27号` probes plus direct,
  spaced, and full-width-separator label variants with background-consent rejection.

## Verification

- Exact R3 and adjacent-mutation replay: `63 passed in 0.36s`.
- Focused memory curation/store/review suite: `479 passed in 1.78s`.
- Full suite: `1460 passed, 13 skipped in 18.14s`.
- `python -m tests`, `python -m compileall -q src tests`, and `git diff --check`
  completed successfully.
