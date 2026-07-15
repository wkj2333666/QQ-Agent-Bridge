# Task 4 Report: Voice Resource Enrichment and Context Integration

## Scope

Implemented QQ voice-resource enrichment for the agent prompt. The resource layer now asks
NapCat/OneBot for a WAV conversion when a voice resource is Silk-like or carries a file token,
stages converted WAV data inside the existing per-event directory, and optionally transcribes the
staged file with the configured local Whisper runner.

## Files Changed

- `src/qq_agent_bridge/types.py`
  - Added `TranscriptStatus` (`verified` or `unavailable`).
- `src/qq_agent_bridge/resources.py`
  - Extended `PreparedResource` with transcript text, status, language, and error fields.
  - Added optional `record_url` and `transcriber` dependencies to `ResourceManager`.
  - Added voice source selection, WAV staging, soft per-resource failures, Whisper result mapping,
    and transcript context rendering.
- `src/qq_agent_bridge/main.py`
  - Creates exactly one `WhisperRunner` for the current configuration when Whisper is enabled and
    both binary and model are configured.
  - Rebuilds the resource manager and runner on config reload.
  - Keeps record-url injection optional for existing fake adapters.
- `tests/test_resources.py`
  - Added NapCat WAV resolver precedence, verified transcript, unavailable transcript, and raw-Silk
    soft-failure coverage.
- `tests/test_app_async.py`
  - Extended quoted-voice prompt coverage to assert the staged WAV path and verified transcript.
  - Added mixed text/voice conversion-failure coverage to ensure the text request still reaches the
    agent with an unavailable-transcript marker.

## RED Evidence

Command:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_resources.py tests/test_app_async.py -k "voice or transcript"
```

Before implementation it exited `1` with five expected failures:

- `ResourceManager` did not accept `record_url`.
- `ResourceManager` did not accept `transcriber`.
- Raw Silk voice entries were discarded after a failed download.
- `PreparedResource` did not accept transcript fields.
- App prompt tests could not construct verified or unavailable transcript resources.

## GREEN Evidence

Focused Task 4 check:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_resources.py tests/test_app_async.py -k "voice or transcript"
```

Result: `7 passed, 91 deselected`.

Affected module checks:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_resources.py tests/test_app_async.py
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_whisper_runner.py tests/test_onebot.py
```

Results: `98 passed`; `40 passed`.

Repository-wide verification:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q
```

Result: `409 passed, 11 skipped`.

## Behavior Notes

- A Silk-like resource without a successful conversion remains visible as a voice resource with a
  stable `transcript: unavailable` context line; it does not prevent unrelated text from reaching
  the agent.
- A successful conversion is always written with a `.wav` extension before transcription.
- Whisper `timeout` and `failed` statuses render bounded stable messages (`Whisper timeout` and
  `Whisper failed`) instead of process output. Runner `unavailable` errors are bounded to 500
  characters.

## Concerns

- The integration tests use fake converters and transcribers; a deployment still needs configured
  and readable Whisper binary/model files plus a NapCat `get_record` implementation that returns a
  fetchable WAV URL.
- Transcript text remains untrusted QQ-provided context. The existing prompt boundary continues to
  label attached resources as untrusted user content.

## Reviewer Fix: Resolver Failure Must Not Stage the Original Token URL

The reviewer found that a configured `record_url` resolver could return `None` or raise for a
file-token voice whose URL did not contain `silk`; `_prepare_voice` then fell through to the
original URL and staged it as decoded audio. The resource layer now records whether conversion was
attempted. Direct URL fallback is allowed only when no conversion was attempted and the original
URL is non-Silk audio. Resolver-backed file-token/Silk resources remain as voice entries with an
unavailable conversion/transcript status when conversion fails.

Added regression coverage in `tests/test_resources.py` for a file ID plus resolver failure. The test
asserts that the resolver is called, the raw token URL is never fetched, no decoded audio path is
staged, and the voice remains marked unavailable.

Fix verification:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_resources.py -k file_id_voice_does_not_fetch_original_url_after_resolver_failure
```

Result: `1 passed, 9 deselected`.

Focused voice/resource/app verification:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_resources.py tests/test_app_async.py -k "voice or transcript"
```

Result: `8 passed, 91 deselected`.

Full suite verification:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q
```

Result: `410 passed, 11 skipped`.
