# QQ Bot Permissions And Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow more commands safely, make bare mentions/private text default to `/ask`, and let multiple QQ requests invoke agent jobs concurrently without blocking OneBot receive handling.

**Architecture:** Keep parsing and authorization in `Policy`, keep network event handling in `App`, and let `App` schedule reply delivery in background tasks. Use an `asyncio.Semaphore` in `Policy` to cap concurrent agent subprocess jobs while allowing excess jobs to queue.

**Tech Stack:** Python 3.13, asyncio, pytest, OneBot v11 reverse WebSocket, Cursor CLI adapter.

---

### Task 1: Parser And Permission Policy

**Files:**
- Modify: `src/qq_agent_bridge/types.py`
- Modify: `src/qq_agent_bridge/policy.py`
- Test: `tests/test_policy.py`

- [ ] **Step 1: Write failing tests**

Add tests asserting:
- `@bot hello` parses as `ask` with args `hello` when default ask is enabled.
- Plain private text parses as `ask` when default ask is enabled.
- `/ask hello` still parses as `ask`.
- Non-owner allowed users may use `ask`, `plan`, `status`, and `help`.
- Non-owner allowed users may not use `code`, `shell`, or `approve`.
- Owners may start `code` and receive a confirmation nonce when dangerous confirmation is enabled.

- [ ] **Step 2: Run the tests and verify failure**

Run: `python -m pytest tests/test_policy.py -q`

- [ ] **Step 3: Implement minimal parser and authorization changes**

Add `plan` to command types. Add an optional `default_command` parameter to `Policy.parse`; when set to `ask` and the stripped text does not start with `/`, return `ParsedCommand("ask", stripped_text, stripped_text)`. In `Policy.allow`, restrict owner-only commands to `code`, `shell`, and `approve`.

- [ ] **Step 4: Run policy tests**

Run: `python -m pytest tests/test_policy.py -q`

### Task 2: Nonblocking Reply Delivery And Queue Status

**Files:**
- Modify: `src/qq_agent_bridge/config.py`
- Modify: `src/qq_agent_bridge/policy.py`
- Modify: `src/qq_agent_bridge/main.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_app_async.py`

- [ ] **Step 1: Write failing tests**

Add tests asserting:
- Two jobs can be started quickly while the first runner is still blocked.
- With `max_concurrent_jobs=1`, status reports one `running` job and one `queued` job before the first completes.
- `App._handle` returns before the underlying job finishes, and the reply is sent later by a background task.

- [ ] **Step 2: Run the tests and verify failure**

Run: `python -m pytest tests/test_policy.py tests/test_app_async.py -q`

- [ ] **Step 3: Implement minimal concurrency changes**

Add `AgentConfig.max_concurrent_jobs = 2`. Have `Policy` create an `asyncio.Semaphore` and mark each job as `queued`, `running`, or `done`. In `App._handle`, schedule `_reply_when_done(job, ev)` with `asyncio.create_task` instead of awaiting the job task inline.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_policy.py tests/test_app_async.py -q`

### Task 3: Docs, Config, And Verification

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`
- Modify: `dry_run.py`

- [ ] **Step 1: Update examples**

Document `plan`, default ask behavior, owner-only dangerous commands, and `agent.max_concurrent_jobs`.

- [ ] **Step 2: Run full verification**

Run:
- `python -m pytest -q`
- `python dry_run.py`
- `python -m compileall -q src tests dry_run.py`
