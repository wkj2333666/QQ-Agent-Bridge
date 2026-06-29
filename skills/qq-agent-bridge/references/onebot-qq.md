# OneBot QQ Adapter Notes

Use this reference when implementing or reviewing QQ transport code for `qq-agent-bridge`.

## Preferred Shape

Prefer OneBot v11 reverse WebSocket for a local bridge:

```text
OneBot gateway -> connects to local bridge ws://127.0.0.1:<port>
local bridge -> receives events and sends OneBot action frames on the same connection
```

Do not hardcode NapCatQQ, Lagrange, or LLOneBot paths. Read the selected gateway's current config and expose the URL, access token, and self id as local configuration.

## Event Fields To Normalize

For message events, normalize only the fields the bridge core needs:

| OneBot-ish field | Bridge field |
|---|---|
| `message_id` | `id` |
| `self_id` | bot account id |
| `message_type` | `isGroup` |
| `group_id` | group `chatId` |
| `user_id` | `senderId` |
| `raw_message` or text segments | `text` |
| `time` | `timestamp` |

Ignore non-message events until explicitly needed. Ignore anonymous messages, forwarded messages, notices, requests, attachments, and images as commands unless the implementation has a tested parser for them.

## Action Frames

Send replies through OneBot actions, usually:

```json
{"action":"send_group_msg","params":{"group_id":123,"message":"text"},"echo":"job-1"}
{"action":"send_private_msg","params":{"user_id":123,"message":"text"},"echo":"job-1"}
```

Match action responses by `echo`. Treat failures as transport errors and report short, chat-safe status messages to logs, not raw stack traces to QQ.

## Group Trigger Rules

In group chats, invoke only when all are true:

- `group_id` is allowed.
- `user_id` is allowed for the requested command.
- The message mentions the bot and starts with the configured prefix.
- The command parser returns a known command.

Private chats may omit mention requirements but still need allowlisted `user_id`.

## Test Fixtures

Keep fixtures for:

- private allowed user
- private denied user
- group allowed user with mention and prefix
- group allowed user without mention
- group denied user with valid-looking command
- duplicate `message_id`
- long output requiring chunking
- OneBot action failure response
