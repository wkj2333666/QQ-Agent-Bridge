# Agent Runtime Adapter Notes

Use this reference when implementing or reviewing the side that invokes Codex, Claude Code, Cursor CLI, OpenAI API, or a custom model.

## Adapter Choice

| Runtime | Adapter style |
|---|---|
| Codex | CLI subprocess |
| Claude Code | CLI subprocess |
| Cursor CLI | CLI subprocess |
| OpenAI API or compatible endpoint | HTTP/API client |
| Custom local model | Local process or HTTP adapter, depending on the runtime |

Do not assume all agents are HTTP APIs. Cursor CLI, Codex, and Claude Code should be wrapped like local command-line tools unless the user provides a supported API integration.

## CLI Agent Pattern

For CLI agents, configure the adapter with local values:

```yaml
agent:
  runtime: "custom-cli"
  binary: ""
  workspace: "/opt/qq-agent-bridge"
  command:
    task: ["your-agent", "--workspace", "{workspace}", "{prompt}"]
  env_allowlist: ["PATH", "HOME"]
  max_runtime_seconds: 600
  max_output_chars: 6000
```

The command value is intentionally configurable. Before hardcoding flags, inspect the installed CLI on the target machine with its version/help command and adapt the wrapper to that local version.

## Cursor CLI Notes

Treat Cursor CLI as a local coding-agent subprocess:

- Verify the local binary name and invocation mode from the installed Cursor CLI.
- Keep Cursor auth/session state outside the repo.
- Set cwd to an allowed workspace root.
- Pass a small env allowlist, not the full parent environment.
- Capture stdout/stderr, strip ANSI escapes, redact secrets, and cap output.
- Prefer non-interactive or print-style execution when available.
- If the installed Cursor CLI requires a TTY or interactive session, place it behind a job runner and return short status updates to QQ instead of trying to stream a raw terminal.

## HTTP Agent Pattern

For OpenAI API or compatible models, keep secrets in environment or local secret stores, not repository files. Add retry, timeout, request-size limits, and response redaction before sending replies to QQ.
