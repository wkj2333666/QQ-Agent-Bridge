# Task 5 Review R1 Fix Report

## Scope

Fixed the Task 5 review findings at `50ccb7a` without starting Task 6. Changes are
limited to the `/memory` command implementation, the shared memory validator/store,
and directly related tests.

## Fixes

- Replaced eager dictionary dispatch for natural-language `status`, `enable`, and
  `disable` intents with single-branch dispatch. Only the selected handler now runs.
- Replaced the permissive interpreter envelope with intent-specific JSON schemas.
  Required fields and exact types are enforced, unknown or irrelevant fields are
  rejected, booleans are not accepted as integers, and malformed output makes no
  state change.
- Expanded deterministic credential rejection to API, OAuth, session, client,
  bearer, refresh, and access token/key/secret labels with space, hyphen, and
  underscore separators, including `Authorization: Bearer ...` forms.
- Added a shared conservative sensitivity classifier for health, precise location
  and contact data, legal identity, financial data, intimate relationships,
  political views, and religious beliefs in practical Chinese and English forms.
- Validator-controlled add/revise operations escalate classified content to
  `sensitive`. Explicit consent from the subject permits storage, while ordinary
  retrieval continues to exclude sensitive records.
- Sensitive records cannot be downgraded during validation or by direct store
  revision. Revision staging and persisted identity keys now preserve the effective
  sensitivity.
- Verified hard deletion removes credential fixtures from items and FTS and scrubs
  content-bearing revision fields.
- The help mapping contains one `review` entry at current HEAD; no duplicate remains.

## Changed Files

- `src/qq_agent_bridge/memory_commands.py`
- `src/qq_agent_bridge/memory_curation.py`
- `src/qq_agent_bridge/long_term_memory.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_curation.py`
- `tests/test_memory_retrieval.py`
- `tests/test_long_term_memory.py`

## Verification

- Focused memory suite:
  `281 passed in 1.68s`
- Required Task 5 command/routing/policy suite:
  `233 passed in 4.69s`
- Full suite:
  `1000 passed, 13 skipped in 15.10s`
- `python -m compileall -q src/qq_agent_bridge tests`
- `git diff --check`

## Residual Risks

- The sensitivity classifier is intentionally conservative and pattern-based. It may
  classify some benign phrases as sensitive; that fails toward restricted retrieval
  rather than exposing personal data.
- Credential detection cannot prove that every arbitrary opaque string is a secret,
  but common labeled and bearer forms are now covered and regression-tested.
- Task 6 coordinator lifecycle integration remains untouched.
