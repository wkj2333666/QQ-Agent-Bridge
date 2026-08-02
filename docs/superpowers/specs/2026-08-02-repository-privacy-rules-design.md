# Repository Privacy Rules Design

## Goal

Persist the repository's privacy requirements so human contributors and coding
agents do not add personal deployment data to tracked files or commit metadata.

## Scope

This change is documentation-only. It does not add a scanner, CI job,
pre-commit hook, or runtime behavior.

## Repository Rules

A root `AGENTS.md` will make the following requirements visible to coding
agents working anywhere in the repository:

- Never copy real QQ user ids, group ids, display names, chat excerpts, quoted
  messages, forwarded records, attachment names, or profile prompts into tests,
  examples, fixtures, documentation, snapshots, logs, or commit messages.
- Never commit credentials, cookies, QR codes, tokens, private configuration,
  runtime state, personal email addresses, or machine-specific paths.
- Use synthetic identities and conversations. The standard examples are
  `1000000001` for a user, `2000000001` for a group, `示例用户` for a display
  name, and `/home/example` or `/opt/qq-agent-bridge` for paths.
- Keep commit author addresses privacy-safe, preferably a GitHub noreply
  address.
- Before committing or pushing, review both the staged diff and newly added
  fixtures for personal data. History rewriting is required if private data was
  already published; deleting it in a later commit is insufficient.

The same policy will be summarized in `CONTRIBUTING.md` and reinforced in
`docs/PUBLISHING.md` so it remains visible to contributors and release owners.

## Acceptance Criteria

- `AGENTS.md` contains the repository-wide privacy requirements.
- `CONTRIBUTING.md` explicitly requires synthetic chat content and a
  privacy-safe commit email in addition to synthetic ids.
- `docs/PUBLISHING.md` requires review of commit messages and author metadata,
  and explains that a follow-up deletion does not remove data from history.
- No automated privacy checker is introduced.
