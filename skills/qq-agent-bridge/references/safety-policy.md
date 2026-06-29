# Safety Policy For QQ-Driven Agents

Use this reference before enabling QQ messages to invoke local coding agents, CLI agents, shell commands, file edits, network access, or long-running jobs.

## Core Rule

No OAuth is acceptable for a local prototype; no authorization is not. A small QQ account does not make arbitrary remote control safe.

## Default Policy

Start with this behavior:

```yaml
owners: []
allowed_users: []
allowed_groups: []
workspaces: {}
commands:
  ask: true
  code: false
  shell: false
dangerous_requires_confirm: true
max_runtime_seconds: 600
max_output_chars: 6000
```

The bridge may receive events before configuration is complete, but it must not invoke an agent until the relevant user, chat, command, and workspace are allowed.

## Command Tiers

| Tier | Examples | Default |
|---|---|---|
| Read-only | `/ask`, `/status`, `/tail` | allowed only for allowlisted users |
| Workspace mutation | `/code`, `/fix`, `/apply` | require workspace allowlist and confirmation when risky |
| Process control | `/stop`, `/reset`, `/cancel` | owners only or job owner only |
| Shell | `/shell` | disabled; if enabled, use local allowlisted templates |
| External side effects | deploy, purchase, email, public post | require explicit local implementation and confirmation |

## Confirmation Nonce

For risky jobs, reply with a short nonce:

```text
Job 42 wants to edit files in /home/me/project. Reply: /approve 42 k7p9
```

Accept approval only from an owner or the original authorized requester, within a short timeout, for the same job id and policy decision.

## Process Boundaries

- Run under a dedicated local user when practical.
- Set cwd to an allowed workspace root, not chat input.
- Pass an env allowlist, not the full parent environment.
- Enforce timeout and output byte caps.
- Strip ANSI escapes and redact secret-shaped values.
- Store audit logs outside chat and avoid full private-chat retention by default.

## Baseline Failures This Skill Prevents

Pressure testing without this skill showed these predictable failure modes:

| Failure | Counter |
|---|---|
| "Just port the WeChat skill tonight" | Build a OneBot adapter; do not clone platform assumptions |
| "One generic skill for every chat and agent" | Generic at adapter boundaries; specific in the first implementation |
| "The QQ account is small, risk is acceptable" | Account risk is separate from local-machine compromise |
| "Fast phone control needs shell" | Queue agent tasks and keep shell disabled by default |
| "Group chat is convenient" | Require allowlisted group, allowlisted user, mention, and prefix |
