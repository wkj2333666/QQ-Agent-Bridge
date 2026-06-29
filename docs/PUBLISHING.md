# Publishing Checklist

Use this checklist before making a repository public.

## Current Tree

- `config.yaml` must remain ignored.
- Runtime gateway state must remain ignored:
  - `runtime/*/data/`
  - `runtime/*/config/`
  - `runtime/*/plugins/`
  - `runtime/*/logs/`
- Agent and download state must remain ignored:
  - `.cursor/`
  - `.codex/`
  - `workspace/downloads/`
  - `downloads/`
- Use placeholder user ids such as `1000000001` and group ids such as
  `2000000001` in tests and docs.
- Use placeholder paths such as `/opt/qq-agent-bridge/workspace` in examples.

## History Audit

The current working tree should not contain real QQ ids, real group ids, real
tokens, local user paths, or chat names.

Before publishing, run:

```bash
git grep -n -I -E 'real-id-or-name|real-token|/home/your-user' -- .
git log --all -- config.yaml runtime/napcat/data runtime/napcat/config .env
git rev-list --all --objects | grep -E 'config\.yaml|auth\.json|token|cookie|qr|runtime/.*/data'
```

If this repository was developed privately before sanitization, old commits may
still contain personal ids, group ids, names, or local paths even when the
current tree is clean. Do not publish that history directly unless you are
comfortable with those values being public.

Recommended public-release options:

1. Create a fresh public repository from the sanitized tree with one initial
   commit.
2. Or, rewrite history to remove personal values, then force-push only to a new
   public remote.

Do not rewrite shared history without coordinating with every collaborator.

## Release Smoke Test

```bash
python -m pytest -q
git diff --check
```

Then read:

- [README.md](../README.md)
- [SECURITY.md](../SECURITY.md)
- [CONTRIBUTING.md](../CONTRIBUTING.md)
- [config.example.yaml](../config.example.yaml)

Make sure they do not contain deployment secrets or personal identifiers.
