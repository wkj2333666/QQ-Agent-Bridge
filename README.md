# QQ Agent Bridge

[中文文档](README.zh-CN.md)

Safety-first OneBot v11 bridge from QQ private/group chats to local CLI agents
such as Cursor Agent, Codex, Claude Code, or compatible custom runners.

This project is not a QQ protocol implementation and not a full chatbot
platform. It focuses on the narrow bridge layer between OneBot events and a
local agent runtime:

```text
QQ user/group
  -> OneBot v11 gateway, such as NapCatQQ
  -> reverse WebSocket
  -> bridge core: authz, routing, queue, memory, resource staging, redaction
  -> local CLI agent inside a constrained workspace
  -> QQ-native text, image, file, audio, or voice replies
```

## Features

- OneBot v11 reverse WebSocket server.
- QQ group mention routing and private-chat defaults.
- Owner/user/group allowlists.
- Read-only `/ask`, `/plan`, `/search`, `/task`, `/status`, `/help`, `/profile`, `/mode`
  command set for ordinary users.
- Persistent `/schedule`; group mutations are owner-only, while allowed private
  users can manage their own schedules when enabled.
- Owner-only `/code`, `/approve`, `/stop`, `/reset`, `/reload`.
- Queueing and global agent concurrency limits.
- Short-term conversation memory per private chat or group.
- Ambient group context for natural chat without treating background messages
  as commands.
- Configurable per-group and per-user profiles.
- Persistent one-shot, finite, bounded, and arbitrary recurring schedules.
- Long-task progress messages and heartbeat updates.
- Attachment cache for mobile-friendly flows: send an image/file first, then
  mention the bot to process it.
- Resource handoff for images, files, voice, audio, videos, URLs, and forwarded
  chat records.
- Outgoing image/file/audio/voice delivery through guarded `QQBOT_SEND_*`
  directives.
- Bubblewrap-based sandboxing for local CLI agent execution.
- Runtime skill packs that teach the agent web search, weather, office docs,
  media understanding, audio/voice/music constraints, anti-hallucination rules,
  and QQ bridge resource protocols.

## Status

This is an early project. The bridge is useful, but the public API, config
schema, and runtime skill format may change before a stable `1.0` release.

## Important Disclaimer

QQ Agent Bridge is an independent bridge layer. It is not affiliated with
Tencent, QQ, NapCatQQ, Cursor, OpenAI, Anthropic, or other agent providers.

Personal QQ gateways may violate platform rules or carry account risk. Use a
test account first and review the gateway you choose. You are responsible for
your deployment, credentials, messages, files, model usage, and compliance with
third-party terms.

## Quick Start

```bash
git clone <repo-url> qq-agent-bridge
cd qq-agent-bridge

cp config.example.yaml config.yaml
# Edit config.yaml:
# - owners
# - allowed_users / allowed_groups
# - workspaces
# - agent.runtime and runtime command settings
# - onebot.access_token
# - bot.self_id after QQ gateway login

# uv manages the bridge environment and locks it in uv.lock.
# Agent tasks run separately in micromamba's base environment.
uv sync --locked

uv run python -m src.qq_agent_bridge.main --echo-only
```

From an allowed private chat, send a message and confirm the bridge replies in
echo mode. After the OneBot gateway is connected and echo works, run:

```bash
uv run python -m src.qq_agent_bridge.main
```

## Agent Trace Logs

To diagnose slow, timed-out, or tool-heavy Agent runs, enable bounded local traces in
`config.yaml`:

```yaml
agent:
  trace_enabled: true
  trace_root: "runtime/agent-traces"
  trace_max_bytes: 5242880
  trace_max_line_chars: 2000
```

Each invocation writes a private JSONL file with lifecycle, tool summaries, stderr,
timeout, and exit events. Tracing is disabled by default, omits the original prompt,
redacts secrets, and never sends logs to QQ. The directory uses `0700` and files use
`0600`; remove local traces when they are no longer needed.

## Optional Local Speech Recognition

The bridge can transcribe QQ voice resources with a local `whisper.cpp` binary.
The isolated installer publishes the binary and model through stable
`current/bin` and `current/models` paths; see
[runtime/asr/README.md](runtime/asr/README.md) for the verified installation
and configuration steps.

## OneBot Gateway

This repository includes a NapCatQQ compose template under `runtime/napcat/`.
It is only a local deployment helper; NapCatQQ is a separate project.

Typical setup:

```bash
cd runtime/napcat
mkdir -p data/qq config plugins
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose up
```

Then open the gateway WebUI, log in, and configure a WebSocket client pointing
to the bridge:

```text
ws://127.0.0.1:8765/onebot
```

Use the same access token in the gateway and `config.yaml`.

See [runtime/napcat/README.md](runtime/napcat/README.md) for details.

## Commands

Private chat:

```text
hello
/ask explain this error
/task search the web and summarize it
```

Group chat:

```text
@bot hello
@bot /ask explain this
@bot /task summarize the linked page
```

Common commands:

- `/ask <text>`: fast question answering or lightweight chat.
- `/plan <text>`: read-only planning.
- `/search <keyword>`: bounded literal search in the configured workspace.
- `/task <text>`: explicit tool-using agent task without modifying existing
  workspace files.
- `/schedule <natural language>`: create a persistent scheduled `send`, `ask`,
  or `task`. Natural-language time interpretation is validated as RFC 5545
  recurrence data before it is stored.
- `/schedule help`: show examples for one-shot, counted, bounded, unbounded,
  and advanced arbitrary recurrence rules.
- `/schedule list|show|pause|resume|run|cancel`: manage schedules by ID or by
  indices such as `0` and `-1`.
- `/status`: show running and queued jobs.
- `/help`: show a short QQ-friendly help message.
- `/help <command>` or `/<command> help`: show detailed usage, permissions, and examples.
- `/permission`: view per-group command access; only group owners can set/clear overrides.
- `/profile`: show the current profile.
- `/profile set <prompt>`: set the current private or group profile.
- `/profile clear`: clear the current private or group profile.
- `/mode`: show the default mode for commandless mentions in this group.
- `/mode set chat|ask|plan|task`: `chat` runs the interjection decision; the other modes execute directly.
- `/mode clear`: remove the group override and use the global default.

Command access is configured independently in `config.yaml`:

```yaml
commands:
  ask: user
  task: user
  code: owner
  shell: disabled
  permission: user
  groups:
    "180188783":
      task: disabled
      search: owner
```

Use `user` for every otherwise authorized user, `owner` for owners only, and
`disabled` to turn a command off. The older boolean form remains accepted:
`true` preserves the historical default for that command and `false` means
`disabled`.

`commands.groups` contains per-group overrides; commands not listed for a group
inherit the global setting. Group owners can change them with
`/permission set <command> user|owner|disabled` and restore the global setting
with `/permission clear [command]`.

Owner-only commands:

- `/code <request>`: trusted workspace-editing path, with confirmation flow.
- `/approve <job> <nonce>`: approve a pending risky job.
- `/stop <job>`: cancel a job.
- `/reset`: clear current conversation memory and group ambient context.
- `/reload`: reload `config.yaml`.

In groups, only owners can change the group profile or `/mode`; other group
members can view them. In private chats, an allowed user can change their own
private profile. `/mode` is group-only.

A commandless mention still goes through the existing chat-versus-question
decision first. Casual chat remains in the interjection flow; only a message
classified as needing an answer is routed to the group's `ask`, `plan`, or
`task` default. Explicit commands are unaffected. Changes are persisted to
`config.yaml`.

In groups, schedule creation and management follow the effective `/schedule`
command permission for that group: `owner` limits mutations to owners, while
`user` allows otherwise authorized group members. Allowed private-chat users
can manage their own schedules when `scheduler.allow_private_users` is enabled.
The scheduler is disabled by
default; review the timezone and limits in `config.example.yaml` before
enabling it. Schedules use durable SQLite storage and resume after a bridge
restart. Arbitrary recurrence is represented by one RFC 5545 RRULE, so rules
such as weekdays, every second Tuesday, or the last workday of each month do
not require hardcoded period types. Scheduler limits can be hot-reloaded;
changing `scheduler.database_path` requires a bridge restart. Non-owner
schedule creation performs one combined model pass for time interpretation and
safety review; suspicious high-frequency, resource-heavy, spam-like, recursive,
or dangerous schedules are rejected before persistence. Owner-created schedules
skip this extra safety gate.

## Profiles

Profiles are optional role/personality prompts configured in `config.yaml`:

```yaml
profiles:
  default: |
    - You are a lightweight, friendly QQ assistant.
  groups:
    "2000000001": |
      - You are this group's technical helper.
      - Keep replies short and practical.
  users:
    "1000000001": |
      - You are a patient study companion in private chat.
```

Profile isolation is by current chat scope:

- group chat uses `profiles.groups[group_id]` or `profiles.default`;
- private chat uses `profiles.users[user_id]` or `profiles.default`;
- profiles from other groups/users are not exposed to the agent.

Per-group commandless mention modes can also be configured directly:

```yaml
mention_modes:
  default: ask
  groups:
    "2000000001": task
```

Set a group to `chat` when an @ should first be treated as casual conversation. `ask`,
`plan`, and `task` skip that decision and directly enter the selected mode. Messages
without an @ still feed the ambient memory and proactive interjection flow in every mode.

Only `chat`, `ask`, `plan`, and `task` are accepted. For `ask`, `plan`, and `task`,
the corresponding command must also be enabled; `chat` uses the `/ask` permission
for its decision step. Risky modes such as `code` and `shell` cannot be implicit.

## Agent Runtime

No CLI agent runtime is enabled by default. Choose one explicitly in
`config.yaml`.

Built-in runtime choices:

- `cursor-cli`: invokes Cursor Agent with the command layout shown below.
- `custom-cli`: invokes command templates from `agent.command`, useful for
  Codex, Claude Code, wrapper scripts, or other compatible runners.

For Cursor Agent:

```yaml
agent:
  runtime: "cursor-cli"
  binary: "cursor-agent"
```

Cursor commands are built like this:

```text
/ask:  bwrap ... micromamba run -n base cursor-agent -p --workspace <ws> --mode ask --sandbox disabled
/plan: bwrap ... micromamba run -n base cursor-agent -p --workspace <ws> --mode plan --sandbox disabled
/task: bwrap ... micromamba run -n base cursor-agent -p --workspace <ws> --sandbox disabled --force
/code: bwrap ... micromamba run -n base cursor-agent -p --workspace <ws> --trust --force --sandbox disabled
```

For another CLI, use `custom-cli`:

```yaml
agent:
  runtime: "custom-cli"
  command:
    ask: ["your-agent", "--mode", "{mode}", "--model", "{model}", "{prompt}"]
    plan: ["your-agent", "--mode", "{mode}", "--model", "{model}", "{prompt}"]
    task: ["your-agent", "--workspace", "{workspace}", "{prompt}"]
    code: ["your-agent", "--workspace", "{workspace}", "{prompt}"]
```

Supported template placeholders are `{prompt}`, `{workspace}`, `{mode}`,
`{model}`, and `{stream}`. Exact Codex or Claude Code flags are not hardcoded
because their CLIs evolve; keep those details in your config or wrapper script.
Agent processes are intentionally required to run through the existing
`micromamba run -n base` environment. Do not disable this guard and do not let
an Agent create `.venv`, `venv`, `env`, or install dependencies inside the
workspace. If the base environment lacks a dependency, report the missing
dependency and provision it outside the workspace before retrying.

Key safety choices:

- `/ask` and `/plan` stay read-only.
- `/task` can use tools but should only create deliverables in the per-job
  outbox.
- `/code` is the owner-approved editing path and should remain disabled until
  tested.
- The workspace comes only from config, never from chat text.
- Bubblewrap mounts system/runtime paths read-only and uses a private sandbox
  home for agent auth state.

## Resource Sending

For generated files, the agent creates files in the per-job outbox named in the
prompt, then prints one directive per line:

```text
QQBOT_SEND_IMAGE: <token> downloads/qq-agent-bridge/outgoing/<job>/image.png
QQBOT_SEND_FILE: <token> downloads/qq-agent-bridge/outgoing/<job>/report.pdf
QQBOT_SEND_AUDIO: <token> downloads/qq-agent-bridge/outgoing/<job>/audio.mp3
QQBOT_SEND_VOICE: <token> downloads/qq-agent-bridge/outgoing/<job>/voice.wav duration=12
```

The bridge strips directive lines from the final text, validates token and path,
checks file limits, copies accepted files into a stable sending directory, then
sends them through OneBot actions.

QQ voice is limited to generated short human voice. The directive must include
real duration metadata, and the bridge verifies the actual file duration is at
or below 60 seconds. Generic audio and music are sent as files.

## Safety Guardrails

- Default deny configuration.
- User/group/workspace/command allowlists.
- Mention required in groups.
- Private users must be allowlisted.
- Ordinary group members can use read-only commands when the group is allowed.
- Owner-only risky commands.
- Confirmation nonce for `/code`.
- Job timeout, output cap, and dedupe by message id.
- Redaction of common token/key/password patterns.
- Agent output guard against prompt/context echo.
- Staged resources excluded from `/search` and conversation memory.
- Outgoing resources accepted only from the current job outbox.
- Runtime container state and local credentials ignored by git.

## How This Differs From Existing Projects

- NapCatQQ and Lagrange are QQ gateway/protocol projects. This bridge consumes
  OneBot events; it does not implement the QQ protocol.
- OneBot v11 is the protocol contract. This project is one Python bridge built
  on top of that contract.
- NoneBot2 and Koishi are general bot frameworks with broad plugin ecosystems.
  This project is narrower: QQ to local agent runtime with safety controls.
- LangBot and CowAgent-style projects are multi-platform AI bot systems. This
  bridge focuses on local CLI agents that can inspect workspaces, run tasks,
  and hand files back to QQ under explicit policy.

## Verify

```bash
uv run python dry_run.py
uv run pytest -q
git diff --check
```

Default tests do not invoke real Cursor, Codex, or Claude CLIs. To smoke-test
local CLI availability on your own machine:

```bash
QQ_AGENT_BRIDGE_CLI_SMOKE=1 uv run pytest tests/test_cli_smoke.py -q
```

You can override commands with `QQ_AGENT_BRIDGE_SMOKE_CURSOR_CMD`,
`QQ_AGENT_BRIDGE_SMOKE_CODEX_CMD`, or `QQ_AGENT_BRIDGE_SMOKE_CLAUDE_CMD`.

To run slower contract tests against a real agent runtime:

```bash
QQ_AGENT_BRIDGE_AGENT_E2E=1 \
QQ_AGENT_BRIDGE_E2E_RUNTIME=cursor-cli \
QQ_AGENT_BRIDGE_E2E_CHAT_MODEL=auto \
QQ_AGENT_BRIDGE_E2E_TASK_MODEL=kimi-k2.5 \
uv run pytest tests/test_agent_e2e.py -q
```

For `cursor-cli`, these tests use `bwrap` by default so Cursor can trust the
temporary pytest workspace without an interactive prompt. Set
`QQ_AGENT_BRIDGE_E2E_BWRAP=0` only if your command wrapper handles workspace
trust another way.

For Codex, Claude Code, or another wrapper, set
`QQ_AGENT_BRIDGE_E2E_RUNTIME=custom-cli` and provide command templates such as
`QQ_AGENT_BRIDGE_E2E_ASK_CMD` and `QQ_AGENT_BRIDGE_E2E_TASK_CMD`. Templates may
use `{prompt}`, `{workspace}`, `{mode}`, `{model}`, and `{stream}`.

To run semantic capability evals, enable the opt-in suite below. These tests run
the target agent, then ask a judge agent to score whether the result satisfied
the capability criteria. Hard checks still verify required tokens, forbidden
claims, and similar deterministic conditions.

```bash
QQ_AGENT_BRIDGE_CAPABILITY_EVAL=1 \
QQ_AGENT_BRIDGE_E2E_RUNTIME=cursor-cli \
QQ_AGENT_BRIDGE_CAPABILITY_CHAT_MODEL=auto \
QQ_AGENT_BRIDGE_CAPABILITY_TASK_MODEL=kimi-k2.5 \
uv run pytest tests/test_agent_capability_eval.py -q
```

By default the judge uses the same runtime. Override it with
`QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_RUNTIME`, `QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_MODEL`,
or `QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_ASK_CMD` when you want an independent judge.

## Repository Hygiene

Do not commit real runtime state:

- `config.yaml`
- `.env`
- QR codes, cookies, QQ credentials, login screenshots
- OneBot/NapCat tokens
- Cursor/Codex/Claude auth state
- `runtime/*/data/`
- `runtime/*/config/`
- `runtime/*/plugins/`
- `workspace/downloads/`
- private/group chat logs

See [SECURITY.md](SECURITY.md) before publishing a deployment.
See [docs/PUBLISHING.md](docs/PUBLISHING.md) before making a repository public.

## License

MIT License. See [LICENSE](LICENSE).
