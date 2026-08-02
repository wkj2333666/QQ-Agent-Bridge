# Long-Term Memory Curation Usability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make automatic memory review converge, preserve evidence safety, and produce useful low-trust recurring-topic observations without presenting them as user facts.

**Architecture:** Extend the existing SQLite-backed memory store with an `observed` trust tier and terminal review-batch semantics. Introduce a versioned curator response with per-source evidence spans, retain the legacy proposal parser only for compatibility, and keep deterministic scope, subject, sensitivity, and transition validation as the authority. Retrieval renders active facts and weak observations separately.

**Tech Stack:** Python 3.13, dataclasses, asyncio, SQLite/FTS5, pytest, uv.

## Global Constraints

- Use test-driven development and observe every new test fail before production edits.
- Use only isolated temporary SQLite databases in tests; never open or mutate the production database.
- Preserve scope isolation and treat curator output and QQ content as untrusted input.
- Do not log source text, memory content, QQ identifiers, or evidence spans.
- Tests and documentation use synthetic identities and conversations only.
- Do not use subagents; execute this plan inline as previously requested.
- Existing explicit `/memory` operations and `agent_work` memory behavior must remain compatible.

---

### Task 1: Schema Version 4 and Trust-Tier Domain Types

**Files:**
- Modify: `src/qq_agent_bridge/long_term_memory_schema.py`
- Modify: `src/qq_agent_bridge/long_term_memory_models.py`
- Modify: `src/qq_agent_bridge/config.py`
- Test: `tests/test_long_term_memory.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `MemoryEvidence(source_id: int, evidence_span: str)`.
- Produces: `CuratorResponse(schema_version, operations, deferred_source_ids)`.
- Produces: `MemoryStatusName` and `ALLOWED_STATUSES` including `observed`.
- Produces: review/decay configuration for observation and candidate retention.

- [ ] **Step 1: Write failing migration and model tests**

Add tests proving that schema version 3 migrates to 4, `observed` can be stored,
review accounting columns exist, and legacy inferred recurring-topic candidates
become rejected with a migration revision. Add model tests for evidence-span
normalization and config tests for positive retention values.

```python
def test_schema_v3_migrates_observed_status_and_archives_legacy_topic(tmp_path):
    conn = _open_v3_database(tmp_path)
    _insert_legacy_recurring_candidate(conn, content="示例主题")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute(
        "SELECT status FROM memory_items WHERE content = ?", ("示例主题",)
    ).fetchone()[0] == "rejected"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_config.py -q`

Expected: failures because schema version 4, `observed`, and new configuration
fields do not exist.

- [ ] **Step 3: Implement the migration and types**

Rebuild `memory_items` transactionally because SQLite cannot alter a CHECK
constraint in place. Add review-run counters with safe defaults:

```sql
ALTER TABLE review_runs ADD COLUMN input_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_runs ADD COLUMN consumed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_runs ADD COLUMN deferred_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_runs ADD COLUMN observed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_runs ADD COLUMN ignored_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_runs ADD COLUMN no_memory_count INTEGER NOT NULL DEFAULT 0;
```

Add config defaults:

```python
observed_score_multiplier: float = 0.65
observed_retention_seconds: int = 2_592_000
candidate_retention_seconds: int = 2_592_000
```

Validate the multiplier in `[0, 1]` and retention values as positive integers.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_config.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/long_term_memory_schema.py src/qq_agent_bridge/long_term_memory_models.py src/qq_agent_bridge/config.py tests/test_long_term_memory.py tests/test_config.py
git commit -m "feat: add observed memory trust tier"
```

### Task 2: Versioned Curator Response and Evidence Validation

**Files:**
- Modify: `src/qq_agent_bridge/memory_curation.py`
- Modify: `src/qq_agent_bridge/memory_review.py`
- Test: `tests/test_memory_curation.py`
- Test: `tests/test_memory_review.py`

**Interfaces:**
- Consumes: `MemoryEvidence`, `CuratorResponse`, and `observed` from Task 1.
- Produces: `parse_curator_response(text: str) -> CuratorResponse`.
- Produces: normalized store operations `add`, `observe`, `support_observation`,
  `revise`, `reinforce`, `contradict`, and `mark_candidate`.

- [ ] **Step 1: Write parser RED tests**

Cover a valid empty v2 response, per-operation allowed fields, duplicate JSON
keys, unknown fields, unknown schema versions, invalid deferred IDs, and exact
evidence spans.

```python
def test_parse_v2_observe_topic_keeps_per_source_evidence():
    parsed = parse_curator_response(json.dumps({
        "schema_version": 2,
        "deferred_source_ids": [],
        "operations": [{
            "operation": "observe_topic",
            "subject_kind": "user",
            "subject_id": "1000000001",
            "topic": "数据库索引",
            "confidence": 0.82,
            "evidence": [
                {"source_id": 1, "evidence_span": "索引怎么优化"},
                {"source_id": 2, "evidence_span": "索引选择"},
            ],
        }],
    }))
    assert parsed.operations[0].operation == "observe"
```

- [ ] **Step 2: Run parser tests and verify RED**

Run: `uv run pytest tests/test_memory_curation.py -q`

- [ ] **Step 3: Implement parser and operation-specific shapes**

Keep `parse_curator_output()` as the legacy compatibility entry point. New
curator calls use `parse_curator_response()`. Reject fields not listed for each
operation. Convert external v2 names to stable internal operations so explicit
command code need not change.

- [ ] **Step 4: Write validator RED tests**

Prove that varied wording can support one normalized observation when every
evidence span is exact, while one source, one-character topics, sensitive
topics, third-party personal claims, cross-subject evidence, and missing spans
are rejected. Prove `support_observation` retains `observed` status.

- [ ] **Step 5: Run validator tests and verify RED**

Run: `uv run pytest tests/test_memory_curation.py -q`

- [ ] **Step 6: Implement deterministic validation**

Validate each evidence span against its own source. Use semantic normalization
only for `recurring_topic` observations. Keep direct-assertion requirements for
active facts. Remove the old exact-common-substring candidate gate from curator
proposals while preserving explicit user candidate flows.

- [ ] **Step 7: Replace the curator prompt and verify GREEN**

The prompt emits only schema version 2, states that empty operations are normal,
requires inspection of the whole batch, and uses `deferred_source_ids` only for
sources it could not inspect.

Run: `uv run pytest tests/test_memory_curation.py tests/test_memory_review.py -q`

- [ ] **Step 8: Commit**

```bash
git add src/qq_agent_bridge/memory_curation.py src/qq_agent_bridge/memory_review.py tests/test_memory_curation.py tests/test_memory_review.py
git commit -m "feat: validate versioned memory curation evidence"
```

### Task 3: Terminal Batch Semantics and Bounded Failure Retry

**Files:**
- Modify: `src/qq_agent_bridge/long_term_memory.py`
- Modify: `src/qq_agent_bridge/memory_review.py`
- Test: `tests/test_long_term_memory.py`
- Test: `tests/test_memory_review.py`

**Interfaces:**
- Produces: `commit_review(..., input_source_ids, deferred_source_ids, ...)`.
- Produces: `quarantine_review_failures(..., max_attempts)`.
- Produces: consistent review counters on success and failure.

- [ ] **Step 1: Write lifecycle RED tests**

Test that a valid empty response consumes all inspected sources, semantic
rejections consume their batch, explicit deferred IDs remain pending, malformed
output retries, and the final failed attempt changes `review_state` to
`quarantined`.

```python
async def test_empty_review_is_success_and_consumes_sources(coordinator, store):
    outcome = await coordinator.review_now(GROUP, OWNER)
    assert outcome.error is None
    assert outcome.no_memory_count == 1
    assert store.pending_sources(GROUP, 10) == ()
```

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run: `uv run pytest tests/test_memory_review.py tests/test_long_term_memory.py -q`

- [ ] **Step 3: Implement atomic source disposition**

On a valid v2 response, delete all inspected source IDs except explicitly
deferred IDs. Record input, consumed, deferred, accepted, candidate, observed,
rejected, and no-memory counts in one transaction.

- [ ] **Step 4: Implement bounded failure quarantine**

Failure marking increments attempts and assigns `quarantined` when the new
attempt count reaches `max_attempts`. Periodic and threshold queries select only
pending rows. Explicit review may select pending plus quarantined rows and reset
selected quarantined rows only for that attempt.

- [ ] **Step 5: Run lifecycle tests and verify GREEN**

Run: `uv run pytest tests/test_memory_review.py tests/test_long_term_memory.py -q`

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/long_term_memory.py src/qq_agent_bridge/memory_review.py tests/test_long_term_memory.py tests/test_memory_review.py
git commit -m "fix: make memory review batches converge"
```

### Task 4: Observation Storage, Deduplication, Decay, and Retrieval

**Files:**
- Modify: `src/qq_agent_bridge/long_term_memory.py`
- Modify: `src/qq_agent_bridge/long_term_memory_models.py`
- Modify: `tests/test_long_term_memory.py`

**Interfaces:**
- Consumes: internal `observe` and `support_observation` operations.
- Produces: trust-aware `retrieve_candidates()` results containing active and
  observed items.
- Produces: maintenance expiry for observed and candidate items.

- [ ] **Step 1: Write storage and retrieval RED tests**

Test observation insertion, normalized duplicate support, source-count and
timestamp refresh, no automatic promotion, lower ranked retrieval, candidate
exclusion, and observed/candidate expiry with revision records.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_long_term_memory.py -q`

- [ ] **Step 3: Implement observation operations**

Store observations as `category='recurring_topic', status='observed'`. A
duplicate `observe` becomes `support_observation`. Supporting an observation
updates score only within the observation ceiling and never changes status to
active.

- [ ] **Step 4: Implement trust-aware retrieval**

Select active and observed rows, calculate ranking with
`observed_score_multiplier`, and preserve FTS synchronization. Do not include
candidate rows.

- [ ] **Step 5: Implement retention maintenance**

Extend `apply_decay()` to reject expired observed/candidate rows with revision
entries. Keep active/dormant behavior compatible.

- [ ] **Step 6: Run focused and adjacent tests and verify GREEN**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_agent_memory.py -q`

- [ ] **Step 7: Commit**

```bash
git add src/qq_agent_bridge/long_term_memory.py src/qq_agent_bridge/long_term_memory_models.py tests/test_long_term_memory.py tests/test_agent_memory.py
git commit -m "feat: retrieve and decay observed memory topics"
```

### Task 5: Trust-Aware Prompt Rendering and Review Feedback

**Files:**
- Modify: `src/qq_agent_bridge/long_term_memory.py`
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `src/qq_agent_bridge/memory_commands.py`
- Test: `tests/test_long_term_memory.py`
- Test: `tests/test_long_term_memory_app.py`
- Test: `tests/test_memory_commands.py`

**Interfaces:**
- Consumes: `CuratorOutcome` accounting fields and observed retrieval items.
- Produces: separate active-memory and weak-observation prompt sections.
- Produces: truthful `/memory`, `/memory review now`, and candidate-management
  summaries.

- [ ] **Step 1: Write rendering and command RED tests**

Assert that observations are explicitly labelled as weak context, are never
rendered under personal facts, and obey `max_items`/`max_chars`. Assert review
summaries distinguish inspected, observed, candidate, ignored, deferred, and
failed counts. Assert candidates remain confirmable and observed items are not
listed as confirmation candidates.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_long_term_memory_app.py tests/test_memory_commands.py -q`

- [ ] **Step 3: Implement rendering and summaries**

Render active and observed sections independently. Use fixed rules that tell the
answering agent observations are continuity hints only. Update command counts
without exposing content or identifiers in logs.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_long_term_memory_app.py tests/test_memory_commands.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/long_term_memory.py src/qq_agent_bridge/main.py src/qq_agent_bridge/memory_commands.py tests/test_long_term_memory.py tests/test_long_term_memory_app.py tests/test_memory_commands.py
git commit -m "feat: expose memory review trust and outcomes"
```

### Task 6: Isolated End-to-End and Real-Agent Capability Coverage

**Files:**
- Modify: `tests/test_memory_e2e.py`
- Modify: `tests/test_memory_review.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Consumes: the complete review, storage, retrieval, and command pipeline.
- Produces: opt-in real-Agent verification that never opens production state.

- [ ] **Step 1: Write the isolated end-to-end RED test**

Drive synthetic messages through collection, `/memory review now`, commit,
retrieval, and final user feedback. Include direct self-statement, repeated
varied topic messages, and unrelated chat. Assert the source buffer is empty,
one active fact and one observed topic exist, and the final prompt separates
their trust levels.

- [ ] **Step 2: Run the isolated test and verify RED**

Run: `uv run pytest tests/test_memory_e2e.py -q`

- [ ] **Step 3: Complete the pipeline wiring and verify GREEN**

Make only integration changes exposed by the test. Do not bypass the parser,
validator, store transaction, or application delivery layer.

- [ ] **Step 4: Update opt-in real-Agent test and documentation**

The real-Agent test reads configuration from environment, uses a temporary
workspace and SQLite path, and asserts schema-level outcomes rather than exact
model wording. Document observed memory semantics, finite retries, and the
isolated test command.

- [ ] **Step 5: Run complete verification**

Run:

```bash
uv run pytest tests/test_memory_curation.py tests/test_memory_review.py tests/test_long_term_memory.py tests/test_memory_commands.py tests/test_long_term_memory_app.py tests/test_memory_e2e.py tests/test_agent_memory.py -q
uv run pytest -q
```

When agent credentials and quota are available, run the opt-in capability test
with the configured task model. Never point it at the production database.

- [ ] **Step 6: Privacy and migration dry-run audit**

Inspect every staged fixture and document for deployment-derived identifiers,
messages, paths, and secrets. Run schema migration against a copied synthetic v3
database twice to prove idempotence. Do not migrate production automatically.

- [ ] **Step 7: Commit**

```bash
git add tests/test_memory_e2e.py tests/test_memory_review.py README.md README.zh-CN.md
git commit -m "test: verify usable memory curation end to end"
```

### Task 7: Completion Audit

**Files:**
- Review: `docs/superpowers/specs/2026-08-03-memory-curation-usability-design.md`
- Review: all files changed by Tasks 1 through 6

**Interfaces:**
- Produces: requirement-by-requirement verification evidence.

- [ ] **Step 1: Map every design success criterion to a test**

Record the exact test name and command proving each criterion. Add a missing
test before claiming completion.

- [ ] **Step 2: Run formatting, privacy, and full regression checks**

Run `git diff --check`, repository privacy checks, and `uv run pytest -q`.

- [ ] **Step 3: Inspect the final diff and commit history**

Confirm only scoped files changed, no production data was opened for writing,
and no private data appears in tracked files or commit messages.

- [ ] **Step 4: Report deployment steps without applying them**

Document the production database backup, service stop, migration/start, and
post-start health checks. Production migration and service restart require an
explicit deployment action after code verification.
