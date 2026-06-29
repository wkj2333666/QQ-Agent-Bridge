---
name: qq-agent-bridge
description: Use when installing or configuring QQ OneBot v11 gateways, connecting local coding agents, LLM APIs, or custom agent runtimes to QQ through NapCatQQ, Lagrange, or LLOneBot; designing chat-to-agent bridges; or reviewing QQ bot code that can invoke Claude Code, Codex, Cursor agents, OpenAI API, or custom models.
---

# QQ Agent Bridge

## Overview

Build a small QQ bridge with three replaceable parts: OneBot transport adapter, policy-controlled bridge core, and agent runtime adapter. Treat QQ as an untrusted remote-control surface for the local machine.

## First Decisions

Ask or infer these before writing code:

| Question | Default |
|---|---|
| QQ transport | OneBot v11 reverse WebSocket |
| Protocol implementation | NapCatQQ or Lagrange, locally configured |
| Agent runtime | CLI subprocess for Claude Code, Codex, or Cursor; HTTP adapter for LLM APIs |
| Deployment | Local daemon near the workspace |
| Access | Default deny with user, group, command, and workspace allowlists |

For official/compliance-first QQ bots, use the QQ Open Platform instead of third-party protocol gateways, and keep the same bridge boundaries.

## Architecture

```text
QQ user/group
  -> OneBot gateway (NapCatQQ/Lagrange/LLOneBot)
  -> reverse WebSocket transport adapter
  -> bridge core: authz, routing, queue, logs, dedupe
  -> agent adapter: Codex/Claude/Cursor CLI or LLM API
  -> chunked, redacted replies through OneBot actions
```

Keep the generic contract at the adapter boundary, not in a giant universal bot:

```ts
type ChatEvent = {
  id: string;
  platform: "qq";
  chatId: string;
  senderId: string;
  isGroup: boolean;
  mentionedBot: boolean;
  text: string;
  timestamp: number;
};
```

## Required Guardrails

- Require allowlisted `user_id`; require allowlisted `group_id` for group use.
- In groups, require an @ mention plus a command prefix such as `/codex`.
- Disable raw shell by default. Prefer task commands: `/ask`, `/code`, `/status`, `/stop`, `/reset`, `/approve`.
- Restrict work to configured workspace roots; never accept a chat-supplied path as trusted.
- Run agent jobs asynchronously with timeout, cancellation, max output size, and duplicate-event suppression.
- Redact secrets, tokens, cookies, QR data, and environment values before sending chat replies or logs.
- Log request id, QQ ids, policy decision, workspace, command, exit status, and redaction status.
- Require a confirmation nonce for mutating, long-running, networked, deploy-like, or file-deleting actions.

## Runtime Agent Skill Injection

When the bridge invokes a CLI agent for `/task` or `/code`, inject `skills/qq-agent-runtime/SKILL.md` into the prompt instead of relying on the model to infer operational discipline:

- Mode correction: explain that CLI transport details are not the same thing as QQ command semantics; `QQ_COMMAND=/task` is still agentic task execution.
- Basic agent discipline: require understanding the user outcome, using tools for evidence, separating facts from guesses, verifying before claiming completion, and reporting blockers honestly.
- Web research: require actual WebSearch or browser use before answering public-info requests; if tools fail, say so instead of answering from memory.
- Sources: require source URLs for key public claims and mark unsupported facts as not found.
- File deliverables: require the agent to create files only in the per-job outbox, verify they exist and are non-empty, then emit `QQBOT_SEND_FILE`, `QQBOT_SEND_IMAGE`, `QQBOT_SEND_AUDIO`, or `QQBOT_SEND_VOICE` with the real path. `QQBOT_SEND_AUDIO` is sent as a file. `QQBOT_SEND_VOICE` is only for generated human voice/short QQ record replies and must include real `duration=` metadata; the bridge must also be able to verify actual file duration at or below QQ's 60-second limit.
- Mode boundary: `/task` may create new deliverable files in the outbox; only `/code` may modify project files or existing workspace files.

## Workflow

1. Install or configure a OneBot-compatible QQ gateway first. OneBot v11 is a protocol, not a standalone app; choose a maintained gateway and configure reverse WebSocket.
2. Build or inspect the OneBot adapter. Verify event parsing and `send_private_msg`/`send_group_msg` actions with a local echo handler.
3. Add the policy engine before any agent execution. Default config must authorize no one.
4. Add the agent adapter as a bounded subprocess or HTTP client. For Cursor CLI, Codex, and Claude Code, use the CLI subprocess pattern and discover the local binary/configuration instead of hardcoding command flags.
5. Add job queue and progress replies. Acknowledge quickly, then send compact updates.
6. Add tests or dry-run fixtures for private messages, group mentions, unauthorized users, duplicate events, long output, timeout, and secret redaction.

## References

- Read `references/gateway-setup.md` when the environment does not already have a configured OneBot v11 gateway.
- Read `references/onebot-qq.md` when implementing or reviewing the OneBot/NapCat/Lagrange adapter.
- Read `references/agent-runtimes.md` when implementing Codex, Claude Code, Cursor CLI, OpenAI API, or custom agent adapters.
- Read `references/safety-policy.md` before enabling agent execution from QQ, especially group chat or CLI agents.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Building one mega-bot for QQ, WeChat, and every agent | Implement one transport and one agent first; keep adapters replaceable |
| Letting group members send raw shell | Use task commands; keep shell disabled unless locally allowlisted |
| Mixing OneBot parsing with Codex/Claude process logic | Split transport adapter, bridge core, and agent adapter |
| Trusting QQ ids, message text, or attachment URLs | Validate ids against local config; sanitize and scope all input |
| Returning full terminal output | Chunk, summarize, redact, and cap replies |
