# Long-Term Memory Curation Usability Design

## Status

Proposed. This design supersedes the source-consumption and recurring-topic
parts of `2026-08-01-memory-recurring-topic-evidence-design.md`. Its sandbox
hardening rules remain unchanged.

## Problem

The current curator can collect and validate messages, but its steady-state
behavior is not useful:

- repeated questions and task requests are forced into `candidate` memories;
- candidates are excluded from retrieval, so those observations never improve
  a later reply;
- a valid `{"operations":[]}` response is treated as a failed review;
- only sources cited by accepted proposals are consumed;
- semantic rejections and irrelevant sources are therefore retried repeatedly;
- the exact-common-substring rule encourages tiny recurring-topic fragments;
- candidate memories neither decay nor expire automatically;
- a generic operation schema encourages invalid field combinations; and
- review-run `source_count` describes consumed sources on success but input
  sources on failure, making operational statistics ambiguous.

The system needs to remember useful conversational context without turning
inferences into asserted personal facts.

## Goals

1. Explicit, well-supported user facts remain durable and directly useful.
2. Repeated conversational interests can help later replies without being
   represented as facts or preferences.
3. Ambiguous personal claims remain invisible until explicitly confirmed.
4. A successfully inspected batch reaches a terminal state even when it
   contains no durable memory.
5. Mechanical failures retry with a finite policy and remain diagnosable.
6. Memory retrieval exposes provenance and trust level to the answering agent.
7. Existing low-quality candidates are migrated without silently promoting
   them.

## Non-Goals

- Building a deterministic Chinese topic-extraction engine.
- Treating every repeated word as a durable topic.
- Inferring sensitive attributes, relationships, identity, or preferences from
  questions alone.
- Automatically converting an observed topic into a personal fact.
- Sharing memory across QQ scopes.

## Memory Trust Tiers

### Active

`active` represents a durable fact supported by an allowed evidence class, for
example an explicit self-statement, an explicit memory request, or an
owner-confirmed group norm.

Active memories participate in normal retrieval. They may be revised,
contradicted, decayed, or made dormant according to the existing rules.

### Observed

`observed` represents a non-sensitive conversational pattern, such as a topic
that a user has discussed repeatedly. It is not a statement that the user
likes, believes, owns, or identifies with the topic.

Observed memories:

- are limited to the `recurring_topic` category;
- require evidence from at least two distinct messages;
- participate in retrieval at a lower rank than active memories;
- are presented to the answering agent with an explicit uncertainty label;
- gain support without becoming active;
- decay faster than active memories; and
- expire after a bounded period without new support.

### Candidate

`candidate` represents an ambiguous personal fact or a proposed correction that
requires user confirmation. Candidates do not participate in answer retrieval.
They may be confirmed through the existing command flow or expire after a
bounded retention period.

Recurring topics must not use `candidate`. This keeps "safe to use as a weak
observation" separate from "unsafe to use until confirmed".

## Curator Operation Contract

The curator returns a versioned object:

```json
{
  "schema_version": 2,
  "operations": []
}
```

Each operation has a dedicated shape. Fields that do not apply to an operation
are forbidden rather than accepted as nullable placeholders.

### `add_fact`

Creates an active memory from direct evidence. It includes subject, category,
normalized content, confidence, sensitivity, source kind, source evidence, and
optional expiry metadata.

### `observe_topic`

Creates an observed recurring topic. It includes subject, normalized topic,
confidence, and evidence from at least two sources.

### `support_observation`

Adds support to an existing observed topic. It includes only the target item,
new source evidence, and confidence. It never changes the item to active.

### `revise_fact`

Replaces the content of an existing active memory using direct newer evidence.

### `reinforce_fact`

Adds direct support to an existing active or dormant memory. It may reactivate a
dormant fact under the existing confidence rules.

### `contradict_fact`

Marks an existing active, dormant, or candidate fact as contradicted using
direct contradictory evidence.

### `mark_candidate`

Creates an ambiguous personal-fact candidate. It cannot use the
`recurring_topic` category.

The parser may temporarily accept the legacy schema during migration, but all
new prompts and tests use schema version 2. Legacy recurring-topic candidates
are not generated after the migration.

## Evidence Model

Normalized memory content does not need to occur verbatim in every source.
Instead, each cited source carries its own exact evidence span:

```json
{
  "source_id": 12,
  "evidence_span": "exact text copied from that source"
}
```

The deterministic validator verifies that:

- the source exists in the current review batch;
- the evidence span is a non-empty substring of that source;
- the source belongs to the proposal's scope and subject;
- the source is eligible for the requested operation;
- sensitive or secret material is not inferred into an observation; and
- topic evidence comes from at least two distinct messages.

Semantic equivalence between evidence spans and a normalized observed topic is
trusted only for the low-trust `observed` tier. Active facts retain stricter
extractive and source-kind requirements.

Topic quality checks reject empty, punctuation-only, identifier-like, and
single-character topics. The validator also enforces a bounded topic length.
This is a quality floor, not a second topic-extraction engine.

## Review Batch Lifecycle

The curator must inspect the complete batch. A syntactically valid schema
version 2 response is a semantic completion, including an empty operation list.

After a valid response:

- all input sources are consumed atomically;
- accepted operations are committed in the same transaction;
- rejected operations and their reason classes are recorded;
- an empty response is recorded as `no_memory`, not an error; and
- rejected semantic proposals do not cause the source batch to loop forever.

If the curator cannot inspect a source, it must return that source ID in an
explicit `deferred_source_ids` field. Only those sources remain pending. Unknown
or duplicated IDs invalidate the response.

Mechanical failures include process errors, timeouts, malformed JSON, unknown
schema versions, and invalid top-level response shape. They use exponential
backoff up to the configured maximum attempt count. After that limit, sources
enter `quarantined` state and are excluded from periodic review. An explicit
`/memory review now` may retry quarantined sources.

Raw-source TTL cleanup still applies to pending and quarantined sources.

## Retrieval Contract

Retrieval considers `active`, `dormant`, and `observed` items:

- active facts keep the existing ranking behavior;
- dormant facts remain subject to the existing lower-score behavior;
- observed topics receive a configurable score multiplier below active facts;
- candidate, contradicted, rejected, and expired items are excluded.

The rendered prompt groups observations separately:

```text
Recent observed topics (weak context, not user facts or preferences):
- The user has recently discussed <topic> repeatedly.
```

The answering agent is instructed to use observations only to improve
continuity or choose relevant examples. It must not say that the user likes,
believes, owns, or identifies with an observed topic.

## Decay and Retention

- Active memories keep their existing category-specific decay behavior.
- Observed topics have a shorter configurable half-life and default retention.
- Supporting evidence increments `source_count`, refreshes
  `last_supported_at`, and raises score within the observed tier.
- Observed topics never become active through support alone.
- Candidates expire after a configurable retention period unless explicitly
  confirmed or rejected.
- Expired observed and candidate items become `rejected` with an auditable
  revision entry rather than being silently promoted or reused.

## Schema and Observability

The schema migration adds:

- `observed` to the allowed memory statuses;
- `review_state = quarantined` behavior for source rows;
- `input_count`, `consumed_count`, `deferred_count`, `observed_count`,
  `ignored_count`, and `no_memory_count` to review-run accounting; and
- optional rejection-reason summaries without source text or identifiers.

Existing `source_count` remains for compatibility but is defined as input count
after migration. New code reads the explicit counters.

User-facing review summaries report inspected, active, observed, candidate,
ignored, deferred, and failed counts. They do not claim that an empty review
failed.

## Existing Data Migration

Migration is conservative:

1. Back up the SQLite database before applying the schema migration.
2. Preserve active, dormant, contradicted, and explicitly confirmed items.
3. Mark inferred `recurring_topic` candidates from the legacy policy as
   rejected with a migration revision entry.
4. Do not promote any legacy candidate to observed or active.
5. Let subsequent source collection rebuild useful observations under the new
   evidence contract.

The migration is transactional and idempotent. It does not modify memory scopes
or profile configuration.

## Security and Privacy

- Scope and subject isolation remain deterministic.
- Third-party personal claims cannot become active or observed memories.
- Sensitive and secret inferences cannot become observed topics.
- Evidence excerpts stay in the local database and are never written to normal
  logs.
- Tests and documentation use synthetic identities and conversations.
- Agent output remains untrusted until parsed and validated.

## Testing Strategy

### Unit Tests

- operation-specific parser acceptance and rejection;
- exact per-source evidence-span validation;
- normalized recurring-topic creation from varied source wording;
- rejection of one-message, one-character, sensitive, cross-subject, and
  third-party observations;
- empty valid review consumes the batch;
- semantic rejection consumes the batch and records reasons;
- malformed output retries and eventually quarantines;
- observation support never promotes to active;
- observation and candidate expiry;
- trust-aware retrieval and prompt formatting;
- schema migration and legacy-candidate archival.

### Integration Tests

- collection to review to atomic commit with an isolated SQLite database;
- `/memory review now` reports completion for a no-memory batch;
- repeated differently worded messages produce one retrievable observation;
- direct self-statements still produce active memories;
- pending, deferred, and quarantined source behavior survives restart.

### Real-Agent Capability Test

An opt-in test invokes the configured agent against synthetic QQ-style input and
checks the complete contract:

- direct self-statement becomes an active fact;
- repeated questions become one observed topic;
- unrelated chat produces no memory and is consumed;
- no unsupported personal claim is emitted; and
- the resulting retrieval prompt labels the observation as weak context.

The test uses an isolated database and workspace and never reads production
memory.

## Rollout

1. Add failing tests for lifecycle, operation schema, and retrieval behavior.
2. Add the schema migration and model types.
3. Implement the versioned parser and validator.
4. Change batch commit and bounded retry behavior.
5. Add observation retrieval, decay, and presentation.
6. Run unit, integration, and opt-in real-Agent tests.
7. Back up the production database and run a dry-run migration report.
8. Apply the migration and monitor review outcomes before enabling automatic
   periodic review again.

## Success Criteria

- A valid empty review completes and removes its inspected sources.
- Periodic review never retries a mechanically failed source beyond the
  configured limit.
- Repeated semantically related questions can create one useful observed topic.
- Observed topics improve retrieval without being stated as user facts.
- Candidates are reserved for ambiguous personal facts and do not accumulate
  indefinitely.
- Review statistics distinguish inputs, consumed sources, deferred sources,
  semantic rejections, and mechanical failures.
- Existing inferred recurring-topic candidates are never silently promoted.
- The isolated end-to-end and real-Agent capability tests pass.
