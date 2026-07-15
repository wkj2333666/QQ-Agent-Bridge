# Task Voice 2 Report: Isolated Whisper Runner

## Scope

Implemented the isolated whisper.cpp subprocess runner in
`src/qq_agent_bridge/whisper_runner.py` and its focused test suite in
`tests/test_whisper_runner.py`.

## RED Evidence

After creating the test module and before creating the runner module, ran:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_whisper_runner.py
```

Result: exit code 2 during collection, with the expected error:

```text
ModuleNotFoundError: No module named 'qq_agent_bridge.whisper_runner'
```

## GREEN Evidence

After implementation, ran the focused command:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_whisper_runner.py
```

Result: `6 passed in 0.47s`.

Ran the complete project suite afterward:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q
```

Result: `400 passed, 11 skipped in 6.98s`.

Also ran `git diff --check`; it exited successfully with no output.

## Files

- `src/qq_agent_bridge/whisper_runner.py`
  - Defines `TranscriptionResult` and `WhisperRunner`.
  - Validates binary, model, and audio files before execution.
  - Runs whisper.cpp only through `asyncio.create_subprocess_exec` and
    `communicate()` under an `asyncio.Semaphore`.
  - Kills and awaits timed-out processes, parses only the requested `.txt`
    output file, and never derives a transcript from stdout or failure output.
  - Stores only successful nonempty results in an atomic JSON cache keyed by
    audio SHA-256, model path/mtime/size, language, and runner version.

- `tests/test_whisper_runner.py`
  - Uses temporary executable fixtures, not whisper model files.
  - Covers generated text-file reading, nonzero failure behavior, timeout,
    concurrency limit, unavailable input, and successful cache reuse.

## Concerns

- No real whisper.cpp binary or model was exercised, by task requirement. The
  fake executables validate the argv/output-file contract only.
- Audio hashing and cache-file parsing are synchronous filesystem operations;
  they are appropriate for the scoped runner but may merit offloading if very
  large audio inputs become common.

## Commit

`2350327 feat: add bounded whisper subprocess runner`

## Reviewer Fix

The Task 2 review identified that `WhisperRunner` trusted values supplied by
callers who constructed `WhisperConfig` directly. `BridgeConfig.load()` already
normalizes these fields, but direct inputs could create a semaphore with an
unbounded concurrency value and pass a non-finite timeout to
`asyncio.wait_for`.

Updated `WhisperRunner.__init__` to apply the same defensive normalization as
`config.py`:

- finite `timeout_seconds` is clamped to `1.0..3600.0`; non-finite values use
  the `WhisperConfig` default of `90.0` seconds;
- finite `max_concurrent` is converted to an integer and clamped to `1..4`;
  non-finite values use the `WhisperConfig` default of `1`;
- the normalized values are written back to the supplied config, so both the
  semaphore and subprocess timeout use the bounded settings.

Added focused regression tests for direct `WhisperConfig(max_concurrent=999999)`
and `WhisperConfig(timeout_seconds=float("inf"))` inputs. Existing runner
behavior and all prior tests remain unchanged.

## Reviewer Fix Evidence

Focused runner tests:

```text
8 passed in 1.42s
```

Full suite:

```text
402 passed, 11 skipped in 8.05s
```

The worktree contained a pre-existing untracked `.superpowers/` directory;
unrelated files were not reverted.

## Task 2 Review Fix

Updated `tests/test_whisper_runner.py` so the timeout regression uses the
runner's valid minimum timeout of `1.0` second while the fake subprocess sleeps
for `2.0` seconds. This makes the test deterministically exercise timeout
cleanup without changing production behavior or unrelated tests.

Verification:

```text
`/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_whisper_runner.py`
```
