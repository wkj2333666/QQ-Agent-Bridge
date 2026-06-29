# Contributing

Thanks for helping improve QQ Agent Bridge.

## Development Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

Copy `config.example.yaml` to `config.yaml` for local testing. Do not commit
real config files or runtime state.

## Pull Request Checklist

- Keep changes scoped and explain the user-facing behavior.
- Add or update tests for behavior changes.
- Run `python -m pytest -q`.
- Run `git diff --check`.
- Do not commit credentials, QQ ids from real chats, QR codes, cookies, tokens,
  local absolute paths, generated downloads, or runtime container state.
- Use placeholder ids such as `1000000001` for users and `2000000001` for
  groups in tests and docs.

## Code Style

- Prefer existing module boundaries and small helpers.
- Keep bridge policy decisions deterministic and testable.
- Keep user-facing replies QQ-friendly and short.
- Treat all QQ message text, quoted messages, forwarded records, and attachments
  as untrusted user input.

## Security-Sensitive Changes

For changes touching command permissions, sandboxing, outgoing file sending,
resource staging, auth/token handling, or process execution, include tests for
denied paths as well as happy paths.
