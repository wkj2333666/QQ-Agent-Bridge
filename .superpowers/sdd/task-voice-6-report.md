# Task Voice 6 Report

## Scope

Added a deterministic end-to-end voice-transcription regression test in
`tests/test_agent_e2e.py`. The test uses no network, QQ account, or Whisper
model.

## Coverage

- Normalizes a private OneBot message containing a `record` segment.
- Uses a fake OneBot `get_record` response to resolve a WAV URL.
- Stages fake WAV bytes through the real resource manager.
- Runs a fake Whisper executable through the real `WhisperRunner`.
- Verifies the successful transcript is injected once and exactly as
  `我是测试语音`.
- Verifies a nonzero Whisper exit contributes an unavailable marker rather
  than a verified or guessed transcript.
- Verifies exactly one agent invocation in `task` mode for each case.

## Production Changes

None. The existing App, resource-manager, OneBot resolver, Whisper runner, and
prompting wiring passed this integration test unchanged.

## Verification

```text
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_agent_e2e.py -k voice_transcription
2 passed, 9 deselected in 0.77s

/home/wkj/projects/qq-bot/.venv/bin/pytest -q
421 passed, 11 skipped in 8.67s
```

## Note

The initial test wait condition observed the immediate `/task` acknowledgement
instead of agent invocation, which cancelled the background task during fake
Whisper startup. The final test waits for the captured agent prompt, and no
production defect was involved.

## Task 6 Review Fix

The review found that the voice fixture called `_make_cfg`, which could skip
because the real-agent prerequisites (`bwrap` or a configured custom-cli
command) were absent. It also accepted a verified transcript by substring and
allowed extra transcript-context lines in the failure case.

The fixture now uses a local runtime-free `BridgeConfig` and the existing
`FakeAgent` adapter directly, so only the fake OneBot resolver, fake WAV
fetcher, and fake Whisper executable participate. The success assertion
extracts `transcript` context lines and compares the complete verified line
exactly. The failure assertion compares the extracted lines exactly to the
single unavailable marker, rejecting any verified or guessed transcript.

## Review Fix Verification

```text
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_agent_e2e.py -k voice_transcription
2 passed, 9 deselected in 0.77s

/home/wkj/projects/qq-bot/.venv/bin/pytest -q
421 passed, 11 skipped in 8.69s
```
