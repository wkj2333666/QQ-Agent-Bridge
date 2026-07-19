# Task 5 Review R6 Fix Report

## Scope

Fixed the bounded P0 finding in `task-5-review-r6.md` on branch
`codex/long-term-memory`. Changes are limited to the shared Task 5 secret policy and
collector/validator/command regressions. Task 6 was not started.

## TDD Evidence

The R6 tests were written before production changes and reproduced the missing generic
credential family across deterministic remember/correct, raw collection, validator, and
prefixed environment forms:

```text
48 failed, 447 passed in 4.33s
```

The first implementation run left two deliberately non-secret discussion fixtures
failing because the sentence `credentials are discussed` itself matched the approved
`are` assignment grammar. The fixtures were corrected to contain no assignment token;
no semantic exception was added. The affected suite then passed:

```text
495 passed in 2.41s
```

## Fixes

- Added bounded singular/plural `credential` and `credentials` labels to the shared
  English natural-language policy.
- Covered the requested auth, authentication, login, and service-qualified labels with
  the existing space/hyphen/underscore separator grammar.
- Added environment suffixes `CREDENTIAL` and `CREDENTIALS`; the existing bounded
  prefix grammar covers service, database, vendor, auth, login, and leading-underscore
  forms without enumerating fixture-specific prefixes.
- Added Chinese `凭据`, `凭证`, `登录凭据`, `认证信息`, `身份凭据`, and corresponding
  login/auth/identity credential variants.
- Retained the existing assignment requirement (`is`, `are`, `equals`, `=`, `:`,
  Chinese `是`, `为`, `等于`, and full-width colon). Ordinary discussion mentioning
  credential labels without assignment remains eligible for collection and explicit
  memory.
- Probed adjacent account/sign-in credential labels and prefixed environment variants;
  they are handled by the generic family rather than dedicated exceptions.

## Changed Files

- `src/qq_agent_bridge/memory_curation.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_curation.py`
- `.superpowers/sdd/task-5-fix-r6-report.md`

## Verification

- Affected command/curation suite: `495 passed in 2.41s`
- Review-focused seven-file suite: `732 passed in 7.17s`
- Full suite: `1287 passed, 13 skipped in 15.94s`
- `python -m compileall -q src/qq_agent_bridge tests`
- `git diff --check`

## Residual Risks

- Syntactic assignment detection intentionally treats phrases such as
  `credentials are discussed` as assigned content. Avoiding that false positive would
  require semantic interpretation and is outside this bounded shared policy.
- Generic assigned `凭证` may reject a harmless voucher-like value. This conservative
  behavior follows the review's explicit always-excluded credential requirement.
- Task 6 coordinator behavior remains untouched.
