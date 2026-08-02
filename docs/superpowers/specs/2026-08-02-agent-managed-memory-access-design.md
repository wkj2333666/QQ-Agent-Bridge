# Agent-Managed Memory Access Design

**Date:** 2026-08-02

**Status:** Approved in conversation; awaiting review of this written specification

## Goal

Let advanced Agent jobs search and maintain the existing scoped long-term memory
without introducing domain-specific state such as course chapters, weather cursors,
or novel-writing fields.

The first release applies to `/task` and `task` schedules. Ordinary chat and
`/ask` retain the current low-latency automatic memory retrieval path.

## Design Principles

- Memory content remains free-form natural language.
- The Agent chooses its own search queries and decides which work notes are useful.
- The bridge, not the Agent, controls scope, subject visibility, provenance,
  validation, and commit timing.
- Existing conversation review remains the authority for claims about users and
  groups.
- Agent-authored work notes describe the Agent's own plan, progress, evidence, and
  pending work. They do not become evidence for new personal facts.
- A successful QQ delivery is the commit boundary. Failed, cancelled, timed-out,
  or undelivered jobs do not change durable Agent work notes.
- The feature is provider-neutral and must work with Cursor, Codex, Claude Code,
  and configured custom Agent CLIs.

## Existing Behavior Retained

The existing long-term-memory system continues to:

- require explicit per-group or per-private-chat enablement;
- isolate group and private scopes;
- collect eligible QQ messages into the review buffer;
- use the curator and deterministic validator for user and group memories;
- retrieve a bounded set of active memories into chat, ask, proactive, task, and
  schedule prompts;
- preserve candidate, contradiction, sensitivity, decay, provenance, and hard
  deletion behavior.

Automatic prompt injection is not removed. Autonomous retrieval supplements it
for advanced jobs.

## Approaches Considered

### 1. Use Only Automatic Prompt Injection

The bridge selects memories before invocation and asks the Agent to infer progress
from them. This requires no new tool surface, but bounded retrieval can omit the
one prior note a recurring task needs. The Agent also cannot refine its query after
learning more during execution.

### 2. Expose a Live Memory MCP Server

The Agent queries the production memory service over MCP. This offers live reads
and conventional tools, but the current hardened Agent environments deliberately
disable arbitrary MCP configuration. It also creates provider-specific setup,
long-lived credentials, a larger network attack surface, and direct coupling
between an untrusted subprocess and production state.

### 3. Scoped Snapshot and Deferred Proposals

The bridge creates a job-scoped, read-only snapshot and a provider-neutral helper
for searching it. The Agent records proposed writes in a separate job-scoped
proposal area. The bridge validates and commits those proposals only after QQ
delivery succeeds.

This is the chosen approach. It gives the Agent autonomous search while preserving
sandbox isolation, provider portability, deterministic authorization, and atomic
delivery semantics.

## Memory Model

### Free-Form Content

No domain schema is introduced. An Agent work note may contain any useful natural
language, for example:

```text
四级语法连续讲解：已完成基本句型；下一次讲一般时与完成时的区别。
用户反馈：例句比术语定义更容易理解，后续先给例句再解释规则。
```

The same mechanism can record a research watchlist, serial-writing continuity,
service-inspection findings, exercise adjustments, or any other ongoing work.

### Minimal System Envelope

Every record still requires non-domain metadata:

- stable memory ID and revision;
- exact group or private scope;
- authorized subject identity;
- status and sensitivity;
- source and actor provenance;
- creation, update, support, and expiry timestamps;
- originating job ID and optional schedule ID for audit and idempotency.

These fields are security and consistency boundaries, not a task-specific schema.
The note body remains opaque natural language to the bridge except for existing
length, secret, and safety checks.

### Provenance Separation

Agent work notes use a distinct generic provenance class. They may be searched by
future advanced jobs, but they cannot be cited by the curator as proof that a user
has a preference, identity, relationship, or other personal fact.

Only the conversation-review path may create or strengthen those user/group facts.
An Agent cannot turn its own previous statement into evidence about a person.

## Authorized Retrieval

### Job Memory Session

Before invoking an eligible job, the bridge creates an immutable
`MemoryToolSession` containing:

- exact memory scope derived from trusted `ChatEvent` fields;
- the current sender;
- real QQ mentions parsed from structured segments;
- the trusted quoted-message sender, when present;
- the current job ID;
- the schedule ID for scheduled execution, when present;
- a random per-job capability token;
- the version of every exported memory record.

The Agent cannot supply or widen these values.

### Subject Visibility

The snapshot contains only records the existing retriever would be allowed to
expose for the current interaction:

- group-level records for the current group;
- records about the current sender;
- records about users genuinely mentioned in the QQ event;
- records about the trusted quoted-message sender.

For a scheduled task, stored structured mentions are treated as genuine mentions.
Being the group owner does not silently export every participant's personal
memory into an unrelated task.

Candidates, rejected records, contradicted losers, expired records, review-buffer
text, other groups, and private scopes are never exported.

### Provider-Neutral Query Helper

The runtime skill bundle contains a dependency-free helper with these logical
operations:

```text
memory search <query>
memory recent
memory read <id>
memory propose-add <text>
memory propose-revise <id> <text>
```

The exact CLI syntax may use JSON input to avoid shell quoting ambiguity. Reads
operate only on the immutable job snapshot. Proposal operations write only to the
job-scoped proposal area and require the per-job capability token supplied through
the runtime environment.

The helper never opens the production SQLite database. The hardened Agent sandbox
receives the snapshot read-only and a writable job-local proposal spool containing
no durable memory. It receives no memory database path, bridge configuration, or
reusable credential.

## Search Behavior

The Agent may issue multiple queries during one run. Search combines:

- FTS keyword matching;
- normalized exact phrases;
- update recency;
- effective confidence;
- optional generic provenance and time filters.

The Agent chooses query text. The bridge does not predefine concepts such as
`course`, `chapter`, `weather`, or `progress`.

Search results include the memory ID, note text, update time, provenance label,
and confidence/status needed to judge reliability. All returned content is marked
as untrusted memory, not as instructions.

The snapshot is stable for one job. Concurrent memory changes become visible to
the next job, avoiding nondeterministic mid-run reads.

## Write and Commit Flow

1. The bridge creates the scoped memory session and invokes the Agent.
2. The Agent searches memory as needed and records zero or more proposals.
3. The bridge parses proposals using strict JSON, rejects duplicate keys, and
   applies count and size limits.
4. The bridge verifies the token, job identity, scope, target record, expected
   revision, provenance, and subject authorization.
5. The job completes its normal text and resource validation.
6. The bridge sends text and verified resources to QQ.
7. Only after at least one user-visible result is delivered and every required
   delivery operation succeeds does one SQLite transaction commit the accepted
   memory proposals.
8. On failure, cancellation, timeout, bridge restart, resource-delivery failure,
   or stale revision, no proposal is committed.

Multiple identical submissions from the same job are idempotent. A revision uses
optimistic concurrency: if the target changed after snapshot creation, the
proposal is rejected as stale and the existing memory is retained.

A partially delivered multi-message response is not considered a successful
memory commit. This may cause a later retry to repeat user-visible material, but it
cannot falsely advance durable work state.

## Validation Rules

- New work notes are stored under the current authorized scope only.
- The Agent may revise only Agent-authored work notes visible in its snapshot.
- The Agent cannot revise, forget, contradict, or reinforce curator-authored user
  or group memories.
- The Agent cannot activate a candidate or change sensitivity, subject, scope,
  profile, permission, schedule rule, or runtime configuration.
- Secret-like content and instructions to expose hidden prompts, credentials,
  internal paths, or cross-scope data are rejected.
- Note and proposal counts are bounded per job; note lengths and total stored size
  use configuration limits.
- Retrieved memory is always framed as untrusted data. Instructions found inside a
  memory note must not override the current user request or runtime rules.
- Scheduled execution may update work notes even when no user is online, but only
  if the original schedule remains authorized at execution time.

Rejected proposals do not fail an otherwise successful user task. They are logged
by reason class without note text, QQ IDs, prompts, or secrets.

## Interaction with Review and Decay

Agent work notes bypass conversational extractiveness because plans and summaries
are often derived rather than quoted. They therefore use the dedicated work-note
validator described above instead of pretending to be user evidence.

They remain in the same memory store and retrieval service. Ordinary curator
review must ignore them as source evidence. Decay may lower stale work-note rank,
but notes associated with an active schedule remain eligible until the schedule is
completed, cancelled, or the user explicitly forgets them. This is a generic
liveness rule, not a domain-specific progress field.

Natural-language `/memory` management can list and forget Agent work notes under
the existing scope and authorization rules. The first release does not let the
Agent autonomously delete durable memory.

## Schedule Behavior

Scheduled `task` jobs receive the same memory tools as ordinary `/task` jobs. The
schedule prompt tells the Agent:

- this is a new execution of an existing schedule;
- search relevant prior work before deciding what to do;
- avoid claiming continuity that memory does not support;
- record useful continuation notes only after producing evidence-backed output;
- do not recreate or modify the schedule itself.

The bridge does not force every schedule to write memory. Stateless reminders and
weather checks may write nothing. A continuous teaching or research task can use
free-form notes to maintain its own plan and next step.

This design improves continuity but does not make a mathematical guarantee that a
free-form Agent plan is complete or non-repeating. The bridge guarantees scope,
provenance, revision safety, and delivery-atomic commits; content quality remains
an Agent capability.

## Configuration

The feature is subordinate to existing long-term-memory scope enablement. If the
current scope has memory disabled, no snapshot or proposal capability is provided.

New settings should remain generic and bounded:

```yaml
long_term_memory:
  agent_access:
    enabled: true
    commands: [task]
    scheduled_task_enabled: true
    max_snapshot_items: 200
    max_snapshot_chars: 50000
    max_search_results: 20
    max_proposals_per_job: 8
    max_note_chars: 2000
```

`ask`, proactive chat, schedule parsing, and memory review never receive these
tools. Configuration hot-reloads for future jobs; active jobs retain their
immutable session.

## Observability

Structured logs include:

- job and optional schedule identifiers;
- snapshot item count and character count;
- search count and result counts;
- proposal counts by operation;
- accepted and rejected counts;
- rejection reason classes;
- commit, rollback, stale-revision, and delivery outcome.

Logs do not include memory text, search queries, QQ IDs, tokens, prompts, or local
paths. Agent traces record tool names and result sizes only.

## Testing

### Unit Tests

- Snapshot export enforces exact scope and authorized subjects.
- Candidate, rejected, contradicted, expired, private, and other-group records are
  excluded.
- Query helper searches FTS, exact phrases, recency, and IDs without accessing the
  production database.
- Proposal parsing rejects malformed JSON, duplicate keys, oversized content,
  invalid operations, forged tokens, stale revisions, and unauthorized targets.
- Agent work notes cannot support curator-authored personal facts.
- Commit is idempotent and atomic.

### App Integration Tests

- `/task` can search a prior authorized work note and propose a revision.
- A scheduled task sees notes from its prior successful run.
- Text delivery, file delivery, cancellation, timeout, and send failure exercise
  commit versus rollback.
- Concurrent jobs cannot overwrite one another silently.
- Memory-disabled scopes receive neither snapshots nor proposal capability.
- Existing ask/chat latency and automatic retrieval behavior remain unchanged.

### Real-Agent Capability Test

Using an isolated memory SQLite database and no NapCat dependency:

1. Seed an enabled group with a free-form work note.
2. Run a real `/task` Agent and require it to find and use that note.
3. Have the Agent propose a continuation note.
4. Pass through the real App delivery boundary using a recording adapter.
5. Start a second real scheduled task and verify that it retrieves the committed
   continuation without receiving it in the initial prompt.

The same test is repeated with a forced delivery failure and must prove that the
proposal was not committed. Provider contract tests cover Cursor, Codex, Claude
Code, and custom CLI argument/rendering behavior; one configured provider performs
the expensive semantic capability run.

### Adversarial Tests

- A memory note containing prompt injection cannot widen scope or invoke writes.
- An Agent cannot read a non-mentioned group participant's personal memory.
- Forged schedule IDs, memory IDs, tokens, revisions, and proposal files fail
  closed.
- Symlinks, hard links, path traversal, snapshot replacement, and proposal-area
  replacement are rejected using the existing outbox-style inode and containment
  checks.
- A malicious custom Agent CLI cannot open the production memory database.
- A successful Agent response followed by failed QQ delivery leaves memory
  unchanged.

## Migration and Rollout

No existing memory content changes semantics. The schema migration adds only the
generic provenance and audit data needed for Agent work notes and idempotent job
commits. Existing active memories remain readable through automatic retrieval.

Rollout order:

1. Enable snapshot search for owner `/task` jobs in selected scopes.
2. Enable deferred add/revise proposals after rollback tests pass.
3. Enable scheduled `task` jobs.
4. Enable non-owner `/task` jobs under existing command permissions and safety
   policy.

A single configuration switch disables Agent access without disabling normal
long-term memory.

## Success Criteria

- An advanced Agent can choose and refine memory searches during one job.
- A successful scheduled task can leave free-form continuation notes that its next
  run retrieves.
- No domain-specific course, weather, writing, or monitoring schema exists.
- No Agent subprocess can access the production memory database or another scope.
- Failed delivery never advances durable work memory.
- Agent-authored notes cannot become evidence for personal claims.
- Chat and `/ask` keep their existing automatic retrieval path and latency.
- Cursor, Codex, Claude Code, and custom Agent configurations use the same memory
  protocol.
