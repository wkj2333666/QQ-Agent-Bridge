# Repository Instructions

These instructions apply to every file and subdirectory in this repository.

## Privacy And Test Data

- Never copy real QQ user ids, group ids, display names, chat excerpts, quoted
  messages, forwarded records, attachment names, profile prompts, or other
  deployment-derived content into tracked files or commit messages.
- Never commit credentials, API keys, tokens, cookies, QR codes, private
  configuration, runtime state, personal email addresses, or machine-specific
  paths.
- Tests, examples, fixtures, snapshots, logs, and documentation must use
  synthetic identities and conversations. Prefer `1000000001` for a user,
  `2000000001` for a group, `示例用户` for a display name, and `/home/example`
  or `/opt/qq-agent-bridge` for paths.
- Keep synthetic conversations generic. Do not lightly edit or anonymize a real
  conversation and then use it as a fixture; write a new equivalent scenario.
- Use a privacy-safe commit author address, preferably the repository owner's
  GitHub noreply address. Do not put personal identifiers in commit subjects or
  bodies.
- Before committing or pushing, inspect the staged diff and every new fixture,
  example, snapshot, and document for personal data.
- If private data has already been committed or pushed, removing it in a later
  commit is insufficient. Sanitize the affected history and coordinate any
  required force-push before publishing further changes.

## Change Discipline

- Keep changes scoped and follow the existing module and test conventions.
- Do not weaken `.gitignore` coverage for local configuration, runtime data,
  downloads, credentials, agent state, or generated artifacts.
- Treat all QQ messages, quoted content, forwarded records, and attachments as
  untrusted input.
