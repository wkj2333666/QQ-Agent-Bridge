# Long-Term Memory Recurring-Topic Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production memory reviews run inside the systemd user namespace and convert repeated QQ topic questions into safe candidate memories instead of leaving every source pending.

**Architecture:** Keep the Agent responsible for semantic topic proposals and keep deterministic Python validation responsible for provenance and activation safety. Add one narrowly bounded evidence path for repeated `recurring_topic` mentions that always downgrades question-derived proposals to candidates, then align the curator prompt and metadata-only logs with that contract.

**Tech Stack:** Python 3.13, asyncio, SQLite, pytest, Cursor CLI, bubblewrap, systemd user services.

## Global Constraints

- Question- or task-derived recurring topics can only become `candidate` memories.
- Direct affirmative self-statements retain the existing active-memory path.
- Every cited source must contain the normalized topic; unrelated sources remain pending.
- User-topic evidence must be authored by that user; group-topic evidence remains within the exact group scope.
- Secrets, sensitive third-party claims, symlinks, writable runtime parents, and arbitrary foreign owners remain rejected.
- Logs contain counts and rejection reason names only, never message text, QQ IDs, memory content, or prompts.

---

### Task 1: Systemd UID-Namespace Runtime Trust

**Files:**
- Modify: `src/qq_agent_bridge/cursor_adapter.py:342-390`
- Test: `tests/test_cursor_adapter.py:715-865`

**Interfaces:**
- Consumes: `CursorAdapter._uid_mapped_system_prefixes(runtime_root: Path) -> set[Path]`
- Produces: `CursorAdapter._runtime_has_single_user_uid_map() -> bool`, `CursorAdapter._runtime_overflow_uid() -> int`, and a narrowed mapped-prefix branch in `_validate_runtime_path`.

- [ ] **Step 1: Add the failing namespace regression test**

Create a runtime whose controlled system prefix reports owner `65534`, force the mount check to `False`, force a single-current-user UID map, and assert `_hardened_cursor_runtime()` accepts it. Retain a separate test where the UID-map proof is false and rejection is required.

```python
monkeypatch.setattr(adapter, "_runtime_has_single_user_uid_map", lambda: True)
monkeypatch.setattr(adapter, "_runtime_overflow_uid", lambda: 65534)
assert adapter._hardened_cursor_runtime(workspace) == (runtime, binary)
```

- [ ] **Step 2: Verify the new test fails for the production reason**

Run:

```bash
.venv/bin/pytest tests/test_cursor_adapter.py -q \
  -k 'unmapped_system_prefix_in_single_user_namespace'
```

Expected: `ValueError: hardened cursor runtime is not trusted` from `_validate_runtime_path`.

- [ ] **Step 3: Implement the minimal namespace proof**

Parse `/proc/self/uid_map` and require one mapping whose inside UID is `os.getuid()` and whose length is `1`. Read `/proc/sys/kernel/overflowuid`; allow that owner only on prefixes already returned by `_uid_mapped_system_prefixes`, with the existing non-writable-directory checks still applied.

```python
def _runtime_is_unmapped_system_owner(self, owner: int) -> bool:
    return self._runtime_has_single_user_uid_map() and owner == self._runtime_overflow_uid()
```

- [ ] **Step 4: Verify acceptance and rejection boundaries**

Run:

```bash
.venv/bin/pytest tests/test_cursor_adapter.py -q -k 'hardened_runtime'
```

Expected: all hardened-runtime tests pass, including foreign-owner, writable-parent, symlink, workspace-local, and temporary-runtime rejection cases.

- [ ] **Step 5: Reproduce in a transient systemd unit**

Run the production `_hardened_cursor_runtime()` call under `NoNewPrivileges=yes` and `PrivateTmp=yes`, using the configured absolute Cursor binary. Expected: exit code `0` and the resolved versioned Cursor runtime path.

### Task 2: Repeated Topic Candidate Validation

**Files:**
- Modify: `src/qq_agent_bridge/memory_curation.py:700-790`
- Test: `tests/test_memory_curation.py`

**Interfaces:**
- Consumes: `MemoryProposal`, cited `MemorySource` values, `_content_supported_by_source`, and `_validate_subject`.
- Produces: `_repeated_topic_candidate(proposal, cited_sources) -> MemoryProposal | None`, called before affirmative-evidence rejection.

- [ ] **Step 1: Add failing tests for the accepted candidate path**

Build two distinct sources authored by one user, both containing `星露谷`, and an `add` proposal with category `recurring_topic`, status `active`, and source kind `inferred`. Assert validation accepts exactly one proposal and normalizes it to `mark_candidate` / `candidate`.

```python
assert accepted.operation == "mark_candidate"
assert accepted.status == "candidate"
assert accepted.category == "recurring_topic"
```

- [ ] **Step 2: Add security boundary tests**

Assert rejection when there is one source, duplicate source IDs, one citation without the topic, user citations authored by another user, category `preference`, sensitive content, or a stateful operation. Assert a direct affirmative self-statement can still remain active through the existing path.

- [ ] **Step 3: Verify the candidate test fails and old safety tests pass**

Run:

```bash
.venv/bin/pytest tests/test_memory_curation.py -q \
  -k 'repeated_topic or question_evidence'
```

Expected: the new happy-path test fails with `source_evidence_disallowed`; rejection cases pass or remain rejected.

- [ ] **Step 4: Implement the narrow candidate normalization**

Before the general affirmative-evidence check, recognize only `add` or `mark_candidate` proposals in category `recurring_topic` with at least two distinct cited sources. Require exact normalized topic support in every citation. Normalize matching proposals to:

```python
replace(proposal, operation="mark_candidate", status="candidate")
```

Continue into `_validate_subject` after normalization so exact-scope and same-subject provenance rules remain authoritative. Do not bypass secret or sensitivity checks.

- [ ] **Step 5: Run the complete validator suite**

Run:

```bash
.venv/bin/pytest tests/test_memory_curation.py tests/test_long_term_memory.py -q
```

Expected: all tests pass.

### Task 3: Curator Contract, Rejection Observability, and End-to-End Proof

**Files:**
- Modify: `src/qq_agent_bridge/memory_review.py:120-170,780-860`
- Test: `tests/test_memory_review.py`
- Test: `tests/test_memory_e2e.py`

**Interfaces:**
- Consumes: `CuratorOutcome.rejected`, `RejectedProposal.reason`, and the Task 2 candidate validator path.
- Produces: prompt instructions aligned with candidate evidence and metadata-only `rejection_reasons` logging.

- [ ] **Step 1: Add a metadata-only rejection logging test**

Use a fake Agent proposal rejected for two known reasons. Capture the `qq_agent_bridge.memory_review` log and assert it contains reason counts but excludes source text, sender IDs, proposed content, and prompt data.

```python
assert "source_content_mismatch=1" in rendered
assert sensitive_source_text not in rendered
```

- [ ] **Step 2: Add a real-Agent recurring-topic capability test**

In `tests/test_memory_e2e.py`, use an isolated SQLite database and four QQ-style questions from the same sender that each contain `星露谷`. Run the real curator, commit accepted operations, and assert at least one stored `recurring_topic` item has status `candidate`, with no question-derived active item.

- [ ] **Step 3: Run the real test against the old contract**

Run:

```bash
QQ_AGENT_BRIDGE_AGENT_E2E=1 \
  .venv/bin/pytest tests/test_memory_e2e.py -x -v \
  -k 'real_recurring_topic_candidate'
```

Expected before implementation: empty proposals or all proposals rejected.

- [ ] **Step 4: Align the curator prompt**

Update `_CURATOR_INSTRUCTIONS` to say that every source must be inspected but irrelevant sources may be omitted from `source_ids`; question/task evidence for repeated topics must use `recurring_topic`, `mark_candidate`, status `candidate`, and only citations containing the exact topic. Explicitly forbid converting these questions into preferences or active memories.

- [ ] **Step 5: Log rejection reason counts**

Use `collections.Counter` over `outcome.rejected` and append a sorted reason summary to the existing metadata-only curator log. Emit only reason identifiers and integer counts.

- [ ] **Step 6: Run focused and full automated verification**

Run:

```bash
.venv/bin/pytest tests/test_memory_review.py tests/test_memory_e2e.py -q
.venv/bin/pytest -q
git diff --check
```

Expected: all non-environment tests pass, real-Agent tests remain opt-in, and no whitespace errors are reported.

- [ ] **Step 7: Run systemd real-Agent end-to-end verification**

Run the isolated real-App memory test in a transient systemd unit with `NoNewPrivileges=yes`, `PrivateTmp=yes`, `QQ_AGENT_BRIDGE_APP_E2E=1`, and `QQ_AGENT_BRIDGE_E2E_BINARY` set to the configured absolute Cursor binary. Expected: `/memory review now` produces a completion message and commits at least one memory.

- [ ] **Step 8: Verify production backlog behavior**

Restart `qq-bridge.service` only after confirming there are no active Agent child processes. Run one explicit review for the affected production scope. Expected: at least one candidate is committed, unrelated sources remain pending, no `助手沙箱未配置` appears, and logs contain rejection reason counts without chat text.

- [ ] **Step 9: Commit the implementation**

```bash
git add src/qq_agent_bridge/cursor_adapter.py \
  src/qq_agent_bridge/memory_curation.py \
  src/qq_agent_bridge/memory_review.py \
  tests/test_cursor_adapter.py \
  tests/test_memory_curation.py \
  tests/test_memory_review.py \
  tests/test_memory_e2e.py \
  docs/superpowers/plans/2026-08-01-memory-recurring-topic-evidence.md
git commit -m "fix: retain recurring topics as memory candidates"
```
