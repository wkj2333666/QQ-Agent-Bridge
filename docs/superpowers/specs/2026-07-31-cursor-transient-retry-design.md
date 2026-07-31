# Cursor Transient Retry Design

## Problem

The bridge currently treats every non-zero Cursor Agent exit as terminal unless
an explicitly selected model reports a usage-limit error. Production traces
show that Cursor's `auto` route can intermittently fail after startup with an
empty model registry:

```text
Cannot use this model: auto. Available models:
```

The same account reports `auto` as available immediately afterward, and nearby
jobs succeed. Cursor Agent also intermittently fails before TLS establishment.
These are transient provider failures, but the bridge currently reports
`[error] 助手执行失败` without retrying.

## Chosen Approach

Retry inside `CursorAdapter`, where the raw subprocess error is still
available. This is preferable to switching models alone, which cannot absorb
network failures, and to restarting the bridge, which would disrupt unrelated
jobs.

The production configuration will use `composer-2.5` for task jobs. Existing
usage-limit handling will continue to fall back to `auto`.

## Retry Policy

- Retry only errors that are strongly identified as transient:
  - `Cannot use this model: auto` with an empty `Available models:` response.
  - A client socket disconnect before the secure TLS connection is established.
- Allow at most two retries after the initial attempt.
- Wait with a short increasing delay between attempts.
- Emit one concise progress update before each retry.
- Give every retry a distinct trace suffix so attempts can be diagnosed.
- Do not retry invalid explicit model names, authentication errors, tool
  failures, storage exhaustion, timeouts, cancellations, or arbitrary process
  exits.
- Preserve the existing usage-limit fallback from explicit task models to
  `auto`; transient retry applies independently to the resulting Auto attempt.

## Data Flow

1. Start Cursor Agent with the requested model.
2. On success, return the result unchanged.
3. On non-zero exit, classify the cleaned subprocess output.
4. If it is a usage-limit failure for an explicit model, retry once with Auto
   through the existing fallback.
5. If it is a supported transient provider failure and retry capacity remains,
   report progress, wait, and rerun the same request.
6. Otherwise return the existing user-facing error.

## Testing

- A fake Cursor process fails once with the empty Auto model response and then
  succeeds; the adapter returns the successful result.
- A fake process fails twice with the TLS startup error and then succeeds,
  proving the retry bound and increasing delays.
- A genuinely invalid model response with a non-empty available-model list is
  not retried.
- Existing usage-limit fallback tests remain green.
- The focused adapter tests and then the full test suite must pass.

## Operational Configuration

The local production `config.yaml` changes `agent.task_model` from `auto` to
`composer-2.5`. This private deployment setting is not committed. No service is
restarted automatically; the user can reload or restart the bridge after
verification.
