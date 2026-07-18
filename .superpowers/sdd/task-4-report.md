# Task 4 Report: Transactional App Delivery, Repair Prompt, and Verified Memory

## Status

Complete.

Commit: `53de3ed Make artifact delivery transactional`

## Implementation Summary

- Integrated `resolve_artifacts` and `inspect_outgoing_resources` into `App._reply_when_done_inner`.
- Added one bounded artifact repair callback using the existing agent runtime, task model, workspace, progress callback, outbox, and token.
- Changed resource delivery to await every image, voice, or file before releasing agent-authored final text.
- Converted unverified artifacts and OneBot failures to deterministic bridge-owned outcomes.
- Stored transactional conversation memory only after the final verified outcome was successfully sent.
- Preserved the existing text-only path, chunk delays, group `reply_ats`, proactive text-send accounting, resource dispatch methods, and resource echo IDs.

## RED Evidence

First production glued-line ordering regression:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_app_async.py::test_malformed_file_directive_sends_real_file_before_success_text -q
1 failed in 3.17s
AssertionError: assert 'text' == 'file'
```

The first non-progress delivery event was text, reproducing the existing text-before-file behavior.

All four required transaction regressions before production edits:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_app_async.py -k "malformed_file_directive or missing_artifact or adapter_file_failure or delivery_failure_memory" -q
4 failed, 95 deselected in 6.11s
```

Observed failures:

- malformed directive: first delivery event was text instead of file;
- missing artifact: `agent_calls` was 1 instead of 2;
- adapter failure: no deterministic `发送到 QQ 失败` message was sent and agent success prose had already been sent;
- memory failure: no `本次未确认交付` outcome was stored, while agent success prose had been stored.

The brief's literal `-k "artifact and (malformed or repair or failure or memory)"` selection was also run. Because three prescribed test names do not contain the word `artifact`, it selected only the repair test and failed with `agent_calls == 1` (`1 failed, 98 deselected in 1.72s`).

## GREEN Evidence

Required transaction regressions after implementation:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_app_async.py -k "malformed_file_directive or missing_artifact or adapter_file_failure or delivery_failure_memory" -q
4 passed, 95 deselected in 0.39s
```

Additional repair budget, cancellation, privacy, and partial-delivery checks:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_app_async.py -k "artifact_repair or partial_adapter_failure or adapter_file_failure" -q
4 passed, 98 deselected in 0.57s
```

Focused App and resource suites:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_app_async.py tests/test_artifact_delivery.py tests/test_outgoing_resources.py -q
141 passed in 4.06s
```

The first focused run exposed one legacy assertion expecting parser warning prose for an outside-workspace path. Task 4 requires deterministic unverified copy, so the test was updated to assert exactly `文件没有成功生成或无法验证，本次未发送。`; resource non-dispatch assertions remained unchanged.

## Full-Suite Verification

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest
556 passed, 12 skipped in 14.94s
```

```text
git diff --check
exit 0
```

The 12 skips are the repository's configured capability/live-agent and CLI smoke skips.

## Changed Files

- `src/qq_agent_bridge/main.py`
- `tests/test_app_async.py`

No coordinator or parser files were modified.

## Self-Review

- Repair is attempted only through `resolve_artifacts`, which makes at most one callback invocation.
- Repair timeout is `min(90s, parent runtime remaining)` and returns without running when no time remains.
- Repair uses `run_agent(self.agent, ...)`, task mode/model selection, the current workspace, progress callback, trace ID, and the same outbox/token context.
- `asyncio.CancelledError` is not caught by repair or per-resource ordinary exception handling and therefore propagates.
- Resources are awaited in selected order before any final agent-authored text is sent.
- Total and partial adapter failures suppress agent success prose and send only deterministic bridge outcomes.
- Failure logs contain job ID, resource index/kind, and exception class only; user failure text contains neither token nor internal path, with explicit regression assertions.
- Transactional memory is appended after delivery and contains only cleaned verified prose or deterministic bridge failure copy.
- Text-only behavior still follows the original memory, guard, chunk, delay, group mention, and proactive accounting path.
- Image, voice, and file dispatch plus existing echo IDs remain covered by the focused suite.
- Diff review confirmed changes are limited to the two owned files.

## Concerns

No blocking concerns. The full suite retains 12 intentionally configured skips; no live external agent or OneBot service was exercised.

---

## Cross-Layer Review Fix

### Status

Complete.

Commit subject: `Fix artifact delivery review findings`

### Fix Summary

- Required a repair inspection to add a newly validated resource before an initially partial artifact result can become verified.
- Preserved direct `attempted == 0` text-only success and rejected repairs that merge zero resources.
- Made repair remaining time use `job.timeout_seconds` when present, otherwise `cfg.effective_max_runtime()`, capped at 90 seconds.
- Deduplicated same-kind, same-source directives before max-item accounting and stable copying while retaining attempted count and first-seen order.
- Changed normal OneBot text, image, voice, and file sends to await their matching response through the existing echo dispatcher.
- Made no-gateway, transport, timeout, non-`ok` status, and nonzero-retcode send outcomes raise to App; the internal send timeout is the monkeypatchable `OneBotAdapter.SEND_ACTION_TIMEOUT_SECONDS` constant.
- Removed exception text from OneBot transport logs, re-raised transport errors, and changed App final delivery failures to class-only logging.
- Redacted bare tokens following all `QQBOT_SEND_IMAGE`, `QQBOT_SEND_FILE`, `QQBOT_SEND_VOICE`, and `QQBOT_SEND_AUDIO` directives.
- Strengthened the multiple-candidate and nested-file discovery tests with missing paths inside the active outbox.
- Left the existing model usage-limit fallback behavior unchanged.

### RED Evidence

Artifact verification, directive deduplication, and parent timeout regressions before production edits:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_artifact_delivery.py::test_resolution_does_not_verify_empty_repair_from_initial_resources tests/test_artifact_delivery.py::test_resolution_keeps_attempted_zero_text_only_output_verified tests/test_artifact_delivery.py::test_resolution_does_not_verify_repair_with_zero_merged_resources tests/test_outgoing_resources.py::test_duplicate_directives_stage_and_count_resource_once tests/test_outgoing_resources.py::test_does_not_guess_when_multiple_top_level_files_exist tests/test_outgoing_resources.py::test_unique_discovery_ignores_nested_temporary_files tests/test_app_async.py::test_artifact_repair_uses_job_specific_remaining_budget -q
3 failed, 4 passed in 0.87s
```

Observed failures were `verified=True` after an empty repair, a duplicate directive consuming the item limit, and a 90-second repair timeout instead of the job-specific 7 seconds remaining. The attempted-zero, zero-merged-resource, and strict missing-discovery guards passed.

OneBot delivery truth and secret-safe logging regressions before production edits:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_onebot.py::test_send_waits_for_matching_success_ack tests/test_onebot.py::test_send_fails_without_gateway tests/test_onebot.py::test_send_reraises_transport_error_without_logging_exception_text tests/test_onebot.py::test_send_fails_on_ack_timeout tests/test_onebot.py::test_send_fails_on_non_ok_status tests/test_onebot.py::test_send_fails_on_nonzero_retcode tests/test_onebot.py::test_send_image_uses_onebot_image_segment tests/test_onebot.py::test_send_voice_uses_onebot_record_segment tests/test_onebot.py::test_send_file_uses_upload_action tests/test_app_async.py::test_reply_delivery_failure_log_uses_exception_class_only tests/test_redact.py::test_redact_bare_outgoing_directive_tokens -q
11 failed in 1.42s
```

The failures showed immediate pre-ack returns, silent gateway/transport/timeout/status/retcode failures, exception text in transport and App logs, and unredacted directive tokens.

### GREEN Evidence

Artifact review regressions after implementation:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_artifact_delivery.py::test_resolution_does_not_verify_empty_repair_from_initial_resources tests/test_artifact_delivery.py::test_resolution_keeps_attempted_zero_text_only_output_verified tests/test_artifact_delivery.py::test_resolution_does_not_verify_repair_with_zero_merged_resources tests/test_outgoing_resources.py::test_duplicate_directives_stage_and_count_resource_once tests/test_outgoing_resources.py::test_does_not_guess_when_multiple_top_level_files_exist tests/test_outgoing_resources.py::test_unique_discovery_ignores_nested_temporary_files tests/test_app_async.py::test_artifact_repair_uses_job_specific_remaining_budget -q
7 passed in 0.35s
```

OneBot and secret-safety review regressions after implementation:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_onebot.py::test_send_waits_for_matching_success_ack tests/test_onebot.py::test_send_fails_without_gateway tests/test_onebot.py::test_send_reraises_transport_error_without_logging_exception_text tests/test_onebot.py::test_send_fails_on_ack_timeout tests/test_onebot.py::test_send_fails_on_non_ok_status tests/test_onebot.py::test_send_fails_on_nonzero_retcode tests/test_onebot.py::test_send_image_uses_onebot_image_segment tests/test_onebot.py::test_send_voice_uses_onebot_record_segment tests/test_onebot.py::test_send_file_uses_upload_action tests/test_app_async.py::test_reply_delivery_failure_log_uses_exception_class_only tests/test_redact.py::test_redact_bare_outgoing_directive_tokens -q
11 passed in 0.34s
```

All owned focused suites:

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest tests/test_artifact_delivery.py tests/test_outgoing_resources.py tests/test_app_async.py tests/test_onebot.py tests/test_redact.py -q
192 passed in 3.95s
```

### Full-Suite Verification

```text
env UV_PROJECT_ENVIRONMENT=/home/wkj/projects/qq-bot/.venv uv run pytest
569 passed, 12 skipped in 12.29s
```

```text
git diff --check
exit 0
```

The 12 skips remain the repository's configured capability/live-agent and CLI smoke skips.

### Files Changed

- `.superpowers/sdd/task-4-report.md`
- `src/qq_agent_bridge/artifact_delivery.py`
- `src/qq_agent_bridge/main.py`
- `src/qq_agent_bridge/onebot.py`
- `src/qq_agent_bridge/outgoing_resources.py`
- `src/qq_agent_bridge/redactor.py`
- `tests/test_app_async.py`
- `tests/test_artifact_delivery.py`
- `tests/test_onebot.py`
- `tests/test_outgoing_resources.py`
- `tests/test_redact.py`

### Concerns

No blocking concerns. OneBot acknowledgements were exercised through the production response dispatcher with fake connections; no live gateway or external agent was used. The full suite retains 12 intentionally configured skips.
