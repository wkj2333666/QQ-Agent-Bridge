# Agent Trace Logging Design

## Goal

Preserve enough of the CLI Agent's execution trace to explain slow, repeated, failed, or timed-out QQ tasks. The trace must distinguish concurrent jobs and must not turn prompts, tokens, or large tool payloads into an unbounded secret-bearing log.

## Chosen Approach

Use one structured JSONL file per Agent invocation. The bridge receives Cursor's `stream-json` output and writes a sanitized, bounded event record before converting the event into a user-facing progress message. This keeps the diagnostic record close to the subprocess boundary while keeping QQ messages short.

The same writer is used by `CursorAdapter` and `CustomCommandAdapter`. Cursor JSON events are recorded as structured events; non-JSON custom CLI output is recorded as bounded text events. The disabled runtime produces no trace.

## Configuration

Add these fields under `agent`:

- `trace_enabled`: disabled by default in the example configuration.
- `trace_root`: relative paths are resolved from the bridge working directory; absolute paths are allowed only where the existing local deployment policy allows them.
- `trace_max_bytes`: maximum size of one JSONL file; additional events are dropped with one final truncation marker.
- `trace_max_line_chars`: maximum serialized event size before field-level truncation.

The user's local configuration may enable tracing with `runtime/agent-traces`. The repository example remains privacy-safe and disabled by default.

## File Naming and Permissions

Each invocation uses the originating job id when available, otherwise a generated run id. The filename is a safe token such as `<job-id>.jsonl`; unsafe characters are replaced rather than interpreted as paths. The root directory is created with mode `0700`, each trace file with mode `0600`, and writes use append-only JSONL semantics. A trace is never written inside the Agent workspace or bwrap-mounted output directory.

## Event Schema

Every line is a standalone JSON object with a stable minimum schema:

```json
{
  "schema_version": 1,
  "time": "2026-07-16T01:12:52.123Z",
  "job_id": "j...",
  "stream": "stdout",
  "event": "tool_call",
  "subtype": "started",
  "elapsed_ms": 20321,
  "tool": "web_search",
  "description": "获取视频字幕"
}
```

The implementation may include a bounded `payload` summary for debugging. It must not include the original prompt by default. Text, descriptions, stderr, and payload strings are passed through the existing redactor and truncated. Non-serializable values are converted to short type descriptions rather than failing the Agent job.

The writer records at least:

- invocation start, command mode, model, and workspace basename;
- stdout stream events, including tool-call start/completion, assistant messages, and non-JSON lines;
- bounded stderr events;
- process exit code and elapsed duration;
- timeout, cancellation, trace-write failure, and truncation markers.

The command itself is never written verbatim because it can contain the user prompt and sensitive paths. Trace-write failures are logged but never fail or delay the Agent job beyond the bounded write operation.

## Job Association

Extend the internal Agent `run` call with an optional `trace_id`. The normal task path passes `job.id`; proactive, schedule-parser, and direct adapter calls receive a generated run id when tracing is enabled. The trace path is logged once at invocation time using the job id, allowing an operator to locate the raw record without exposing it to QQ users.

## Lifecycle and Limits

The trace writer is opened before the subprocess starts and closed in a `finally` block. It records timeout and cancellation cleanup after the process-group termination attempt. File-size checks happen before each append; once the limit is reached, the writer emits one bounded truncation record when possible and ignores later events. No automatic deletion is added in this change; operators can archive or remove old trace files explicitly.

## Testing

Add tests for:

1. disabled tracing creates no files;
2. one job produces one valid JSONL trace with start, stream, exit, and elapsed records;
3. tool-call and stderr records are redacted and bounded;
4. unsafe job ids cannot escape the trace root;
5. file permissions are `0700`/`0600` where supported;
6. the byte limit emits a truncation marker and remains bounded;
7. timeout and non-JSON custom CLI output still close the trace without changing job results;
8. the existing progress behavior remains unchanged while the raw event trace is retained.

