# Whisper Voice Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every incoming QQ voice resource be converted, transcribed locally with whisper.cpp, and injected into ask, task, quoted-message, and proactive-chat context without hallucinating failures.

**Architecture:** Add a small `WhisperRunner` subprocess boundary and inject two callbacks into `ResourceManager`: one for NapCat record conversion and one for transcription. `OneBotAdapter` only resolves a record to a WAV URL through `get_record`; the resource layer stages the WAV and asks the runner for a transcript. The runner never invokes an agent and never writes outside its configured cache directory.

**Tech Stack:** Python 3.13, asyncio subprocesses, dataclasses, PyYAML, pytest, OneBot v11 reverse WebSocket, whisper.cpp `whisper-cli`, SHA-256 cache keys.

## Global Constraints

- QQ voice resources are automatically transcribed when `whisper.enabled` is true.
- NapCat conversion uses `get_record` with `out_format=wav`; the bridge does not implement Silk decoding.
- The default ASR concurrency is `1`; a voice failure is a soft failure that does not discard text or other resources.
- The runner uses `asyncio.create_subprocess_exec` and `communicate()`; it must not use `readline()` on agent or whisper output.
- The model and binary are configured paths; the open-source default does not download them or modify system Python/mamba.
- A transcript is evidence only when the runner exited successfully with non-empty text; unavailable transcripts must be marked unavailable in context.
- Task agent write permissions remain limited to the configured workspace/outbox.
- No model, private audio, cache, or personal `config.yaml` may be committed.

---

### Task 1: Add explicit Whisper configuration

**Files:**
- Modify: `src/qq_agent_bridge/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `WhisperConfig` with `enabled`, `binary`, `model`, `language`, `timeout_seconds`, `max_concurrent`, `cache_enabled`, `cache_root`, `cache_ttl_seconds`, and `cache_max_items`.
- Produces `BridgeConfig.whisper` and loads a top-level `whisper:` YAML mapping.

- [ ] **Step 1: Write the failing tests**

Add tests that load `config.example.yaml` and assert the example keeps `whisper.enabled` false, has empty `binary` and `model`, language `zh`, timeout `90`, concurrency `1`, and a cache root under `data/whisper-cache`. Add a temporary YAML test that loads all fields and clamps `max_concurrent` to at least `1`.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_config.py -k whisper
```

Expected: FAIL because `BridgeConfig` has no `whisper` field.

- [ ] **Step 3: Implement the configuration**

Add:

```python
@dataclass
class WhisperConfig:
    enabled: bool = False
    binary: str = ""
    model: str = ""
    language: str = "zh"
    timeout_seconds: float = 90.0
    max_concurrent: int = 1
    cache_enabled: bool = True
    cache_root: str = "data/whisper-cache"
    cache_ttl_seconds: int = 86400
    cache_max_items: int = 256
```

Add `whisper: WhisperConfig` to `BridgeConfig`, load it from `raw.get("whisper", {})`, and normalize `max_concurrent`, timeout, and cache limits to positive values at load time. Add a commented example block:

```yaml
whisper:
  enabled: false
  binary: ""
  model: ""
  language: "zh"
  timeout_seconds: 90
  max_concurrent: 1
  cache_enabled: true
  cache_root: "data/whisper-cache"
  cache_ttl_seconds: 86400
  cache_max_items: 256
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_config.py -k whisper
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/config.py config.example.yaml tests/test_config.py
git commit -m "feat: add whisper runtime configuration"
```

### Task 2: Implement the isolated Whisper runner

**Files:**
- Create: `src/qq_agent_bridge/whisper_runner.py`
- Test: `tests/test_whisper_runner.py`

**Interfaces:**
- Consumes `WhisperConfig` and an audio `Path`.
- Produces:

```python
@dataclass(frozen=True)
class TranscriptionResult:
    text: str | None
    status: Literal["ok", "unavailable", "timeout", "failed"]
    language: str | None = None
    error: str | None = None
```

- Exposes `WhisperRunner.transcribe(path: Path, *, language: str | None = None) -> Awaitable[TranscriptionResult]`.

- [ ] **Step 1: Write failing tests**

Use a temporary executable created by the test with `Path.write_text` and `chmod`, not the real model. Cover:

```python
async def test_runner_reads_text_file_without_readline(tmp_path):
    fake = make_fake_whisper(tmp_path, output="你好，世界")
    result = await WhisperRunner(make_cfg(fake)).transcribe(tmp_path / "input.wav")
    assert result == TranscriptionResult("你好，世界", "ok", "zh", None)

async def test_runner_reports_nonzero_exit_and_does_not_guess(tmp_path):
    fake = make_fake_whisper(tmp_path, exit_code=2, stderr="model missing")
    result = await WhisperRunner(make_cfg(fake)).transcribe(tmp_path / "input.wav")
    assert result.status == "failed"
    assert result.text is None
    assert "model missing" in (result.error or "")

async def test_runner_reports_timeout(tmp_path):
    fake = make_sleeping_whisper(tmp_path, seconds=1)
    cfg = make_cfg(fake, timeout_seconds=0.05)
    result = await WhisperRunner(cfg).transcribe(tmp_path / "input.wav")
    assert result.status == "timeout"
    assert result.text is None

async def test_runner_limits_concurrency_to_one(tmp_path):
    fake = make_recording_whisper(tmp_path, marker=tmp_path / "active")
    runner = WhisperRunner(make_cfg(fake, max_concurrent=1))
    first, second = await asyncio.gather(
        runner.transcribe(tmp_path / "one.wav"),
        runner.transcribe(tmp_path / "two.wav"),
    )
    assert first.status == second.status == "ok"
    assert (tmp_path / "max-active").read_text() == "1"
```

The fake executable must write the requested `-of` prefix plus `.txt`, so the test verifies the same output contract used by whisper.cpp.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_whisper_runner.py
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the subprocess boundary**

Build the command as an argv list:

```python
[
    str(cfg.binary),
    "-m", str(cfg.model),
    "-f", str(audio_path),
    "-l", language or cfg.language,
    "-otxt",
    "-of", str(output_prefix),
    "-nt",
    "-np",
]
```

Create a private temporary directory under `cache_root` for each run and execute:

```python
proc = await asyncio.create_subprocess_exec(
    *command,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await asyncio.wait_for(
    proc.communicate(),
    timeout=self.cfg.timeout_seconds,
)
```

Read only the generated text file; stdout is retained only for diagnostics. Validate binary, model, input file, and output file are regular files. On timeout kill and await the process, then return `status="timeout"`. Truncate stderr in the error field to 500 characters. Use an `asyncio.Semaphore(max_concurrent)` around the complete process lifetime. Never return a non-empty transcript for non-zero exit or empty output.

Implement a cache key from audio SHA-256, model path plus mtime/size, language, and runner version. Store JSON entries under the configured cache root, enforce TTL and max item count, and ignore malformed or stale entries. Do not cache failures.

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_whisper_runner.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/whisper_runner.py tests/test_whisper_runner.py
git commit -m "feat: add bounded whisper subprocess runner"
```

### Task 3: Add OneBot record conversion

**Files:**
- Modify: `src/qq_agent_bridge/onebot.py`
- Test: `tests/test_onebot.py`

**Interfaces:**
- Adds `OneBotAdapter.resolve_record_url(resource: ChatResource) -> Awaitable[str | None]`.
- Calls action `get_record` with `file` set to `resource.file_id` or `resource.url` and `out_format="wav"`.

- [ ] **Step 1: Write the failing tests**

Add a fake action response test that invokes `resolve_record_url` and asserts the outgoing frame is:

```python
{
    "action": "get_record",
    "params": {"file": "voice.silk", "out_format": "wav"},
}
```

Assert a response `{"file": "https://qq.example/voice.wav"}` returns that URL, and a response without `file`, `url`, or `path` returns `None`.

- [ ] **Step 2: Run the focused test**

Run:

```bash
.venv/bin/pytest -q tests/test_onebot.py -k record_url
```

Expected: FAIL because the method does not exist.

- [ ] **Step 3: Implement the method**

Call `_call_action("get_record", params, timeout=5.0)`. Accept only string values from `url`, `file`, or `path`, preferring `url`, then `file`, then `path`. Reject empty strings. Never expose the response object to the resource layer.

- [ ] **Step 4: Run the focused test**

Run:

```bash
.venv/bin/pytest -q tests/test_onebot.py -k record_url
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/onebot.py tests/test_onebot.py
git commit -m "feat: resolve QQ voice records as wav"
```

### Task 4: Enrich voice resources and context

**Files:**
- Modify: `src/qq_agent_bridge/resources.py`
- Modify: `src/qq_agent_bridge/types.py`
- Modify: `src/qq_agent_bridge/main.py`
- Test: `tests/test_resources.py`
- Test: `tests/test_app_async.py`

**Interfaces:**
- Adds `PreparedResource.transcript`, `transcript_status`, `transcript_language`, and `transcript_error`.
- `ResourceManager` accepts optional `record_url: Callable[[ChatResource], Awaitable[str | None]]` and `transcriber: WhisperRunner | None`.
- `format_resource_context` renders verified and unavailable transcript states.

- [ ] **Step 1: Write failing tests**

Add a fake transcriber and record resolver. Cover:

```python
async def test_voice_uses_napcat_wav_before_download_and_adds_transcript(tmp_path):
    resolved: list[str] = []

    async def record_url(resource):
        return "https://qq.example/voice.wav"

    async def fetch(url, limit):
        resolved.append(url)
        return b"wav", "audio/wav"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch,
                              record_url=record_url,
                              transcriber=FakeTranscriber("你好"))
    refs = await manager.prepare(make_ev((silk_resource(),)))
    assert resolved == ["https://qq.example/voice.wav"]
    assert refs[0].transcript == "你好"
    assert "verified by local Whisper" in format_resource_context(refs)

async def test_failed_transcription_keeps_audio_and_marks_unavailable(tmp_path):
    manager = ResourceManager(
        make_cfg(tmp_path),
        fetch=fetch_wav,
        transcriber=FailedTranscriber("model missing"),
    )
    refs = await manager.prepare(make_ev((wav_resource(),)))
    assert refs[0].local_path is not None
    assert refs[0].transcript is None
    assert refs[0].transcript_status == "unavailable"
    assert "model missing" in format_resource_context(refs)
```

Add an app test for a quoted voice event asserting the agent prompt contains the verified transcript and the local audio path. Add a test that ordinary text in the same event is still handled when voice conversion fails.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_resources.py tests/test_app_async.py -k "voice or transcript"
```

Expected: FAIL because the fields and callbacks do not exist.

- [ ] **Step 3: Implement resource enrich**

For a `voice` resource, choose the source in this order:

1. Call `record_url` when available and the resource may be Silk or a file token.
2. Fetch the returned WAV URL under the existing byte limits.
3. If no resolver is available and the original URL is already a non-Silk audio URL, fetch it directly.
4. If the resource is raw Silk and conversion failed, keep the resource entry with `transcript_status="unavailable"` rather than pretending it is decodable.

Stage the converted bytes under the existing event directory with a `.wav` extension. Invoke the runner after the file is written. Catch conversion, download, and runner exceptions per resource so other resources continue. Map runner `timeout` and `failed` to a stable unavailable message without including unbounded subprocess output.

Render:

```text
- voice: downloads/qq-agent-bridge/2026-07-15/ev-voice-123/voice.wav duration=12s, QQ voice limit=60s
  transcript (verified by local Whisper, language=zh): 你好
```

or:

```text
- voice: downloads/qq-agent-bridge/2026-07-15/ev-voice-123/voice.wav duration=12s, QQ voice limit=60s
  transcript: unavailable (Whisper timeout)
```

In `App.__init__` and the existing config reload path, instantiate `WhisperRunner(cfg.whisper)` only when enabled and configured, and pass `self.adapter.resolve_record_url` plus the runner into `ResourceManager`. Do not create a second runner during a single request. Keep fake-resource tests working by leaving callbacks optional.

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_resources.py tests/test_app_async.py -k "voice or transcript"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/resources.py src/qq_agent_bridge/types.py src/qq_agent_bridge/main.py tests/test_resources.py tests/test_app_async.py
git commit -m "feat: transcribe QQ voice resources into context"
```

### Task 5: Add isolated deployment and smoke checks

**Files:**
- Create: `runtime/asr/README.md`
- Create: `scripts/install_whisper_cpp.sh`
- Create: `scripts/check_whisper_cpp.sh`
- Modify: `.gitignore`
- Test: `tests/test_deployment_docs.py`

**Interfaces:**
- `scripts/install_whisper_cpp.sh` installs only under `${QAB_ASR_ROOT:-$HOME/.local/share/qq-agent-bridge/asr}`.
- `scripts/check_whisper_cpp.sh` validates the configured binary, model, and a WAV smoke input without modifying project files.

- [ ] **Step 1: Write failing documentation tests**

Assert the scripts exist, contain `set -euo pipefail`, default to a home-local path, do not contain `sudo apt`, `pip install`, `mamba install`, or writes to `/usr`, and that `.gitignore` excludes `runtime/asr/cache/`, `*.bin`, and `*.wav` under runtime deployment paths.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_deployment_docs.py
```

Expected: FAIL because the deployment files do not exist.

- [ ] **Step 3: Implement the deployment files**

The installer must clone the pinned whisper.cpp source into a temporary directory, configure a Release CPU build, copy only `whisper-cli` into `bin/`, download the configured model into `models/`, verify its SHA-256, and remove the temporary source tree. It must never alter `/usr`, the project `.venv`, or mamba base. The README must show the current Tiny Q8 model hash and the exact YAML needed to enable the local runner.

The checker must fail when binary/model paths are absent, run `whisper-cli` with `--help`, and optionally transcribe an explicitly supplied WAV. It must print timing and exit status without storing private audio in Git.

- [ ] **Step 4: Run tests and shell syntax checks**

Run:

```bash
.venv/bin/pytest -q tests/test_deployment_docs.py
bash -n scripts/install_whisper_cpp.sh scripts/check_whisper_cpp.sh
```

Expected: PASS.

- [ ] **Step 5: Commit project deployment support**

```bash
git add runtime/asr/README.md scripts/install_whisper_cpp.sh scripts/check_whisper_cpp.sh .gitignore tests/test_deployment_docs.py
git commit -m "docs: add isolated whisper deployment checks"
```

- [ ] **Step 6: Deploy the local runtime outside Git**

Run the installer only after reviewing its printed destination and SHA-256. Use the home-local default:

```bash
QAB_ASR_ROOT="$HOME/.local/share/qq-agent-bridge/asr" bash scripts/install_whisper_cpp.sh
```

Then run the checker against a temporary generated WAV and record the result. Do not copy the WAV, model, or cache into the repository.

### Task 6: Verify the full voice path

**Files:**
- Modify: `tests/test_agent_e2e.py`
- Modify: `tests/test_app_async.py` only if the integration fixture needs a fake OneBot resolver

**Interfaces:**
- Uses a fake whisper executable and fake OneBot `get_record` response; no network, real QQ account, or real model is required.

- [ ] **Step 1: Write the end-to-end test**

Create a normalized private voice event, connect the fake adapter, return a WAV URL from `get_record`, and have the fake runner produce `我是测试语音`. Assert the generated agent prompt contains exactly that transcript, that the task is invoked once, and that no unverified text is substituted when the runner exits non-zero.

- [ ] **Step 2: Run the test to verify the missing integration**

Run:

```bash
.venv/bin/pytest -q tests/test_agent_e2e.py -k voice_transcription
```

Expected: FAIL until the complete resource-to-prompt path is wired.

- [ ] **Step 3: Fix only integration wiring**

Do not add alternate ASR logic to the app test. Wire the existing fake adapter, resource manager, and runner boundaries so the same behavior used in production is exercised.

- [ ] **Step 4: Run the full verification**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all existing tests plus the new voice tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_agent_e2e.py tests/test_app_async.py
git commit -m "test: verify voice transcription end to end"
```
