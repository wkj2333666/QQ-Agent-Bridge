# Long-Term Memory Recurring-Topic Evidence Design

## Problem

Long-term-memory collection is healthy, but production reviews can leave every
source pending for two independent reasons:

1. A systemd user namespace exposes unmapped system-owned path components as
   the kernel overflow UID. The hardened Cursor runtime validator rejects those
   components when the mount is writable, even though the service user cannot
   modify them.
2. The curator can correctly infer recurring topics from repeated questions,
   but deterministic validation requires affirmative statements. It therefore
   rejects every inferred topic. The curator may also cite related messages
   that do not contain the proposed topic, causing source-content mismatch.

The result is misleading: collection and review both run, but users see only
pending sources and no active or candidate memories.

## Chosen Design

### Hardened Runtime Trust

Accept an unmapped owner on system-prefix components above the current user's
home only when all of the following hold:

- the runtime resolves inside the current user's home;
- `/proc/self/uid_map` contains exactly one mapping for the current UID;
- the component owner equals the kernel overflow UID;
- the component is a real directory and is not group- or world-writable.

Runtime artifacts and all components inside the user's home remain restricted
to root or the current UID. Symlinks, writable parents, arbitrary foreign
owners, workspace-local runtimes, and temporary runtimes remain rejected.

### Repeated Topic Evidence

Repeated topic mentions may support a memory only under a narrow candidate
rule:

- category is `recurring_topic`;
- operation is `add` or `mark_candidate`;
- the proposal cites at least two distinct messages;
- the normalized topic occurs in every cited message;
- user memories cite only messages authored by that user;
- group memories cite messages in that group;
- evidence is normal, non-secret content;
- the stored result is forced to `mark_candidate` with status `candidate`,
  regardless of model confidence.

Questions and task requests can satisfy this repeated-topic rule. They cannot
directly create active memories. A direct affirmative self-statement continues
to use the existing active-memory path.

One-off questions, mixed-source citations, inferred preferences, identity
claims, relationships, sensitive facts, and third-party personal claims do not
gain any new exception.

### Curator Contract

The curator prompt will explicitly distinguish reviewed sources from cited
sources:

- inspect the whole batch;
- omit sources that do not justify durable memory;
- cite only sources containing the exact proposed topic;
- use `recurring_topic` plus `mark_candidate` for repeated questions or task
  requests;
- never label question-derived topics as active or as preferences.

This aligns model output with deterministic validation instead of weakening
validation to accept malformed evidence.

### Retry and Commit Behavior

An empty proposal set or a fully rejected proposal set remains retryable and
does not consume source rows. Successful candidate proposals consume only the
sources they cite; unrelated sources stay pending for later review or expiry.
SQLite commits remain atomic.

### Observability

Review logs will include rejection-reason counts without message text, QQ IDs,
or memory content. This makes `source_content_mismatch` and
`source_evidence_disallowed` distinguishable without leaking chat data.

## Alternatives Considered

### Prompt-Only Adjustment

Rejected because question-derived proposals still fail deterministic evidence
validation, even when the model marks them as candidates.

### Deterministic Topic Miner Outside the Agent

Rejected for now because tokenization, aliases, and Chinese topic extraction
would add a second semantic engine. The narrow validator rule keeps the Agent
responsible for proposing topics while deterministic code controls safety.

## Testing

- Unit tests cover the single-user UID namespace exception and retain all
  hardened-runtime rejection cases.
- Validator tests prove that two same-subject topic mentions become a candidate,
  while one mention, mismatched citations, active question-derived memories,
  and cross-subject evidence are rejected or downgraded.
- Curator prompt tests use a fake proposal to exercise the consuming validator,
  not source-text inspection.
- A real-Agent capability test feeds repeated QQ-style questions and requires a
  committed recurring-topic candidate.
- A systemd transient-unit test runs the real `/memory review now` pipeline with
  an isolated SQLite database and the production Cursor binary path.

## Success Criteria

- Production curator no longer fails with `助手沙箱未配置` in the current
  systemd user namespace.
- Repeated questions about one topic can create a candidate memory.
- No question-derived topic becomes active without later affirmative support.
- Rejected sources remain pending, and logs explain rejection classes without
  exposing chat content.
