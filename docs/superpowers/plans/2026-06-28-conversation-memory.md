# Conversation Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-memory rolling conversation window per allowed private user or group chat.

**Architecture:** Create a focused `ConversationMemory` module that owns message storage, trimming, formatting, and reset. `App` injects formatted history into prompts before invoking Cursor and appends completed `/ask` or `/plan` exchanges after replies are produced.

**Tech Stack:** Python 3.13, asyncio, pytest, existing QQ bridge modules.

---

### Task 1: Memory Module

**Files:**
- Create: `src/qq_agent_bridge/memory.py`
- Test: `tests/test_memory.py`

- [ ] **Step 1: Write failing tests**

Test private/group keying, rolling `max_messages`, `max_chars`, and reset.

- [ ] **Step 2: Run focused tests**

Run: `python -m pytest tests/test_memory.py -q`

- [ ] **Step 3: Implement minimal memory module**

Add `ConversationMemory` with `key_for`, `append_exchange`, `format_history`, and `reset`.

- [ ] **Step 4: Run focused tests again**

Run: `python -m pytest tests/test_memory.py -q`

### Task 2: Prompt And App Integration

**Files:**
- Modify: `src/qq_agent_bridge/prompting.py`
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `src/qq_agent_bridge/policy.py`
- Modify: `src/qq_agent_bridge/types.py`
- Test: `tests/test_prompting.py`
- Test: `tests/test_app_async.py`
- Test: `tests/test_policy.py`

- [ ] **Step 1: Write failing tests**

Test prompt history injection, `/reset` authorization/parsing, response history append, and reset clearing current conversation.

- [ ] **Step 2: Run focused tests**

Run: `python -m pytest tests/test_prompting.py tests/test_app_async.py tests/test_policy.py -q`

- [ ] **Step 3: Implement integration**

Add `reset` command, create memory in `App`, pass history into `build_agent_prompt`, append ask/plan exchanges on completion, and clear memory on `/reset`.

- [ ] **Step 4: Run focused tests again**

Run: `python -m pytest tests/test_prompting.py tests/test_app_async.py tests/test_policy.py -q`

### Task 3: Config And Verification

**Files:**
- Modify: `src/qq_agent_bridge/config.py`
- Modify: `config.example.yaml`
- Modify: `config.test.yaml`
- Modify: `README.md`

- [ ] **Step 1: Add memory config**

Add `memory.enabled`, `memory.max_messages`, and `memory.max_chars`.

- [ ] **Step 2: Run full verification**

Run:
- `python -m pytest -q`
- `python dry_run.py`
- `python -m compileall -q src tests dry_run.py`
