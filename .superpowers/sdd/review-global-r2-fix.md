# Scoped Long-Term Memory Global Review R2 Fix

## Resolution

- Curator `forget` operations now resolve every related ID and require either an
  actually elapsed stored expiry or same-subject/category replacement items whose
  content is affirmatively supported by the cited batch. Curator provenance keeps
  this proof mandatory during `/memory review now`; deterministic user deletion is
  unchanged.
- Evidence validation now distinguishes extractive occurrence from affirmative
  support. Quoted examples, negations, output instructions, forget requests, and
  do-not-store/do-not-remember contexts are rejected regardless of model confidence.
- Credential checks normalize text with Unicode NFKC and case folding before matching.
  They cover `就是`, full-width assignments and labels, access-key-ID labels, and
  `AWS_ACCESS_KEY_ID` environment forms at collection and validation.
- Legal-name and precise-address detection tolerates punctuation and token spacing,
  including `我家住` street-and-house-number forms. Background proposals are upgraded
  to sensitive and require explicit subject consent.
- Both proactive Agent paths carry raw retrieved-memory values alongside their final
  prompts and pass them as `redact_extra`. Redaction does not parse untrusted prompt
  text, and the decision and QQ output values remain unchanged.
- Curator JSON parsing rejects `NaN`, `Infinity`, and `-Infinity` with
  `parse_constant`. The coordinator treats them as malformed output, retains source
  rows, increments attempts, and schedules retry backoff.

## Deterministic Coverage

- Fabricated, cross-subject, unproved, and curator-supplied-expiry forget attempts;
  valid elapsed expiry and evidence-backed replacement controls; review-actor bypass.
- English and Chinese quoted, negated, opt-out, forget, example, and instruction
  evidence, including curly apostrophes and spaced Chinese tokens; affirmative controls.
- Full-width API keys, Chinese `就是`, AWS access-key-ID casing/full-width variants,
  and English/Chinese labels at both ingress and validation.
- Punctuated and spaced legal names plus compact, colon-separated, and spaced `我家住`
  precise addresses with background-consent rejection.
- Unmentioned proactive batch and direct-mention trace redaction with exact output
  preservation and a memory-content delimiter-injection variant.
- Parser and coordinator retry probes for all three non-standard numeric constants.

## Verification

- Focused regression suite: `958 passed in 9.78s`.
- Full suite with asyncio diagnostics: `1426 passed, 13 skipped in 19.38s`.
- `python -m tests`, `python -m compileall -q src tests`, and staged diff checks
  completed successfully.
