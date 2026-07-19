# Task 3 Fix R2 Report

## Status

PASS. The remaining Task 3 P0 is fixed within `storage_gate`, `cursor_adapter`,
and focused tests.

## Fix

- The restricted curator builder creates a new private `curator-home-*` and a
  separate private `curator-workspace-*` under
  `~/.local/state/qq-agent-bridge` for every construction. Neither path reuses
  the normal Agent home, configured sandbox home, or project workspace.
- Hardened Cursor preparation resets the curator home before every launch and
  does not copy normal `.cursor/cli-config.json`,
  `.cursor/agent-cli-state.json`, workspace trust, project state, sessions,
  MCP configuration, or plugin state.
- Hardened preparation writes deterministic clean policy/state: no allowed
  permissions, explicit shell/write denies, empty MCP servers, empty plugins,
  disabled auto-review, and empty tool approvals.
- Only `~/.config/cursor/auth.json` is imported. The source path rejects
  symlinks and non-owned/non-regular artifacts, reads through a no-follow file
  descriptor with identity and size checks, and writes the private copy with
  mode `0600`. Missing or unsafe auth fails closed through the existing generic
  metadata-only sandbox error.
- Bubblewrap exposes the hardened home as `/home/curator` and the read-only
  curator workspace as `/workspace`. It does not expose the normal sandbox
  home or real-home destinations inside the hardened namespace.
- The default non-hardened adapter keeps its existing state copy and workspace
  trust behavior.

## TDD Evidence

The new builder-isolation, hostile-state, repeated-cleanup, synthetic-mount,
and missing-auth tests were run before implementation and failed for the
expected reasons: the builder reused `agent-home`, hardened mode copied hostile
state, no empty MCP policy existed, real paths were namespace destinations, and
missing auth did not fail closed. The same focused cases passed after each
minimal implementation step.

## Verification

- Focused Task 3/storage/Cursor/config/trace and prior memory suites:
  `336 passed in 1.69s`.
- Full runnable suite with the documented managed-sandbox exception:
  `866 passed, 12 skipped, 1 deselected in 14.14s`.
- The deselected test was
  `tests/test_resources.py::test_default_http_fetch_rejects_loopback_targets`.
  Run alone under the managed no-network Codex sandbox, it reached the test and
  timed out after 20 seconds (`exit 124`). The first otherwise-full run was
  interrupted after the same test stopped progress at 81 percent.
- `git diff --check`: clean.

## Commit

`fix: isolate curator runtime state`

## Concerns

No code concern remains in the requested scope. The loopback HTTP test remains
an environment-only verification limitation; it was not modified or classified
as passing.
