# QQ Bot Long-Task Progress Design

## Scope

Build long-task progress messaging for existing `/task` and `/code` jobs. This spec covers multi-message task progress only. It does not implement autonomous group chatting, scheduled reminders, or proactive conversation joining.

The feature has two progress sources:

- Agent progress: Cursor prints explicit progress directives that the bridge sends to QQ immediately.
- Bridge heartbeat: the bridge sends low-frequency "still working" messages when a running job has been silent too long.

The existing final-result flow remains unchanged: when the job completes, `_reply_when_done()` sends the cleaned final text and any outgoing image/file resources.

## Goals

- Let long tasks send useful intermediate updates before final completion.
- Keep current job authorization, concurrency, `/status`, `/stop`, and outgoing resource behavior.
- Avoid progress spam in group chats.
- Strip progress directives from final replies.
- Stop progress and heartbeat delivery when the job is done, cancelled, timed out, or failed.

## Non-Goals

- Do not make the bot speak without a user-triggered job.
- Do not implement a scheduler, reminder system, or group activity observer.
- Do not require the bridge to understand task-specific resources such as Bilibili videos.
- Do not trust agent progress as proof of completion. Final answers must still satisfy the existing runtime skill completion rules.

## User Experience

When a user sends a long `/task`, the bridge sends the existing immediate acknowledgement:

```text
收到，我处理一下。
```

During execution, Cursor may emit:

```text
QQBOT_PROGRESS: 已解析链接，正在检查视频元数据。
QQBOT_PROGRESS: 已抽取 6 个时间点截图，正在核对画面字幕。
```

The bridge forwards these as QQ messages. If no agent progress arrives for a configured interval, the bridge sends a heartbeat such as:

```text
还在处理，已经跑了 60 秒。
```

When the task finishes, the bridge sends the normal final result. Progress lines are not repeated in the final result.

## Architecture

### Progress Config

Add a `ProgressConfig` section to `BridgeConfig`:

- `enabled: bool = True`
- `first_heartbeat_seconds: int = 30`
- `heartbeat_seconds: int = 45`
- `min_progress_interval_seconds: int = 8`
- `max_progress_messages: int = 8`
- `max_progress_chars: int = 240`

Example YAML:

```yaml
progress:
  enabled: true
  first_heartbeat_seconds: 30
  heartbeat_seconds: 45
  min_progress_interval_seconds: 8
  max_progress_messages: 8
  max_progress_chars: 240
```

### Progress Reporter

Add a small `ProgressReporter` component owned by `App` per job. It is responsible for:

- sending agent progress messages,
- rate-limiting progress,
- tracking the last sent progress timestamp,
- running the heartbeat loop,
- stopping cleanly when the job reaches a terminal state.

It should not know how Cursor works. It only receives progress text and observes the `Job` state/task.

`App` should keep reporters in a dictionary keyed by job id. The runner must be able to find the reporter for the job whose Cursor process is running. To avoid relying on `ChatEvent.id` as a proxy for job identity, the job runner interface should receive the `Job` or job id instead of only `(cmd, args, event)`.

### Cursor Streaming

Extend `CursorAdapter.run()` with an optional progress callback:

```python
ProgressCallback = Callable[[str], Awaitable[None]]
```

When a callback is provided, the adapter should prefer Cursor's stream-friendly output:

- invoke Cursor with `--output-format stream-json` when compatible with the current command,
- parse text deltas or message events as they arrive,
- extract complete lines beginning with `QQBOT_PROGRESS:`,
- call the callback with the directive payload,
- keep non-progress text for the final result.

If streaming cannot be used or parsing fails, the adapter should fall back to current captured-output behavior. Heartbeats still protect the user experience in that fallback.

### Directive Syntax

Only this exact prefix is recognized:

```text
QQBOT_PROGRESS:
```

Parsing rules:

- A directive consumes the whole line.
- Payload is stripped, capped to `max_progress_chars`, and ignored if empty.
- Directives are removed from final output.
- Directives in code blocks are still treated as directives. The runtime skill should tell Cursor not to print examples unless asked, but the bridge treats the prefix as operational.

### Job Flow

For a non-approval job:

1. `Policy.start_job()` creates the existing `Job` in `queued` state but does not start its task yet.
2. `App._configure_outgoing_resources()` runs as today.
3. `App` creates and stores a `ProgressReporter` for `/task` and `/code` when progress is enabled.
4. `Policy.start_job_task(job)` starts the task after the reporter exists.
5. `App._schedule_reply(job)` starts the reporter heartbeat loop and final reply watcher.
6. `_agent_runner(job)` passes the reporter callback into `CursorAdapter.run()` for `/task` and `/code`.
7. The adapter streams agent progress directives to the reporter.
8. `_reply_when_done()` awaits the job task, sends final text/resources, removes the reporter, and stops reporter state through terminal job state/task completion.

For an approval job:

1. `Policy.start_job()` creates a waiting job.
2. No reporter starts while waiting for approval.
3. After `/approve`, `App` creates the reporter first.
4. `Policy.start_job_task(job)` starts the approved task.
5. `App._schedule_reply(job)` starts heartbeat and final reply watcher.

For `/ask`, `/plan`, and `/search`:

- no agent progress directives are requested,
- no heartbeat loop is required for `/ask`,
- `/plan` can stay single-shot unless later expanded.

## Prompt And Skill Contract

Update the task/code runtime skill to tell Cursor:

- use `QQBOT_PROGRESS: <short update>` for meaningful milestones in long tasks,
- report only steps that actually happened,
- do not emit progress for every tiny action,
- keep progress short and user-facing,
- do not include hidden paths, tokens, or internal implementation details,
- final answers should not restate every progress update.

The bridge should also include the progress directive in outgoing resource context or task prompt so Cursor sees the exact syntax.

## Error Handling

- If progress send fails, log it and continue the job.
- If heartbeat send fails, log it and continue the job.
- If Cursor streaming parse fails, fall back to captured output and rely on heartbeat.
- If the job is cancelled with `/stop`, the reporter stops and sends no more progress.
- If the job times out, existing timeout result is sent once. Heartbeat stops.
- If agent prints too many progress messages, excess messages are ignored.
- If agent prints the same progress repeatedly, rate limiting prevents spam.

## Security And Safety

- Progress text is redacted with the existing redactor before sending.
- Progress is capped in length.
- Progress directives never create outgoing resources.
- Progress directives do not change job state and do not count as completion evidence.
- The bridge does not evaluate URLs, files, or task semantics for progress.

## Testing

Unit tests:

- parse and strip `QQBOT_PROGRESS:` lines from final Cursor output,
- stream parser handles split/chunked directive lines,
- rate limiter suppresses rapid progress messages,
- max progress message count is enforced,
- heartbeat sends after the first silent interval,
- heartbeat resets after agent progress,
- cancellation stops heartbeat and progress delivery,
- `/task` passes a progress callback to Cursor,
- `/ask` does not start progress reporting.

Integration-style async tests:

- a fake Cursor emits two progress directives and a final answer; adapter sends three QQ messages in order,
- a silent long task triggers heartbeat before final answer,
- `/stop` cancels a running long task without later heartbeat.

Regression tests:

- final outgoing file directives still work,
- `QQBOT_PROGRESS:` lines are not included in final replies,
- existing `/status` running/queued behavior is preserved.

## Rollout

Keep progress enabled by default in `config.example.yaml` and `config.test.yaml`. Existing deployments can disable it with:

```yaml
progress:
  enabled: false
```

The first implementation should keep heartbeat text simple and deterministic. More personalized progress messages can be added later after the mechanics are stable.
