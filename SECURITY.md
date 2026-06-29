# Security Policy

QQ Agent Bridge connects chat messages to local agent processes. Treat every
deployment as security-sensitive.

## Supported Versions

The main branch is the only supported development line until the project starts
publishing tagged releases.

## Reporting a Vulnerability

Please report security issues privately to the maintainers before publishing
details. Include:

- affected version or commit,
- deployment shape,
- command or message flow involved,
- expected and actual behavior,
- any relevant sanitized logs.

Do not include real QQ credentials, cookies, QR data, Cursor auth files, access
tokens, private chats, or group logs in a public issue.

## Secret Handling

Never commit:

- `config.yaml`,
- `.env` files,
- OneBot or NapCat access tokens,
- QQ login state, cookies, QR codes, or screenshots,
- Cursor/Codex/Claude auth state,
- files under `runtime/*/data/`, `runtime/*/config/`, `runtime/*/plugins/`,
- files under `workspace/downloads/`,
- raw private or group chat logs.

Use `config.example.yaml` as the public template and keep real values local.

## Deployment Safety

- Use a dedicated QQ account for testing.
- Keep OneBot and WebUI ports bound to localhost unless you have a separate
  authenticated reverse proxy.
- Keep `code: false` until the owner workflow is tested.
- Keep workspace allowlists narrow.
- Prefer Bubblewrap sandboxing for local CLI agents.
- Do not mount a real home directory into gateway or agent containers.

This project is a bridge. It does not make third-party QQ gateways, model
providers, or local CLI agents safe by default.
