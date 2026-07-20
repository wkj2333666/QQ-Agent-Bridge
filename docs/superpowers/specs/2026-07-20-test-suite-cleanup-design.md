# Test Suite Cleanup + Capability Tests + CI

## Problem

1526 tests, ~30k lines, 0 CI. Two real bugs shipped that should have been caught:
- `/reboot` not in COMMANDS set (no command registration integrity check)
- Memory curator `malformed_output` (no end-to-end test of curator with model-like output)

~150-200 tests are waste (10-13% of tests, 16-23% of lines).

## Design

### Phase 1: Remove/consolidate waste

Nine waste patterns to address, ordered by impact:

1. **Copy-paste boilerplate** (`test_app_async.py`) — merge 40-60 identical-structure tests into parametrized forms. 72/127 tests share the same `adapter + cfg + _handle` template.

2. **Redundant parametrize cases** (`test_memory_curation.py`) — cut 15-20 cases that test the same code path redundantly (55+ secret-detection cases, 31 Chinese/English evidence variations).

3. **Error message string assertions** (~135 places) — test error categories/types, not exact strings. Use shared constants.

4. **Fixture/data bloat** — `test_visual_media_skill.py` (testing doc content, not code), `test_agent_capability_eval.py` (always skipped), `test_video_media_fixtures.py` (validation infra, not tests).

5. **Near-identical normalize tests** (`test_onebot.py`) — collapse 13 tests into 2-3 parametrized.

6. **Repetitive negative-recovery tests** (`test_outgoing_resources.py`) — parametrize 20 same-structure tests.

7. **Low-assertion tests** (~135 tests with 0-1 asserts) — delete or upgrade.

8. **Duplicate cross-file coverage** — de-duplicate proactive + memory tests.

### Phase 2: Add capability tests

- **Command registration integrity** — test that every command in `config.yaml` is in `policy.COMMANDS`
- **Memory curator end-to-end** — FakeAgent returns markdown-wrapped JSON, verify full pipeline: parse → validate → commit
- **Progress message accuracy** — parametrized test verifying keyword → message mapping

### Phase 3: GitHub Actions CI

Single-job workflow: `ubuntu-latest`, `uv run pytest`, on push + PR to main.

## Non-goals

- Rewriting tests that work
- Adding coverage for uncovered code
- Changing test framework or patterns
