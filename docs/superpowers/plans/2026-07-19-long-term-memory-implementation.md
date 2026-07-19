# Scoped Long-Term Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build opt-in, strictly scoped long-term memory with SQLite persistence, periodic Agent curation, unified retrieval, and authorized `/memory` management.

**Architecture:** A typed SQLite store is the source of truth and exposes scope-mandatory APIs. A lightweight collector stages eligible user text, a deterministic validator commits constrained curator proposals, and one low-priority coordinator runs reviews and decay. Ask, task, schedule, and proactive prompts consume one bounded retriever; a command service handles deterministic and constrained natural-language management.

**Tech Stack:** Python 3.13, asyncio, stdlib sqlite3/FTS5, PyYAML, pytest, existing Agent adapter and OneBot bridge.

## Global Constraints

- Every group and private scope is disabled by default and must be explicitly enabled.
- Group, private, and cross-group records are strictly isolated by `(scope_kind, scope_id)` in every API and SQL query.
- Group-level memories may use ordinary group messages; personal memories require self-statement, direct interaction, explicit subject request, or non-sensitive owner confirmation.
- Sensitive personal memories require explicit subject consent; secrets are never stored.
- Temporary review text is deleted atomically after successful review and unconditionally after 604800 seconds.
- SQLite is the source of truth; v1 uses structured filters and FTS5, not embeddings.
- Ask, task, code, schedule, and proactive paths use the same retriever.
- Retrieval defaults to at most 12 active items and 1500 formatted characters.
- Bot output, profile text, system prompts, runtime skills, candidates, dormant items, rejected items, expired items, and contradicted losers never enter normal retrieval.
- User forget and clear operations hard-delete content and FTS data.
- `/reset` clears only short-term and ambient memory and must say long-term memory is unaffected.
- Curator output is a proposal; deterministic validation is the security authority.

---

### Task 1: Configuration, Models, and Scoped SQLite Store

**Files:**
- Create: `src/qq_agent_bridge/long_term_memory.py`
- Create: `tests/test_long_term_memory.py`
- Modify: `src/qq_agent_bridge/config.py`
- Modify: `tests/test_config.py`
- Modify: `config.example.yaml`

**Interfaces:**
- Produces: `LongTermMemoryConfig`, `MemoryReviewConfig`, `MemoryRetrievalConfig`, `MemoryDecayConfig` on `BridgeConfig.long_term_memory`.
- Produces: `MemoryScope`, `MemorySource`, `MemoryItem`, `MemoryProposal`, `LongTermMemoryStore`.
- Store methods used later: `initialize()`, `is_scope_enabled(scope)`, `set_scope_enabled(scope, enabled)`, `collect(source)`, `pending_sources(scope, limit)`, `commit_review(scope, source_ids, operations)`, `list_items(...)`, `get_item(...)`, `retrieve_candidates(...)`, `hard_delete(...)`, `clear_subject(...)`, `status(scope)`, `expire_raw(now)`, `apply_decay(now)`, and `close()`.

- [ ] **Step 1: Write failing configuration and store tests**

Add tests that load an absent block and assert `default_scope_enabled is False`, then load bounded custom values. Add SQLite tests that initialize under `tmp_path`, assert mode `0600`, enable one group, collect rows, and prove that private/group/cross-group queries cannot see each other.

```python
def test_long_term_memory_scopes_are_disabled_by_default() -> None:
    cfg = BridgeConfig()
    assert cfg.long_term_memory.enabled is True
    assert cfg.long_term_memory.default_scope_enabled is False

def test_store_requires_exact_scope_for_every_query(tmp_path: Path) -> None:
    store = LongTermMemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    group_a = MemoryScope("group", "a")
    group_b = MemoryScope("group", "b")
    store.set_scope_enabled(group_a, True)
    assert store.is_scope_enabled(group_a)
    assert not store.is_scope_enabled(group_b)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_config.py -q`

Expected: import/attribute failures for the new configuration and store types.

- [ ] **Step 3: Implement bounded configuration and schema**

Add nested dataclasses with the exact defaults from the design specification. Parse malformed sections safely, clamp positive thresholds, normalize group/user scope maps to string keys, and add `long_term_memory` to `BridgeConfig.load`.

Implement SQLite initialization with WAL, foreign keys, `busy_timeout=5000`, private parent/database modes, migrations, `review_buffer`, `memory_items`, `memory_revisions`, `review_runs`, and an FTS5 table synchronized transactionally by store methods. All public methods accept a `MemoryScope`; no user-ID-only lookup exists.

- [ ] **Step 4: Implement store behavior**

Use explicit transactions for review commit, hard deletion, expiry, and decay. `commit_review` must apply accepted operations and delete only its consumed source IDs in the same transaction. Keep retry metadata for failed rows and delete all source rows older than the configured TTL.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_long_term_memory.py tests/test_config.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/long_term_memory.py src/qq_agent_bridge/config.py tests/test_long_term_memory.py tests/test_config.py config.example.yaml
git commit -m "feat: add scoped long-term memory store"
```

### Task 2: Collection Eligibility and Deterministic Validation

**Files:**
- Create: `src/qq_agent_bridge/memory_curation.py`
- Create: `tests/test_memory_curation.py`
- Modify: `src/qq_agent_bridge/long_term_memory.py`

**Interfaces:**
- Consumes: Task 1 store/model APIs.
- Produces: `MemoryCollector.collect_event(ev, command_name=None, explicit=False) -> bool`.
- Produces: `MemoryValidator.validate(scope, sources, proposals, actor) -> ValidationResult`.
- Produces: JSON parser `parse_curator_output(text) -> tuple[MemoryProposal, ...]`.

- [ ] **Step 1: Write failing eligibility tests**

Cover private ordinary text, group culture collection, self-statement provenance, structured real mentions/quotes, bot sender rejection, disabled scope rejection, command/nonce/secret rejection, bounded text, and third-party personal claim rejection.

```python
def test_textual_mention_cannot_become_personal_memory(enabled_group_store, make_event) -> None:
    ev = make_event("@123 他住在北京", sender="456", group="g")
    collector = MemoryCollector(enabled_group_store, cfg)
    assert collector.collect_event(ev)
    proposals = (MemoryProposal.add(subject_kind="user", subject_id="123", content="住在北京"),)
    result = MemoryValidator(cfg).validate(MemoryScope("group", "g"), sources(), proposals, actor=None)
    assert result.accepted == ()
    assert result.rejected[0].reason == "third_party_personal_claim"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_memory_curation.py -q`

Expected: missing collector, parser, and validator.

- [ ] **Step 3: Implement collector**

Normalize bounded user text and preserve structured sender, quoted sender, real mention IDs, reply/direct-interaction flags, command class, source reason, and timestamp. Reject bot output, disabled scopes, attachment payloads, internal directives, dangerous command bodies, approval nonces, and secret-like text before insert.

- [ ] **Step 4: Implement strict proposal parsing and validation**

Parse only the documented operation/category/status/sensitivity fields. Enforce exact scope, valid subject provenance, sensitivity consent, forbidden secret patterns, maximum operation count and content length, valid state transitions, and actor permissions. Convert low-confidence valid operations to candidates; duplicates reinforce; contradictions create revisions rather than silent overwrite.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_memory_curation.py tests/test_long_term_memory.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/memory_curation.py src/qq_agent_bridge/long_term_memory.py tests/test_memory_curation.py
git commit -m "feat: validate long-term memory curation"
```

### Task 3: Curator and Low-Priority Review Coordinator

**Files:**
- Create: `src/qq_agent_bridge/memory_review.py`
- Create: `tests/test_memory_review.py`
- Modify: `src/qq_agent_bridge/storage_gate.py`

**Interfaces:**
- Consumes: `LongTermMemoryStore`, `MemoryValidator`, configured Agent adapter, `StorageActivityGate`.
- Produces: `MemoryCurator.review(scope, sources, existing) -> CuratorOutcome`.
- Produces: `MemoryReviewCoordinator.start()`, `stop()`, `notify(scope)`, `review_now(scope, actor)`, `reload(cfg)`, `cancel_background_for_interactive()`.

- [ ] **Step 1: Write failing curator/coordinator tests**

Use a fake Agent to assert the curator uses mode `ask`, model `auto`, no progress callback, bounded JSON-only prompt, and no source text in logs. Test threshold+idle, periodic minimum, explicit review, one-at-a-time behavior, cancellation before commit, backoff, max-attempt cool-down, TTL deletion, and daily decay.

```python
async def test_failed_review_keeps_sources_and_backs_off(store, fake_agent) -> None:
    fake_agent.results = ["not json"]
    coordinator = make_coordinator(store, fake_agent)
    outcome = await coordinator.review_now(GROUP, actor=OWNER)
    assert outcome.error == "malformed_output"
    assert store.status(GROUP).pending_count == 1
    assert store.pending_sources(GROUP, limit=10, now=outcome.next_attempt_at - 1) == ()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_memory_review.py -q`

Expected: missing review classes and cancellation hook.

- [ ] **Step 3: Implement curator**

Build a prompt that labels source and existing memory as untrusted data and requests the exact JSON schema. Invoke the gated Agent in ask mode with the configured model and timeout. Do not expose tools, runtime skill, outgoing resources, progress, QQ sending, or writable task paths. Parse and validate before committing.

- [ ] **Step 4: Implement coordinator**

Track dirty scopes and idle deadlines without blocking OneBot handling. Run at most one background review; interactive work cancels only uncommitted model review. Mechanical failures retain rows with bounded exponential backoff; after `max_attempts`, defer to the next periodic cycle. Start daily expiry/decay and clean shutdown.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_memory_review.py tests/test_memory_curation.py tests/test_storage_gate.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/memory_review.py src/qq_agent_bridge/storage_gate.py tests/test_memory_review.py
git commit -m "feat: review and consolidate long-term memory"
```

### Task 4: Unified Retrieval and Prompt Injection

**Files:**
- Modify: `src/qq_agent_bridge/long_term_memory.py`
- Modify: `src/qq_agent_bridge/prompting.py`
- Modify: `src/qq_agent_bridge/proactive.py`
- Modify: `tests/test_prompting.py`
- Modify: `tests/test_proactive.py`
- Create: `tests/test_memory_retrieval.py`

**Interfaces:**
- Produces: `LongTermMemoryRetriever.retrieve(scope, current_sender, real_mentions, quoted_sender, query) -> str`.
- Extends: `build_agent_prompt(..., long_term_memory="")`.
- Extends: `ProactiveSpeaker(..., long_term_context=callable)`.

- [ ] **Step 1: Write failing retrieval tests**

Prove exact scope isolation, actual-sender/real-mention/quote subject selection, textual `@` rejection, active-only filtering, 12-item/1500-character bounds, FTS ranking, no sensitive disclosure, and consistent ask/task/schedule/proactive formatting.

```python
def test_textual_qq_number_does_not_authorize_subject_retrieval(retriever) -> None:
    text = retriever.retrieve(GROUP, "u1", (), None, "@u2 最近怎么样")
    assert "u2-private-in-group" not in text
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_memory_retrieval.py tests/test_prompting.py tests/test_proactive.py -q`

Expected: missing retriever/context arguments.

- [ ] **Step 3: Implement bounded retriever**

Filter exact scope, authorized subjects, active state, sensitivity, and expiry before ranking by FTS/BM25, effective score, reinforcement, and freshness. Format as labeled untrusted background and include the four prompt-contract rules verbatim.

- [ ] **Step 4: Inject through shared prompt paths**

Add one `long_term_memory` section after explicit profile and before short-term history. Feed the same retriever to normal Agent jobs and proactive prompts. Scheduled jobs use the scope and sender captured on their synthetic `ChatEvent`; code uses the same retrieval as task.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_memory_retrieval.py tests/test_prompting.py tests/test_proactive.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/qq_agent_bridge/long_term_memory.py src/qq_agent_bridge/prompting.py src/qq_agent_bridge/proactive.py tests/test_memory_retrieval.py tests/test_prompting.py tests/test_proactive.py
git commit -m "feat: inject unified long-term memory context"
```

### Task 5: `/memory` Commands and Constrained Natural Language

**Files:**
- Create: `src/qq_agent_bridge/memory_commands.py`
- Create: `tests/test_memory_commands.py`
- Modify: `src/qq_agent_bridge/policy.py`
- Modify: `src/qq_agent_bridge/command_help.py`
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `tests/test_app_async.py`

**Interfaces:**
- Produces: `MemoryCommandService.handle(ev, args) -> MemoryCommandResult` where the result contains immediate text and optional background review request.
- Supports deterministic commands and constrained intents `status|enable|disable|remember|list|show|correct|confirm|forget|clear|review|clarify`.

- [ ] **Step 1: Write failing deterministic command tests**

Cover default status, enable/disable persistence, group-owner and private-user rules, member self-management, owner group/member clear without browse access, candidates, confirmation, page indexes/short IDs, hard deletion, help, and `/reset` wording.

- [ ] **Step 2: Write failing natural-language tests**

Use a fake Agent JSON response and prove immediate acknowledgement, one interpretation pass, caller-visible summaries only, maximum five mutations, post-model permission revalidation, clarification for ambiguous destructive references, and no changes on malformed output.

```python
async def test_natural_language_forget_never_guesses_ambiguous_item(service, fake_agent) -> None:
    fake_agent.result = '{"intent":"forget","references":["考研"]}'
    result = await service.handle(EV, "忘掉考研那件事")
    assert result.text.startswith("需要你确认")
    assert store.list_items(PRIVATE, subject_id="u1") == original_items
```

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run pytest tests/test_memory_commands.py tests/test_app_async.py -q`

Expected: missing command and service.

- [ ] **Step 4: Implement deterministic service and permissions**

Add `memory` to policy/help with default `user` semantics. Route commands before Agent jobs. Make scope switches durable in SQLite. Require confirmation tokens for clear operations. Enforce current-scope ownership rules in the service even after the command-level gate.

- [ ] **Step 5: Implement constrained natural-language interpretation**

Try deterministic syntax first. Otherwise acknowledge immediately, invoke one ask-mode `auto` interpretation with only accessible summaries, parse the fixed schema, resolve references deterministically, reauthorize every operation, cap mutations at five, and clarify rather than guess destructive targets.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_memory_commands.py tests/test_app_async.py tests/test_command_help.py tests/test_policy.py -q`

Expected: all focused tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/qq_agent_bridge/memory_commands.py src/qq_agent_bridge/policy.py src/qq_agent_bridge/command_help.py src/qq_agent_bridge/main.py tests/test_memory_commands.py tests/test_app_async.py
git commit -m "feat: manage scoped memory from QQ"
```

### Task 6: App Lifecycle, Collection Integration, Reload, and Documentation

**Files:**
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `src/qq_agent_bridge/storage_maintenance.py`
- Modify: `src/qq_agent_bridge/self_knowledge.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `config.example.yaml`
- Create: `tests/test_long_term_memory_app.py`
- Modify: `tests/test_deployment_docs.py`

**Interfaces:**
- Consumes all prior tasks.
- App startup initializes the store/coordinator before accepting events; shutdown stops review and closes SQLite.
- Reload updates hot-reloadable thresholds/scope maps but reports `database_path` changes as restart-required.

- [ ] **Step 1: Write failing app lifecycle and end-to-end tests**

Cover disabled-scope no-op collection, enabled group/private collection, nonblocking event handling, review trigger, shared ask/task/proactive retrieval, restart persistence, reload, database failure isolation, storage activity gating, clean shutdown, and `/reset` preserving long-term data.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_long_term_memory_app.py tests/test_app_async.py -q`

Expected: app does not yet own/start/stop the subsystem.

- [ ] **Step 3: Integrate lifecycle and collection**

Initialize the database before OneBot handling, but degrade only the long-term subsystem on open/migration failure. Collect eligible events after structured parsing without waiting for review. Notify the coordinator, cancel background review when interactive Agent work begins, and stop/close in the existing best-effort shutdown sequence.

- [ ] **Step 4: Implement reload and operator documentation**

Hot-reload enablement maps, review, retrieval, and decay settings. Keep the opened path until restart and report this clearly. Document plaintext-database protection, backup implications, opt-in behavior, commands, examples, TTL, strict scope isolation, and configuration defaults in both READMEs and `config.example.yaml`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_long_term_memory_app.py tests/test_deployment_docs.py tests/test_app_async.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Run full verification**

Run: `uv run pytest -q`

Run: `uv run python -m tests`

Run: `git diff --check`

Expected: all tests pass and no whitespace errors.

- [ ] **Step 7: Adversarial review**

Review cross-scope SQL, textual mention spoofing, third-party claims, secret storage, sensitive activation, hard-delete completeness, cancellation atomicity, malformed curator output, command authorization, profile residue, and database failure behavior. Fix every Critical/Important finding and rerun its covering tests plus the full suite.

- [ ] **Step 8: Commit**

```bash
git add src/qq_agent_bridge/main.py src/qq_agent_bridge/storage_maintenance.py src/qq_agent_bridge/self_knowledge.py README.md README.zh-CN.md config.example.yaml tests/test_long_term_memory_app.py tests/test_deployment_docs.py
git commit -m "docs: finish long-term memory integration"
```
