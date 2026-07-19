# Scoped Long-Term Memory Global Review R1 Fix

## Resolution

- Curator JSON operations now carry `source_ids`. Validation requires cited rows from
  the exact review batch and normalized extractive support for proposed or target
  content. Hallucinated personal/group facts and unrelated stateful mutations are
  rejected mechanically.
- `owner_confirmed` now requires an item-specific supporting source authored by the
  reviewing group owner. `/memory review now` is only a review trigger and grants no
  blanket confirmation.
- Credential detection uses one mixed Chinese/English assignment grammar across
  English labels, Chinese labels, and environment-style names. Explicit requests do
  not bypass secret rejection.
- Legal-name statements, WeChat/contact handles, and precise street/house-number
  addresses classify as sensitive and require explicit consent from the subject.
- Restricted curator/interpreter adapters own their generated workspace/home paths and
  remove them on idempotent disposal, App shutdown, partial construction, and startup
  failure. Cleanup validates the private state root, generated prefix, type, and owner.
- CQ-string `at` codes become real mention segments. Synthetic schedule events carry
  persisted numeric mentions for long-term-memory retrieval.
- Normal Agent invocations add retrieved memory blocks and item content to trace/log
  redaction values while returning the original assistant result for output delivery.
- Curator JSON duplicate keys are rejected at every object level.
- Help and self-knowledge use the same effective command-access resolver as policy, so
  an omitted `commands.memory` advertises the documented `user` default while exact
  group overrides still win.

## Deterministic Coverage

- Unsupported vegetarian/group-report proposals versus `hello everyone`.
- Uncited and out-of-batch source IDs, plus unrelated stateful reinforcement.
- Explicit owner review with an unrelated third-party source and victim claim.
- Mixed credential assignments in both language directions and environment variants.
- Chinese legal names, WeChat/contact handles, and precise postal addresses.
- Adapter disposal, constructor failure, App shutdown, and App startup failure cleanup.
- CQ-string multi-mention segments and schedule retrieval mention authority.
- Memory-content trace redaction with unmodified returned output.
- Duplicate envelope and nested operation keys.
- Default `/memory` help visibility and group-level disable override.

## Verification

- Focused regression suite: `866 passed in 7.85s`.
- Full pytest suite: `1383 passed, 13 skipped in 17.75s`.
- `python -m tests`, `python -m compileall -q src tests`, and `git diff --check`
  completed successfully.

Exact-scope, structured mention/quote authority, sensitivity, secret rejection, and
prior long-term-memory tests remain in the suite.
