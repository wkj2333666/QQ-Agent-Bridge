# Group Command Permissions and Help Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent per-group command permission overrides and detailed help subcommands for every bridge command without changing existing global permission compatibility or schedule help behavior.

**Architecture:** Keep the existing global `BridgeConfig.commands` map as the default layer and add a validated `command_groups` map loaded from `commands.groups`. `BridgeConfig.command_access(name, group_id)` becomes the single effective-permission resolver used by policy, mode checks, self-knowledge, and the new permission command. A focused help module owns static command metadata and renders `/help <command>` and `/<command> help`; `main.py` intercepts help before job creation.

**Tech Stack:** Python 3.13, dataclasses, PyYAML, existing atomic YAML block writer, pytest, asyncio.

## Global Constraints

- Preserve legacy boolean command configuration: `true` keeps historical command defaults and `false` means `disabled`.
- Group overrides only affect group chats; private chats always use global permissions.
- Only an owner may set or clear group permission overrides; authorized users may view them when `/permission` is enabled.
- Help requests must not create Agent jobs or consume Agent concurrency.
- Invalid permission values fail closed during config loading.
- Existing `/schedule help` output and existing `/help` role filtering must remain compatible.
- Run focused tests after each task and full `pytest`, `compileall`, and `git diff --check` before completion.

---

### Task 1: Add validated group permission configuration

**Files:**
- Modify: `src/qq_agent_bridge/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Add `BridgeConfig.command_groups: dict[str, dict[str, CommandAccess]]`.
- Extend `BridgeConfig.command_access(name: str, group_id: str | None = None) -> CommandAccess`.
- Extend `BridgeConfig.is_command_allowed(name: str, group_id: str | None = None) -> bool`.
- Add `_load_command_groups(raw: Any) -> dict[str, dict[str, CommandAccess]]`.

- [ ] **Step 1: Write failing configuration tests**

Add tests covering a YAML block such as:

```yaml
commands:
  ask: user
  task: user
  groups:
    "180188783":
      task: disabled
      search: owner
```

Assert that global values remain in `cfg.commands`, the nested map is loaded into `cfg.command_groups`, group lookup overrides only the matching command, missing commands fall back to global values, private lookup with the same id does not use the group map, and invalid group values raise `ValueError`.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run:

```bash
./.venv/bin/pytest -q tests/test_config.py -k "group_command or command_access"
```

Expected: failures because `command_groups` and the group-aware resolver do not exist yet.

- [ ] **Step 3: Implement the minimal config model**

Add the field and loader. `_load_commands` must ignore the reserved `groups` key while still validating every ordinary command value. `_load_command_groups` must require a mapping of group ids to command mappings, normalize ids and command names to strings/lowercase, accept only `COMMAND_ACCESS_LEVELS`, and raise `ValueError` for malformed structures or invalid values.

Resolve group values before the global value, but only when `group_id` is supplied. Keep the existing legacy boolean conversion in one helper so global and group values cannot diverge.

- [ ] **Step 4: Run the focused tests and existing config tests**

Run:

```bash
./.venv/bin/pytest -q tests/test_config.py
```

Expected: all config tests pass.

- [ ] **Step 5: Commit the configuration layer**

```bash
git add src/qq_agent_bridge/config.py tests/test_config.py
git commit -m "feat: add group command permission config"
```

### Task 2: Persist group permission overrides safely

**Files:**
- Create: `src/qq_agent_bridge/command_access_store.py`
- Test: `tests/test_command_access_store.py`

**Interfaces:**
- Add `write_command_access_to_config(path: Path, commands: dict[str, bool | CommandAccess], groups: dict[str, dict[str, CommandAccess]]) -> None`.
- The writer must use `write_top_level_block(path, "commands", block)` and preserve unrelated top-level config content.

- [ ] **Step 1: Write failing persistence tests**

Test writing global values plus a nested `groups` block, reloading with `BridgeConfig.load`, preserving an unrelated `owners` block, writing an empty group map as `groups: {}`, and replacing an existing commands block without leaving duplicate top-level `commands:` keys.

- [ ] **Step 2: Run the focused tests and verify failure**

```bash
./.venv/bin/pytest -q tests/test_command_access_store.py
```

Expected: collection failure because the module does not exist.

- [ ] **Step 3: Implement the formatter and atomic persistence call**

Format booleans using their YAML-safe values, string permissions as `user`, `owner`, or `disabled`, quote group ids with the same escaping style as `mention_mode_store.py`, sort command and group keys for stable output, and emit `groups: {}` when there are no overrides. Do not serialize the reserved nested map as a command.

- [ ] **Step 4: Run persistence and config tests**

```bash
./.venv/bin/pytest -q tests/test_command_access_store.py tests/test_config.py
```

Expected: all pass, including reload of the generated YAML.

- [ ] **Step 5: Commit the persistence layer**

```bash
git add src/qq_agent_bridge/command_access_store.py tests/test_command_access_store.py
git commit -m "feat: persist group command permissions"
```

### Task 3: Route authorization through the effective group permission

**Files:**
- Modify: `src/qq_agent_bridge/policy.py`
- Modify: `src/qq_agent_bridge/types.py`
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `src/qq_agent_bridge/self_knowledge.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_app_async.py`

**Interfaces:**
- Add `permission` to `CommandName` and `policy.COMMANDS`.
- Add `permission` to the default read/user command configuration in `config.example.yaml` during Task 5.
- `Policy.allow()` passes `ev.chat_id` to the resolver for group events and no group id for private events.

- [ ] **Step 1: Write failing authorization tests**

Add tests asserting a group-level `task: disabled` blocks a group member with `cmd-disabled`, a group-level `ask: owner` blocks a non-owner with `owner-only` but permits the owner, an unoverridden command falls back to global, and a private user with the same id is unaffected. Add a test that `/mode set task` rejects a task disabled in the current group.

- [ ] **Step 2: Run the focused tests and verify failure**

```bash
./.venv/bin/pytest -q tests/test_policy.py tests/test_app_async.py -k "group_permission or mode"
```

Expected: new group-override assertions fail because policy and mode still use global permissions.

- [ ] **Step 3: Implement effective authorization routing**

Change `Policy.allow()` and the mode setter to pass the group id only for group events. Register `permission` in the parser types and command set. Update self-knowledge command visibility helpers to resolve permissions using the current event’s group id, so bot descriptions do not advertise commands disabled in that group.

- [ ] **Step 4: Run policy and app tests**

```bash
./.venv/bin/pytest -q tests/test_policy.py tests/test_app_async.py
```

Expected: all existing and new authorization tests pass.

- [ ] **Step 5: Commit authorization routing**

```bash
git add src/qq_agent_bridge/policy.py src/qq_agent_bridge/types.py src/qq_agent_bridge/main.py src/qq_agent_bridge/self_knowledge.py tests/test_policy.py tests/test_app_async.py
git commit -m "feat: apply group command permission overrides"
```

### Task 4: Add the `/permission` command

**Files:**
- Modify: `src/qq_agent_bridge/main.py`
- Test: `tests/test_app_async.py`

**Interfaces:**
- Add `_handle_permission_command(ev: ChatEvent, args: str) -> str`.
- Add `_parse_permission_args(args: str) -> tuple[str, str, str]` returning action, command, access.
- Add `_permission_view_reply(ev: ChatEvent) -> str`.

- [ ] **Step 1: Write failing app interaction tests**

Test `/permission` displays current/global/effective values; `/permission set task disabled` by a group owner updates `cfg.command_groups`, persists and reloads; `/permission clear task` restores global; `/permission clear` removes all group overrides; non-owner set/clear is denied; private set/clear returns the group-only message; malformed command/access returns a concise usage message; and persistence failure restores the previous in-memory map.

- [ ] **Step 2: Run the focused tests and verify failure**

```bash
./.venv/bin/pytest -q tests/test_app_async.py -k permission
```

Expected: failures because the command is not registered or handled.

- [ ] **Step 3: Implement command parsing, display, mutation, and rollback**

Handle `/permission` after the normal `Policy.allow()` check and before Agent job creation. Only `set` and `clear` require `cfg.is_owner(ev.sender_id)` and a group event. Validate command names against the registered command set and values against `COMMAND_ACCESS_LEVELS`; reject `groups`, unknown commands, and empty set values. Snapshot the previous group map, mutate, call the persistence writer, and restore the snapshot on `OSError`.

Render one compact line per command with global, group override (or `-`), effective value, and enough role context for a QQ user to understand why a command is unavailable.

- [ ] **Step 4: Run the app tests**

```bash
./.venv/bin/pytest -q tests/test_app_async.py tests/test_policy.py
```

Expected: all pass.

- [ ] **Step 5: Commit the permission command**

```bash
git add src/qq_agent_bridge/main.py tests/test_app_async.py
git commit -m "feat: add persistent permission command"
```

### Task 5: Add structured help for every command

**Files:**
- Create: `src/qq_agent_bridge/command_help.py`
- Modify: `src/qq_agent_bridge/main.py`
- Modify: `src/qq_agent_bridge/self_knowledge.py`
- Modify: `config.example.yaml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Test: `tests/test_command_help.py`
- Test: `tests/test_app_async.py`

**Interfaces:**
- Add `build_command_help(name: str, cfg: BridgeConfig, ev: ChatEvent) -> str`.
- Add `build_help_reply(cfg: BridgeConfig, ev: ChatEvent, topic: str = "") -> str` behavior for `/help <command>` while preserving the no-topic overview.

- [ ] **Step 1: Write failing help tests**

Test every registered command has non-empty help with `用法` and `权限`, `/help task` and `/task help` both describe task without invoking the runner, `/task 帮助` is equivalent, unknown `/help nope` gives a friendly list/unknown message, disabled commands show disabled status, and `/schedule help` retains its existing examples and timezone.

- [ ] **Step 2: Run the focused tests and verify failure**

```bash
./.venv/bin/pytest -q tests/test_command_help.py tests/test_app_async.py -k "help"
```

Expected: failures because command-specific help and topic routing do not exist.

- [ ] **Step 3: Implement the metadata table and renderer**

Define one entry per command with purpose, syntax, examples, and restrictions. Render effective permission from the current group. For `/help <command>`, resolve the topic and return detailed help; for `/<command> help|帮助`, intercept before `ask`, schedule dispatch, or job creation. Route `schedule` to the existing detailed schedule help content so its tested examples remain unchanged, and make `/help schedule` identify the same examples.

- [ ] **Step 4: Update self-knowledge, example config, and README**

Add `permission: user` and a commented `commands.groups` example to `config.example.yaml`. Mention `/permission`, `/help <command>`, and `/<command> help` in both READMEs. Ensure public self-knowledge lists `/permission` according to effective group permission.

- [ ] **Step 5: Run help and full app tests**

```bash
./.venv/bin/pytest -q tests/test_command_help.py tests/test_app_async.py tests/test_schedule_app.py
```

Expected: all pass and existing schedule help assertions remain green.

- [ ] **Step 6: Commit the help feature**

```bash
git add src/qq_agent_bridge/command_help.py src/qq_agent_bridge/main.py src/qq_agent_bridge/self_knowledge.py config.example.yaml README.md README.zh-CN.md tests/test_command_help.py tests/test_app_async.py
git commit -m "feat: add detailed command help"
```

### Task 6: Full verification and adversarial review

**Files:**
- Modify: only files required by review findings.

- [ ] **Step 1: Run complete verification**

```bash
./.venv/bin/pytest -q
python3 -m compileall -q src tests
git diff --check
```

Expected: all tests pass, compilation succeeds, and diff check is clean.

- [ ] **Step 2: Review security and UX boundaries**

Check that a non-owner cannot mutate group overrides through aliases, unknown command names, malformed YAML, or private chat; that a group override cannot affect private chats or bypass workspace/confirmation policy; that disabled command help never starts a job; and that persistence failure restores the exact previous state.

- [ ] **Step 3: Add regression tests for every review finding and rerun verification**

For each finding, add a focused test before the fix, verify it fails, implement the minimal fix, and rerun the focused and full suites.

- [ ] **Step 4: Commit final review fixes**

```bash
git add src tests config.example.yaml README.md README.zh-CN.md
git commit -m "test: harden group permissions and command help"
```
